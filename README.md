# sizzix-eclips-diy-cutter
A free SVG-to-eclips sender, built by reverse-engineering a USB capture of
eCAL talking to your Sizzix eclips. The machine speaks HPGL (the same
open, standard plotter language used by Roland/Graphtec/HP devices), plus
a few Craft Edge vendor extension commands.
 
**This is unverified, reverse-engineered protocol. Test on scrap material
first.** You are working with your own hardware at your own risk.
 
## Setup (Windows)
 
1. Install Python 3 from python.org if you don't have it (check "Add to PATH" during install).
2. Open Command Prompt in this folder and run:
```
   pip install pyserial svgelements
```
3. Run the tool:
```
   python eclips_cutter.py
```
   This opens the GUI. Or use CLI mode — run `python eclips_cutter.py --help`.
 
## First-time calibration (important!)
 
The single biggest unknown is **how many machine units = 1 inch**. The
capture showed coordinates like `7723,2617` for what looked like a small
(roughly 1-2 inch) shape, so the tool defaults to a guess of **1000 units
per inch**. This WILL be wrong until you calibrate it:
 
1. In eCAL, cut a simple 2-inch square on scrap material, capturing the
   USB traffic with Wireshark+USBPcap while you do it (same way we captured
   the sample you sent me).
2. Look at the `PU`/`PD` coordinates in that capture. If the square is
   2 inches (measured edge to edge) and the coordinates span, say, 2000
   units, then you have 1000 units/inch. If they span 4000 units, you have
   2000 units/inch, etc.
3. Update the "Machine units per inch" field in the GUI (or `--units-per-inch`
   on the CLI) to match.
4. Also check whether your cut comes out mirrored or upside-down compared
   to your SVG — if so, enable "Flip Y axis."
## Usage
 
**GUI:**
1. "Open SVG..." — pick your design file.
2. Set units-per-inch (after calibration), speed, pressure, offsets.
3. Click "1. Generate HPGL" — review the commands in the log box.
4. Click "2. Save to file" if you just want the raw HPGL text (e.g. to
   compare byte-for-byte against a real eCAL capture for debugging).
5. Load material, select your COM port, click "3. Send to Machine" —
   you'll get a confirmation prompt before anything is actually sent.
**CLI:**
```
# List available COM ports
python eclips_cutter.py --list-ports
 
# Dry run — see the generated commands without sending
python eclips_cutter.py --svg design.svg --dry-run --units-per-inch 1000
 
# Save HPGL to a file for inspection
python eclips_cutter.py --svg design.svg --save design.hpgl --units-per-inch 1000
 
# Actually send to the machine (asks for confirmation)
python eclips_cutter.py --svg design.svg --send --port COM3 --units-per-inch 1000 --speed 80 --pressure 55
```
 
## Known limitations / things that still need verification
 
- **Coordinate scale (units/inch)** — needs your calibration, see above.
- **Y-axis direction** — SVG is Y-down; unclear yet if the machine matches
  or is mirrored. Toggle "Flip Y" if your first test cut comes out upside down.
- **`!ASP<a>,<b>;` meaning** — appears to be speed/pressure, sent multiple
  times with different values within a single job (possibly ramping speed
  at path start/end). This tool sends it once at the start; if cut quality
  is inconsistent on long curves, this is the first thing to investigate
  further with more captures.
- **`!GBX;` / `!GBY;`** — "get boundary" queries the machine sends; this
  tool doesn't query or use them (not required to send a job, based on
  the capture).
- **`!L;` / `!MS;`** — purpose still unclear; not sent by this tool. If jobs
  fail to start on the real machine, these might be required handshake
  steps — worth investigating with more captures (idle machine before a
  job, blade-change events, etc.)
- **No pen/tool switching, no scoring-only mode, no print-and-cut registration**
  — this only does basic single-pass vector cutting.
## If something doesn't work
 
Capture the traffic from a real eCAL job that's similar to what you're
trying to do (same rough shape complexity), and compare it line-by-line
against what this tool generates for the same SVG. Send me both and I can
help pinpoint the difference.
 
