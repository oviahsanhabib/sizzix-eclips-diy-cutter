#!/usr/bin/env python3
"""
eclips_cutter.py
-----------------
DIY SVG -> eclips cutting machine sender.

Built from a real USB capture of eCAL talking to a Sizzix eclips. The
machine speaks a dialect of HPGL (the same plotter language used by
Roland/Graphtec/HP pen plotters) plus a handful of Craft Edge vendor
extension commands prefixed with "!".

CONFIRMED from capture (high confidence):
    IN;               initialize
    SP1;              select pen/tool 1
    PU x,y;           pen up, travel move to x,y
    PD x,y;           pen down, cutting move to x,y
    PG;               end of page / finish job
    !ASP<a>,<b>;      speed/pressure-ish setting sent before moves
    !GBX; / !GBY;     query boundary X / Y (machine replies "!0;")
    !PON;             enable something (motor/pressure?) before a cut
    !SBP0,0;          set blade/something position

UNCONFIRMED / best guesses (marked in code, verify via your own captures):
    - Coordinate units (assumed 1000 units/inch -- THIS NEEDS CALIBRATION)
    - Y axis direction (assumed SVG y-down == machine y-down; toggle if mirrored)
    - Exact meaning/necessity of !L; and !MS;
    - Whether the machine requires waiting for "!0;"/"!1;" ACKs between commands

SAFETY: This is unverified reverse-engineered protocol. Test on scrap
material first, at low speed, with the blade retracted / no material
loaded for the very first dry run if possible. You are responsible for
what you send to your own hardware.

Requires:
    pip install pyserial svgelements

Usage:
    GUI mode:   python eclips_cutter.py
    CLI mode:   python eclips_cutter.py --svg design.svg --port COM3 --dry-run
                python eclips_cutter.py --svg design.svg --port COM3 --send
"""

import sys
import time
import argparse
import threading

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("Missing dependency. Run: pip install pyserial --break-system-packages")
    sys.exit(1)

try:
    from svgelements import SVG, Path, Move, Close, Line as SvgLine
except ImportError:
    print("Missing dependency. Run: pip install svgelements --break-system-packages")
    sys.exit(1)


# ----------------------------------------------------------------------
# Configuration defaults (EDIT / calibrate these against your machine)
# ----------------------------------------------------------------------

DEFAULT_UNITS_PER_INCH = 1200   # CALIBRATED from real test cut: requested 4in,
                                   # measured 2.92in @ 1000 units/in => 1000*(4/2.92)≈1370.
                                   # Re-verify with your own test cut; this is one data point.
DEFAULT_SPEED = 20                 # from capture: !ASP80,55 seen at job start
DEFAULT_PRESSURE = 70              # from capture: !ASP80,55 seen at job start
DEFAULT_FLATNESS_STEPS = 12        # bezier/arc flattening resolution
DEFAULT_INTER_COMMAND_DELAY = 0.03 # seconds; capture showed ~30ms between PD commands
DEFAULT_BAUD = 9600                # CDC-ACM devices usually ignore baud, but pyserial needs a value


# ----------------------------------------------------------------------
# SVG -> flattened point paths
# ----------------------------------------------------------------------

def svg_to_polylines(svg_path, flatness_steps=DEFAULT_FLATNESS_STEPS):
    """
    Parse an SVG file and return a list of subpaths, where each subpath
    is a list of (x, y) tuples in SVG user units (typically px, 96/inch).
    Curves are flattened into line segments.
    """
    svg = SVG.parse(svg_path)
    subpaths = []
    current = []

    for element in svg.elements():
        if not isinstance(element, Path):
            continue
        # apply any transforms already baked in by svgelements
        path = element

        for seg in path.segments():
            if isinstance(seg, Move):
                if len(current) > 1:
                    subpaths.append(current)
                current = [(seg.end[0], seg.end[1])]
            elif isinstance(seg, Close):
                if current:
                    start = current[0]
                    current.append(start)
            elif isinstance(seg, SvgLine):
                current.append((seg.end[0], seg.end[1]))
            else:
                # Cubic/Quadratic Bezier or Arc -- sample it
                for i in range(1, flatness_steps + 1):
                    t = i / flatness_steps
                    pt = seg.point(t)
                    current.append((pt[0], pt[1]))

        if len(current) > 1:
            subpaths.append(current)
        current = []

    return subpaths


# ----------------------------------------------------------------------
# Coordinate transform: SVG units -> machine units
# ----------------------------------------------------------------------

