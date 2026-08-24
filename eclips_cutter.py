#!/usr/bin/env python3
"""
eclips_cutter.py
-----------------
Developer   : Md Ahsan Habib
Email       : ahovi57@gmail.com
Version     : V2
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
    !GBX; / !GBY;     query boundary X / Y (machine replies with a coordinate)
    !PON;             enable blade motor/pressure before cutting moves
    !POF;             disable blade motor/pressure before pure travel moves
    !SBP x,y;         Set Beam Position -- moves the laser pointer target
    !LON; / !LOF;     Laser ON / Laser OFF -- visual boundary preview laser
    !SBP0,0;          set blade/something position
    !L;               load/unload mat toggle (starts motor motion)
    !MS;              sent ~11s after !L; -- appears to confirm/finalize
                       the load/unload motion (see toggle_load_unload_mat)

UNCONFIRMED / best guesses (marked in code, verify via your own captures):
    - Y axis direction (assumed SVG y-down == machine y-down; toggle if mirrored)
    - Whether the machine requires waiting for "!0;"/"!1;" ACKs between commands
    - Exact relationship between multiple !ASP values within a single job
      (speed appears to ramp between values at path start/end/corners)

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

DEFAULT_UNITS_PER_INCH = 1365.33  # CONFIRMED by user testing on real machine: verified
                                   # accurate against BOTH a 4in and 6in design. This
                                   # compensates for the SVG file's likely 72px/inch
                                   # authoring scale combined with the machine's real
                                   # 1024 units/inch (72/96 * 1365.33 / ... nets out
                                   # correctly for this workflow -- trust the test result
                                   # over the theory if a future SVG behaves differently).
                                   # NOTE: this is an SVG->machine SCALE FACTOR, not the
                                   # machine's true physical resolution. It already bakes
                                   # in a 72/96 dpi compensation on top of that resolution.
                                   # Do NOT use this constant to convert machine units back
                                   # into real-world inches (e.g. for on-screen previews) --
                                   # use MACHINE_UNITS_PER_INCH below for that instead.
MACHINE_UNITS_PER_INCH = 1024      # The eclips's actual physical resolution (units/inch).
                                    # Use this -- not DEFAULT_UNITS_PER_INCH -- whenever you
                                    # need to convert machine units back into true inches,
                                    # e.g. for the mat preview or the "design size" readout.

DEFAULT_OFFSET_X = 50              # CONFIRMED by user testing: with these offsets, cuts land
DEFAULT_OFFSET_Y = 600              # exactly at the edge of the vinyl sheet on their mat. This
                                    # is empirical mat/machine-home calibration, not a simple
                                    # inch-based margin -- don't try to derive it from
                                    # MACHINE_UNITS_PER_INCH, trust the test result (same
                                    # philosophy as DEFAULT_UNITS_PER_INCH above).

# The mat's sticky/adhesive area is inset from the mat's own physical edges.
# X=50 / Y=550 (see DEFAULT_OFFSET_X/Y above) is the calibrated "true edge"
# home position for this mat -- the preview treats that offset as its zero
# point, so a design cut with the default offsets shows flush against the
# top-left corner of the preview, matching where it actually lands physically.

DEFAULT_SPEED = 80                 # from capture: !ASP80,55 seen at job start
DEFAULT_PRESSURE = 55              # from capture: !ASP80,55 seen at job start
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
                     ack_count=0, log=print):
    """
    Send a list of commands to the machine.

    ack_count: wait for and log a response after each of the first
    `ack_count` commands (useful for confirming the init/handshake
    sequence landed correctly). Remaining commands are sent without
    waiting for a reply, matching how the real capture looked -- the
    bulk PU/PD stream during a cut has no per-command ACKs, only a
    single "!0;" at the very end of the job.
    """
    ser = serial.Serial(port, baud, timeout=1)
    try:
        log(f"Opened {port} @ {baud} baud")
        time.sleep(0.5)  # let the port settle
        for i, cmd in enumerate(commands):
            ser.write(cmd.encode("ascii"))
            log(f"-> {cmd}")
            if i < ack_count:
                resp = ser.read(16)
                if resp:
                    log(f"<- {resp!r}")
                else:
                    log("   (no ACK received)")
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


def laser_on(port, baud=DEFAULT_BAUD, log=print):
    """
    Turns the boundary-preview laser ON. CONFIRMED command from capture
    (!LON;), seen paired with !SBP (set beam position) to trace the
    design's bounding box before cutting. Called standalone here, so it
    will turn on at whatever position the laser is currently at.
    """
    ser = serial.Serial(port, baud, timeout=1)
    try:
        log(f"Opened {port} @ {baud} baud")
        time.sleep(0.5)
        ser.write(b"!LON;")
        log("-> !LON;  (laser on)")
        resp = ser.read(16)
        if resp:
            log(f"<- {resp!r}")
    finally:
        ser.close()


def laser_off(port, baud=DEFAULT_BAUD, log=print):
    """Turns the boundary-preview laser OFF. CONFIRMED command (!LOF;)."""
    ser = serial.Serial(port, baud, timeout=1)
    try:
        log(f"Opened {port} @ {baud} baud")
        time.sleep(0.5)
        ser.write(b"!LOF;")
        log("-> !LOF;  (laser off)")
        resp = ser.read(16)
        if resp:
            log(f"<- {resp!r}")
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
    parser.add_argument("--offset-x", type=float, default=DEFAULT_OFFSET_X)
    parser.add_argument("--offset-y", type=float, default=DEFAULT_OFFSET_Y)
    parser.add_argument("--flip-y", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print/save HPGL, don't send")
    parser.add_argument("--send", action="store_true", help="Actually send to the machine")
    parser.add_argument("--save", help="Save generated HPGL to this file")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--load-unload", action="store_true",
                         help="Just toggle the mat load/unload command and exit (needs --port)")
    parser.add_argument("--auto-unload", action="store_true",
                         help="Send the load/unload toggle right after the job finishes")
    parser.add_argument("--ack-count", type=int, default=6,
                         help="Wait for a response after each of the first N commands "
                              "(the init/handshake sequence). Default 3. Use 0 to disable.")
    parser.add_argument("--laser-on", action="store_true",
                         help="Just turn the boundary preview laser on and exit (needs --port)")
    parser.add_argument("--laser-off", action="store_true",
                         help="Just turn the boundary preview laser off and exit (needs --port)")
    args = parser.parse_args()

    if args.list_ports:
        ports = list_com_ports()
        print("Available COM ports:" if ports else "No COM ports found.")
        for p in ports:
            print(f"  {p}")
        return

    if args.laser_on:
        if not args.port:
            print("ERROR: --laser-on requires --port COMx")
            return
        laser_on(args.port)
        return

    if args.laser_off:
        if not args.port:
            print("ERROR: --laser-off requires --port COMx")
            return
        laser_off(args.port)
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
        send_to_machine(args.port, commands, ack_count=args.ack_count)
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

    # ---------------- Color palette / theme ----------------
    COL_BG = "#1e2530"        # app background (dark slate)
    COL_PANEL = "#2a3342"     # panel background
    COL_ACCENT = "#4fc3f7"    # cyan accent (matches "laser" theme)
    COL_ACCENT2 = "#66bb6a"   # green for safe actions
    COL_WARN = "#ef5350"      # red for send/cut action
    COL_TEXT = "#e8ecf1"
    COL_SUBTEXT = "#9aa7b8"
    COL_MAT = "#f4efe4"       # cutting mat cream color
    COL_MAT_GRID = "#d8cfb8"
    COL_MAT_GRID_BOLD = "#b8ab88"
    COL_DESIGN = "#e53935"    # design outline, like a red cut-line preview

    root = tk.Tk()
    root.title("Sizzix eClips DIY Cutter (unofficial) V2.01")
    root.geometry("1180x760")
    root.minsize(1000, 620)
    root.configure(bg=COL_BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=COL_BG, foreground=COL_TEXT, font=("Segoe UI", 9))
    style.configure("TFrame", background=COL_BG)
    style.configure("Panel.TFrame", background=COL_PANEL)
    style.configure("TLabelframe", background=COL_BG, foreground=COL_TEXT, borderwidth=1)
    style.configure("TLabelframe.Label", background=COL_BG, foreground=COL_ACCENT,
                     font=("Segoe UI Semibold", 10))
    style.configure("TLabel", background=COL_BG, foreground=COL_TEXT)
    style.configure("Sub.TLabel", background=COL_BG, foreground=COL_SUBTEXT, font=("Segoe UI", 8))
    style.configure("TCheckbutton", background=COL_BG, foreground=COL_TEXT)
    style.configure("TEntry", fieldbackground="#ffffff", foreground="#14181f",
                     insertcolor="#14181f")
    style.configure("TCombobox", fieldbackground="#ffffff", foreground="#14181f")
    style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")],
              foreground=[("readonly", "#14181f")])
    style.configure("TButton", padding=6, font=("Segoe UI", 9))
    style.configure("Accent.TButton", background=COL_ACCENT2, foreground="#0a1a0a")
    style.map("Accent.TButton", background=[("active", "#7fd482")])
    style.configure("Warn.TButton", background=COL_WARN, foreground="#2a0a0a")
    style.map("Warn.TButton", background=[("active", "#ff7a75")])
    style.configure("Laser.TButton", background=COL_ACCENT, foreground="#062028")
    style.map("Laser.TButton", background=[("active", "#7fd9ff")])

    state = {"svg_path": None, "commands": [], "preview_subpaths_in": None}

    # ================= Title bar =================
    frm_title = tk.Frame(root, bg=COL_PANEL, height=48)
    frm_title.pack(fill="x")
    tk.Label(frm_title, text="\u2702  Sizzix eClips DIY Cutter", bg=COL_PANEL, fg=COL_TEXT,
             font=("Segoe UI Semibold", 14)).pack(side="left", padx=14, pady=8)
    tk.Label(frm_title, text="unofficial \u00b7 reverse-engineered protocol", bg=COL_PANEL,
             fg=COL_SUBTEXT, font=("Segoe UI", 9)).pack(side="left", pady=8)

    # ================= Body: left controls / right preview =================
    frm_body = ttk.Frame(root)
    frm_body.pack(fill="both", expand=True)

    left_col = ttk.Frame(frm_body)
    left_col.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=8)

    right_col = ttk.Frame(frm_body)
    right_col.pack(side="right", fill="y", padx=(4, 8), pady=8)

    # --- File selection row ---
    frm_file = ttk.Frame(left_col)
    frm_file.pack(fill="x")
    lbl_file = ttk.Label(frm_file, text="No SVG selected", style="Sub.TLabel")
    lbl_file.pack(side="left", padx=(0, 8))

    def choose_file():
        path = filedialog.askopenfilename(filetypes=[("SVG files", "*.svg")])
        if path:
            state["svg_path"] = path
            lbl_file.config(text=path.split("/")[-1].split("\\")[-1], style="TLabel")
            generate()  # auto-preview as soon as a file is chosen

    ttk.Button(frm_file, text="\U0001F4C1 Open SVG...", command=choose_file).pack(side="left")

    # --- Action buttons ---
    frm_actions = ttk.LabelFrame(left_col, text="Actions", padding=10)
    frm_actions.pack(fill="x", pady=(8, 8))

    # --- Settings grid ---
    frm_settings = ttk.LabelFrame(left_col, text="Calibration & Settings", padding=10)
    frm_settings.pack(fill="x", pady=(0, 8))

    def add_row(row, label, default):
        ttk.Label(frm_settings, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=str(default))
        ttk.Entry(frm_settings, textvariable=var, width=12).grid(row=row, column=1, sticky="w", padx=8)
        return var

    var_units = add_row(0, "Machine units per inch:", DEFAULT_UNITS_PER_INCH)
    var_speed = add_row(1, "Speed:", DEFAULT_SPEED)
    var_pressure = add_row(2, "Pressure:", DEFAULT_PRESSURE)
    var_offx = add_row(3, "Offset X (units):", DEFAULT_OFFSET_X)
    var_offy = add_row(4, "Offset Y (units):", DEFAULT_OFFSET_Y)
    var_flip = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_settings, text="Flip Y axis (try if cuts appear mirrored/upside-down)",
                     variable=var_flip).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # --- COM port / machine connection ---
    frm_port = ttk.LabelFrame(left_col, text="Machine Connection", padding=10)
    frm_port.pack(fill="x", pady=(0, 8))

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

    var_ack_count = tk.StringVar(value="3")
    ttk.Label(frm_port, text="Wait for ACK on first N commands:").grid(
        row=1, column=0, sticky="w", pady=6)
    ttk.Entry(frm_port, textvariable=var_ack_count, width=6).grid(
        row=1, column=1, sticky="w", pady=6)
    ttk.Label(frm_port, text="(handshake only, not the whole job -- 0 disables)",
              style="Sub.TLabel").grid(row=1, column=2, columnspan=2, sticky="w", pady=6)

    var_auto_unload = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm_port, text="Auto load/unload mat after job finishes",
                     variable=var_auto_unload).grid(row=2, column=0, columnspan=2, sticky="w")

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

    def laser_on_clicked():
        port = port_var.get()
        if not port:
            messagebox.showwarning("No port", "Select a COM port.")
            return

        def worker():
            try:
                laser_on(port, log=log)
            except Exception as e:
                log(f"ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def laser_off_clicked():
        port = port_var.get()
        if not port:
            messagebox.showwarning("No port", "Select a COM port.")
            return

        def worker():
            try:
                laser_off(port, log=log)
            except Exception as e:
                log(f"ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(frm_port, text="Load/Unload Mat", command=load_unload_clicked).grid(
        row=0, column=3, padx=8)
    ttk.Button(frm_port, text="Laser ON", style="Laser.TButton",
               command=laser_on_clicked).grid(row=2, column=3, padx=8, sticky="w")
    ttk.Button(frm_port, text="Laser OFF", command=laser_off_clicked).grid(
        row=2, column=4, padx=4, sticky="w")

    # --- Log output ---
    frm_log = ttk.LabelFrame(left_col, text="Output / Generated HPGL", padding=8)
    frm_log.pack(fill="both", expand=True)
    log_box = scrolledtext.ScrolledText(frm_log, height=12, font=("Consolas", 9),
                                         bg="#10151c", fg="#8fe38f", insertbackground="#8fe38f")
    log_box.pack(fill="both", expand=True)

    def log(msg):
        log_box.insert("end", str(msg) + "\n")
        log_box.see("end")
        root.update_idletasks()

    # ================= Right column: Mat Preview =================
    frm_preview = ttk.LabelFrame(right_col, text="Mat Preview -- 12\" x 12\" sheet", padding=10)
    frm_preview.pack(fill="both", expand=True)

    MAT_INCHES = 12.0
    CANVAS_PX = 460  # square canvas, px per inch = CANVAS_PX / MAT_INCHES

    preview_canvas = tk.Canvas(frm_preview, width=CANVAS_PX, height=CANVAS_PX,
                                bg=COL_MAT, highlightthickness=1,
                                highlightbackground="#555")
    preview_canvas.pack()

    lbl_dims = ttk.Label(frm_preview, text="No design loaded", style="Sub.TLabel")
    lbl_dims.pack(anchor="w", pady=(8, 0))

    def draw_mat_grid():
        preview_canvas.delete("grid")
        px_per_in = CANVAS_PX / MAT_INCHES
        for i in range(0, int(MAT_INCHES) + 1):
            x = i * px_per_in
            bold = (i % 6 == 0)
            preview_canvas.create_line(x, 0, x, CANVAS_PX, tags="grid",
                                        fill=COL_MAT_GRID_BOLD if bold else COL_MAT_GRID,
                                        width=1.4 if bold else 1)
            preview_canvas.create_line(0, x, CANVAS_PX, x, tags="grid",
                                        fill=COL_MAT_GRID_BOLD if bold else COL_MAT_GRID,
                                        width=1.4 if bold else 1)
        # inch tick labels along top/left edges
        for i in range(0, int(MAT_INCHES) + 1, 2):
            preview_canvas.create_text(i * px_per_in + 3, 3, text=str(i), anchor="nw",
                                        fill=COL_MAT_GRID_BOLD, font=("Segoe UI", 7), tags="grid")

    def draw_design_preview(subpaths_inches):
        preview_canvas.delete("design")
        px_per_in = CANVAS_PX / MAT_INCHES
        for subpath in subpaths_inches:
            if len(subpath) < 2:
                continue
            pts = []
            for (x_in, y_in) in subpath:
                pts.append(x_in * px_per_in)
                pts.append(y_in * px_per_in)
            preview_canvas.create_line(*pts, fill=COL_DESIGN, width=2, tags="design",
                                        joinstyle="round", capstyle="round")
        # bounding box + out-of-bounds warning
        all_pts = [p for sp in subpaths_inches for p in sp]
        if all_pts:
            xs = [p[0] for p in all_pts]
            ys = [p[1] for p in all_pts]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            out_of_bounds = max(xs) > MAT_INCHES or max(ys) > MAT_INCHES or min(xs) < 0 or min(ys) < 0
            lbl_dims.config(
                text=f"Design size: {w:.2f}\" x {h:.2f}\"   |   position: "
                     f"({min(xs):.2f}\", {min(ys):.2f}\") to ({max(xs):.2f}\", {max(ys):.2f}\")"
                     + ("   \u26a0 OUTSIDE MAT AREA" if out_of_bounds else ""),
                style="Sub.TLabel" if not out_of_bounds else None,
                foreground=(COL_WARN if out_of_bounds else COL_SUBTEXT),
                background=COL_BG,
            )

    draw_mat_grid()

    # --- Actions ---
    def generate():
        if not state["svg_path"]:
            messagebox.showwarning("No file", "Choose an SVG file first.")
            return
        log_box.delete("1.0", "end")
        try:
            units_per_inch = float(var_units.get())
            subpaths = svg_to_polylines(state["svg_path"])
            subpaths_m = transform_points(
                subpaths,
                units_per_inch=units_per_inch,
                offset_x=float(var_offx.get()),
                offset_y=float(var_offy.get()),
                flip_y=var_flip.get(),
            )
            commands = build_hpgl(subpaths_m, speed=int(float(var_speed.get())),
                                   pressure=int(float(var_pressure.get())))
            state["commands"] = commands

            # Convert machine units back to REAL-WORLD inches for the mat preview.
            # NOTE: this must use the machine's true physical resolution
            # (MACHINE_UNITS_PER_INCH), NOT the SVG->machine scale factor
            # (units_per_inch / DEFAULT_UNITS_PER_INCH), which already has a
            # 72/96 dpi compensation baked in. Using the scale factor here was
            # the cause of the preview showing designs ~25% smaller than the
            # actual physical cut size (e.g. a 4" design previewing as 3").
            #
            # The offset (X=50, Y=550 by default) is calibration to this
            # machine/mat's true physical home position, not a design margin.
            # The preview subtracts the FIXED calibration reference
            # (DEFAULT_OFFSET_X/Y) -- not whatever is currently typed in the
            # offset fields -- so that changing the offset actually shifts
            # the design in the preview, and it only sits flush at the
            # top-left corner when the offset equals the calibrated default.
            subpaths_in = [[((x - DEFAULT_OFFSET_X) / MACHINE_UNITS_PER_INCH,
                             (y - DEFAULT_OFFSET_Y) / MACHINE_UNITS_PER_INCH)
                            for (x, y) in sp]
                           for sp in subpaths_m]
            draw_mat_grid()
            draw_design_preview(subpaths_in)

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
                try:
                    ack_n = int(var_ack_count.get())
                except ValueError:
                    ack_n = 0
                send_to_machine(port, state["commands"],
                                 ack_count=ack_n, log=log)
                if var_auto_unload.get():
                    log("Auto-unload enabled, sending load/unload toggle...")
                    time.sleep(1.0)
                    toggle_load_unload_mat(port, log=log)
            except Exception as e:
                log(f"ERROR: {e}")

        threading.Thread(target=worker, daemon=True).start()

    ttk.Button(frm_actions, text="1. Generate + Preview", command=generate).pack(
        side="left", padx=4)
    ttk.Button(frm_actions, text="2. Save to file", command=save_hpgl).pack(side="left", padx=4)
    ttk.Button(frm_actions, text="3. Send to Machine", style="Warn.TButton",
               command=send).pack(side="left", padx=4)

    log("Ready. This tool is built from a reverse-engineered protocol -- ")
    log("calibrate 'units per inch' with a known-size test cut before trusting it on real material.\n")

    root.mainloop()


# ----------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli()
    else:
        run_gui()
