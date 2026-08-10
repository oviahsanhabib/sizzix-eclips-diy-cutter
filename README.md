# eclips DIY Cutter

An unofficial SVG → HPGL sender for the Sizzix eclips cutting machine, built
from a reverse-engineered USB capture of eCAL. Includes a GUI with a mat
preview, and a CLI for scripting.

> ⚠️ **This protocol is reverse-engineered and unverified.** Test on scrap
> material first, at low speed, before trusting it on real material. You are
> responsible for what you send to your own hardware.

## Tested Hardware

- **Machine:** Sizzix eclips
- **Mat:** Cricut StandardGrip Adhesive Cutting Mat, 12" x 12"

The default calibration values in this script (`DEFAULT_UNITS_PER_INCH`,
`DEFAULT_OFFSET_X`/`DEFAULT_OFFSET_Y`) were tuned against this exact
combination. If you're using a different mat or machine, recalibrate with a
known-size test cut before trusting it on real material — see
[Calibration](#calibration) below.

## Setup (Windows)

1. Install Python 3 from [python.org](https://www.python.org/) if you don't
   have it (check **"Add to PATH"** during install).
2. Open Command Prompt in this folder and run:

   ```
   pip install pyserial svgelements
   ```

3. Run the tool:

   ```
   python eclips_cutter.py
   ```

## Usage

### GUI mode

Just run the script with no arguments:

```
python eclips_cutter.py
```

1. Click **Open SVG...** and choose your design — it auto-generates a
   preview on the mat.
2. Check **Calibration & Settings** (units per inch, speed, pressure,
   offsets) and the **Mat Preview** panel on the right to confirm the design
   size and position look correct before cutting.
3. Pick your **COM Port** under Machine Connection and refresh if needed.
4. Click **3. Send to Machine** to cut, or **2. Save to file** to export the
   generated HPGL without sending it.

### CLI mode

```
python eclips_cutter.py --svg design.svg --port COM3 --dry-run
python eclips_cutter.py --svg design.svg --port COM3 --send
```

Useful flags:

| Flag | Description |
|---|---|
| `--svg` | Path to the SVG file to cut |
| `--port` | COM port, e.g. `COM3` |
| `--units-per-inch` | SVG→machine scale factor (default `1365.33`, see Calibration below) |
| `--speed` / `--pressure` | Cut speed and blade pressure |
| `--offset-x` / `--offset-y` | Machine-unit offset applied to every point (default `50` / `550`) |
| `--flip-y` | Mirror the Y axis, if cuts come out upside-down |
| `--dry-run` | Print/save the generated HPGL without sending |
| `--send` | Actually send the job to the machine |
| `--save FILE` | Save generated HPGL to a file |
| `--list-ports` | List available COM ports |
| `--load-unload` | Toggle the mat load/unload sequence, then exit |
| `--laser-on` / `--laser-off` | Toggle the boundary-preview laser, then exit |
| `--auto-unload` | Send the load/unload toggle automatically after the job finishes |
| `--ack-count N` | Wait for a response after each of the first N commands (handshake only) |

## Calibration

Two constants at the top of the script matter most, and both were tuned
empirically against a real machine rather than derived from a spec sheet —
trust a test result over the theory if your own SVGs don't match:

- **`DEFAULT_UNITS_PER_INCH` (1365.33)** — the SVG→machine coordinate scale
  factor used when generating cut commands. Confirmed accurate against both
  4" and 6" test designs.
- **`MACHINE_UNITS_PER_INCH` (1024)** — the machine's true physical
  resolution, used only to convert machine units back into real-world inches
  for the on-screen mat preview.
- **`DEFAULT_OFFSET_X` / `DEFAULT_OFFSET_Y` (50 / 550)** — machine-unit
  offset that aligns the cut with your mat's true physical home position.
  This is mat/machine-specific calibration, not a simple inch-based margin.
  The mat preview treats these defaults as its zero point, so a design cut
  with the default offsets previews flush against the top-left corner,
  matching where it actually lands. Changing the offset fields shifts the
  design in the preview accordingly.

If your cuts come out the wrong size or in the wrong spot, recalibrate these
against your own machine with a simple known-size test shape before cutting
anything you care about.

## Command reference (confirmed from USB capture)

| Command | Meaning |
|---|---|
| `IN;` | Initialize |
| `SP1;` | Select pen/tool 1 |
| `PU x,y;` | Pen up, travel move to x,y |
| `PD x,y;` | Pen down, cutting move to x,y |
| `PG;` | End of page / finish job |
| `!ASP<a>,<b>;` | Speed/pressure setting, sent before moves |
| `!GBX;` / `!GBY;` | Query boundary X / Y |
| `!PON;` / `!POF;` | Enable / disable blade motor before cutting / travel moves |
| `!SBP x,y;` | Set beam position (laser pointer target) |
| `!LON;` / `!LOF;` | Laser on / off (boundary preview) |
| `!L;` | Load/unload mat toggle (starts motor motion) |
| `!MS;` | Sent ~11s after `!L;` — confirms/finalizes the load/unload motion |

Some details (Y-axis direction, ACK handshaking, multi-`!ASP` behavior) are
still best guesses — see the comments in `eclips_cutter.py` for specifics and
verify against your own USB captures if you run into issues.