def transform_points(subpaths, units_per_inch, svg_units_per_inch=96.0,
                      offset_x=0.0, offset_y=0.0, flip_y=False,
                      canvas_height=None):
    """
    Convert SVG (px, 96/in by default) coordinates into machine units.
    """
    scale = units_per_inch / svg_units_per_inch
    out = []
    for subpath in subpaths:
        new_sub = []
        for (x, y) in subpath:
            mx = x * scale + offset_x
            if flip_y and canvas_height is not None:
                my = (canvas_height - y) * scale + offset_y
            else:
                my = y * scale + offset_y
            new_sub.append((round(mx), round(my)))
        out.append(new_sub)
    return out


# ----------------------------------------------------------------------
# HPGL command generation (eclips dialect)
# ----------------------------------------------------------------------

def build_hpgl(subpaths, speed=DEFAULT_SPEED, pressure=DEFAULT_PRESSURE):
    """
    Build the list of command strings to send, mirroring the structure
    seen in the captured traffic:

        IN;SP1;
        !ASP<speed>,<pressure>;
        PU <first point of first subpath>;
        PG;                          <- only seen for a "positioning only" job;
                                         real cut jobs go straight into PU/PD

    For an actual cut job (matches capture from frame 103 onward):
        IN;SP1;
        !ASP<speed2>,<pressure>;      <- second ASP call, different speed
        PU x0,y0;PD x1,y1;PD x2,y2; ...
        PG;
    """
    commands = []
    commands.append("IN;SP1;")
    commands.append(f"!ASP{speed},{pressure};")

    for subpath in subpaths:
        if not subpath:
            continue
        x0, y0 = subpath[0]
        commands.append(f"PU{x0},{y0};")
        # Batch PD commands two-per-line like the real capture did
        # (not required, but mirrors observed traffic pattern)
        pd_chunk = []
        for (x, y) in subpath[1:]:
            pd_chunk.append(f"PD{x},{y};")
            if len(pd_chunk) == 2:
                commands.append("".join(pd_chunk))
                pd_chunk = []
        if pd_chunk:
            commands.append("".join(pd_chunk))

    commands.append("PG;")
    return commands


# ----------------------------------------------------------------------
# Serial sender
# ----------------------------------------------------------------------

def list_com_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


def send_to_machine(port, commands, baud=DEFAULT_BAUD,
                     delay=DEFAULT_INTER_COMMAND_DELAY,
                     wait_for_ack=False, log=print):
    ser = serial.Serial(port, baud, timeout=1)
    try:
        log(f"Opened {port} @ {baud} baud")
        time.sleep(0.5)  # let the port settle
        for cmd in commands:
            ser.write(cmd.encode("ascii"))
            log(f"-> {cmd}")
            if wait_for_ack:
                resp = ser.read(16)
                if resp:
                    log(f"<- {resp!r}")
            time.sleep(delay)
        log("Done sending.")
    finally:
        ser.close()


def toggle_load_unload_mat(port, baud=DEFAULT_BAUD, log=print, settle_seconds=11.0):
    """
    Sends the inferred mat load/unload sequence: "!L;" then "!MS;".

    INFERRED, not confirmed -- but the timing in the original capture is a
    strong clue: !L; was sent, then ~11 seconds later (matching the time
    the mat mechanically takes to move) !MS; was sent and got an immediate
    reply (!1;). If the firmware treats the motor as "still active" until
    it receives that !MS; follow-up, sending !L; alone and disconnecting
    (as the previous version of this function did) could leave the motor
    running indefinitely -- which matches "continuously unloading" behavior.

    WATCH THE MACHINE the first few times you use this. If it still
    doesn't stop after !MS; is sent, power off the machine immediately
    rather than let the motor keep running, and let me know what you saw --
    we'll need another capture of eCAL's own load/unload button in
    isolation to nail this down further.
    """
    ser = serial.Serial(port, baud, timeout=1)
    try:
        log(f"Opened {port} @ {baud} baud")
        time.sleep(0.5)

        ser.write(b"!L;")
        log("-> !L;  (load/unload mat, starts motor motion)")
        resp = ser.read(16)
        if resp:
            log(f"<- {resp!r}")

        log(f"Waiting {settle_seconds:.0f}s for mechanical motion to finish...")
        time.sleep(settle_seconds)

        ser.write(b"!MS;")
        log("-> !MS;  (confirm/finalize -- this may be what stops the motor)")
        resp = ser.read(16)
        if resp:
            log(f"<- {resp!r}")
        else:
            log("(no response to !MS; -- motor may still be running, watch the machine)")
    finally:
        ser.close()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def run_cli():
    parser = argparse.ArgumentParser(description="SVG -> eclips HPGL sender")
    parser.add_argument("--svg", help="Path to SVG file")
    parser.add_argument("--port", help="COM port, e.g. COM3")
    parser.add_argument("--units-per-inch", type=float, default=DEFAULT_UNITS_PER_INCH)
    parser.add_argument("--speed", type=int, default=DEFAULT_SPEED)
    parser.add_argument("--pressure", type=int, default=DEFAULT_PRESSURE)
    parser.add_argument("--offset-x", type=float, default=0.0)
    parser.add_argument("--offset-y", type=float, default=0.0)
    parser.add_argument("--flip-y", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print/save HPGL, don't send")
    parser.add_argument("--send", action="store_true", help="Actually send to the machine")
    parser.add_argument("--save", help="Save generated HPGL to this file")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--load-unload", action="store_true",
                         help="Just toggle the mat load/unload command and exit (needs --port)")
    parser.add_argument("--auto-unload", action="store_true",
                         help="Send the load/unload toggle right after the job finishes")
    args = parser.parse_args()

    if args.list_ports:
        ports = list_com_ports()
        print("Available COM ports:" if ports else "No COM ports found.")
        for p in ports:
            print(f"  {p}")
        return

    if args.load_unload:
        if not args.port:
            print("ERROR: --load-unload requires --port COMx")
            return
        toggle_load_unload_mat(args.port)
        return

    if not args.svg:
        print("No --svg given, launching GUI instead...")
        run_gui()
        return

    subpaths = svg_to_polylines(args.svg)
    subpaths = transform_points(
        subpaths,
        units_per_inch=args.units_per_inch,
        offset_x=args.offset_x,
        offset_y=args.offset_y,
        flip_y=args.flip_y,
    )
    commands = build_hpgl(subpaths, speed=args.speed, pressure=args.pressure)

    print(f"Generated {len(commands)} commands from {len(subpaths)} subpaths.")

    if args.save:
        with open(args.save, "w") as f:
            f.write("\n".join(commands))
        print(f"Saved HPGL to {args.save}")

    if args.dry_run or not args.send:
        for c in commands[:20]:
            print(c)
        if len(commands) > 20:
            print(f"... ({len(commands) - 20} more commands)")
        if not args.send:
            print("\n(dry run only -- pass --send --port COMx to actually cut)")

    if args.send:
        if not args.port:
            print("ERROR: --send requires --port COMx")
            return
        confirm = input(f"About to send to {args.port}. Machine loaded and ready? [y/N] ")
        if confirm.lower() != "y":
            print("Aborted.")
            return
        send_to_machine(args.port, commands)
        if args.auto_unload:
            print("Job finished, sending load/unload toggle...")
            time.sleep(1.0)
            toggle_load_unload_mat(args.port)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk, scrolledtext

    root = tk.Tk()
    root.title("eclips DIY Cutter (unofficial)")
    root.geometry("760x680")
    root.minsize(700, 550)   # prevents the button row from being squeezed off-screen

    state = {"svg_path": None, "commands": [], "subpaths_svg_units": None}

    # --- File selection row ---
    frm_file = ttk.Frame(root, padding=8)
    frm_file.pack(fill="x")
    lbl_file = ttk.Label(frm_file, text="No SVG selected")
    lbl_file.pack(side="left", padx=(0, 8))

    def choose_file():
        path = filedialog.askopenfilename(filetypes=[("SVG files", "*.svg")])
        if path:
            state["svg_path"] = path
            lbl_file.config(text=path.split("/")[-1].split("\\")[-1])

    ttk.Button(frm_file, text="Open SVG...", command=choose_file).pack(side="left")

    # --- Action buttons: packed early (right under file row) so they're
    # ALWAYS visible regardless of window size / how much the log box grows ---
    frm_actions = ttk.LabelFrame(root, text="Actions", padding=8)
    frm_actions.pack(fill="x", padx=8, pady=(0, 8))

    # --- Settings grid ---
    frm_settings = ttk.LabelFrame(root, text="Calibration & Settings", padding=8)
    frm_settings.pack(fill="x", padx=8, pady=8)

    def add_row(row, label, default):
        ttk.Label(frm_settings, text=label).grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=str(default))
        ttk.Entry(frm_settings, textvariable=var, width=12).grid(row=row, column=1, sticky="w", padx=8)
        return var

    var_units = add_row(0, "Machine units per inch (CALIBRATE THIS):", DEFAULT_UNITS_PER_INCH)
    var_speed = add_row(1, "Speed:", DEFAULT_SPEED)
    var_pressure = add_row(2, "Pressure:", DEFAULT_PRESSURE)
    var_offx = add_row(3, "Offset X:", 0)
    var_offy = add_row(4, "Offset Y:", 0)
    var_flip = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_settings, text="Flip Y axis (try this if cuts appear mirrored/upside-down)",
                     variable=var_flip).grid(row=5, column=0, columnspan=2, sticky="w", pady=4)

    # --- COM port row ---
    frm_port = ttk.LabelFrame(root, text="Machine Connection", padding=8)
    frm_port.pack(fill="x", padx=8, pady=(0, 8))

    ttk.Label(frm_port, text="COM Port:").grid(row=0, column=0, sticky="w")
    port_var = tk.StringVar()
    port_combo = ttk.Combobox(frm_port, textvariable=port_var, width=15)
    port_combo.grid(row=0, column=1, padx=8)

    def refresh_ports():
        ports = list_com_ports()
        port_combo["values"] = ports
        if ports:
            port_var.set(ports[0])

    ttk.Button(frm_port, text="Refresh", command=refresh_ports).grid(row=0, column=2, padx=4)
    refresh_ports()

    var_wait_ack = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_port, text="Wait for ACK after each command (slower, safer)",
                     variable=var_wait_ack).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)

    var_auto_unload = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_port, text="Auto load/unload mat after job finishes",
                     variable=var_auto_unload).grid(row=2, column=0, columnspan=3, sticky="w")

    def load_unload_clicked():
        port = port_var.get()
        if not port:
            messagebox.showwarning("No port", "Select a COM port.")
            return

        def worker():
            try:
                toggle_load_unload_mat(port, log=log)
            except Exception as e:
                log(f"ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(frm_port, text="Load/Unload Mat", command=load_unload_clicked).grid(
        row=0, column=3, padx=8)

    # --- Log output ---
    frm_log = ttk.LabelFrame(root, text="Output / Generated HPGL", padding=8)
    frm_log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    log_box = scrolledtext.ScrolledText(frm_log, height=15, font=("Consolas", 9))
    log_box.pack(fill="both", expand=True)

    def log(msg):
        log_box.insert("end", str(msg) + "\n")
        log_box.see("end")
        root.update_idletasks()

    # --- Actions ---
    def generate():
        if not state["svg_path"]:
            messagebox.showwarning("No file", "Choose an SVG file first.")
            return
        log_box.delete("1.0", "end")
        try:
            subpaths = svg_to_polylines(state["svg_path"])
            subpaths_m = transform_points(
                subpaths,
                units_per_inch=float(var_units.get()),
                offset_x=float(var_offx.get()),
                offset_y=float(var_offy.get()),
                flip_y=var_flip.get(),
            )
            commands = build_hpgl(subpaths_m, speed=int(float(var_speed.get())),
                                   pressure=int(float(var_pressure.get())))
            state["commands"] = commands
            log(f"Parsed {len(subpaths)} subpath(s) from SVG.")
            log(f"Generated {len(commands)} HPGL commands.\n")
            for c in commands[:40]:
                log(c)
            if len(commands) > 40:
                log(f"... ({len(commands) - 40} more)")
        except Exception as e:
            messagebox.showerror("Error", str(e))
            log(f"ERROR: {e}")

    def save_hpgl():
        if not state["commands"]:
            messagebox.showwarning("Nothing to save", "Generate HPGL first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".hpgl",
                                             filetypes=[("HPGL", "*.hpgl"), ("Text", "*.txt")])
        if path:
            with open(path, "w") as f:
                f.write("\n".join(state["commands"]))
            log(f"Saved to {path}")

    def send():
        if not state["commands"]:
            messagebox.showwarning("Nothing to send", "Generate HPGL first.")
            return
        port = port_var.get()
        if not port:
            messagebox.showwarning("No port", "Select a COM port.")
            return
        if not messagebox.askyesno(
            "Confirm cut",
            f"About to send {len(state['commands'])} commands to {port}.\n\n"
            "Is the machine loaded with scrap material and ready?\n"
            "(This protocol is reverse-engineered and unverified -- "
            "start with a simple test shape.)"
        ):
            return

        def worker():
            try:
                send_to_machine(port, state["commands"],
                                 wait_for_ack=var_wait_ack.get(), log=log)
                if var_auto_unload.get():
                    log("Auto-unload enabled, sending load/unload toggle...")
                    time.sleep(1.0)
                    toggle_load_unload_mat(port, log=log)
            except Exception as e:
                log(f"ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(frm_actions, text="1. Generate HPGL", command=generate).pack(side="left", padx=4)
    ttk.Button(frm_actions, text="2. Save to file", command=save_hpgl).pack(side="left", padx=4)
    ttk.Button(frm_actions, text="3. Send to Machine", command=send).pack(side="left", padx=4)

    log("Ready. This tool is built from a reverse-engineered protocol -- ")
    log("calibrate 'units per inch' with a known-size test cut before trusting it on real material.\n")

    root.mainloop()


# ----------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli()
    else:
        run_gui()
