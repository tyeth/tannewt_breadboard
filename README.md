# breadboard

`breadboard` is a command-line tool that helps you:

- browse Fritzing parts
- inspect part metadata and SVG views
- auto-place parts on common breadboard sizes
- auto-generate likely wiring between parts
- generate Jumperless overlay + connection commands

This repo already includes a `Fritzing-Library` folder, so you can get started quickly.

## 1) Install (beginner-friendly)

### Requirements

- Python 3.10+
- Cairo
- A terminal (PowerShell on Windows, Terminal on macOS/Linux)

### From the project folder

```bash
python -m venv .venv
```

Activate the environment:

- Windows PowerShell:

	```powershell
	.\.venv\Scripts\Activate.ps1
	```

- macOS/Linux:

	```bash
	source .venv/bin/activate
	```

Then install the project:

```bash
pip install -e .
```

## 2) First success in 60 seconds

```bash
breadboard list
breadboard search "itsybitsy"
breadboard info "Adafruit ItsyBitsy nRF52840"
```

If your Fritzing library is somewhere else, add:

```bash
--library-path /path/to/Fritzing-Library
```

## 3) Command overview

### Part browsing and inspection

- `breadboard list`
	- Lists all parts in a table.
- `breadboard search "text"`
	- Searches title, label, and filename.
- `breadboard info "part name"`
	- Shows metadata, properties, views, and connector bus groups.
- `breadboard summarize`
	- Summarizes SVG scale/units across the library.

### Layout rendering

- `breadboard fritz PART [PART ...]`
	- Places selected parts on a breadboard and renders a layout.
	- By default, it displays in-terminal graphics.
	- Use `--output layout.svg` to write SVG to disk.

### Jumperless upload planning

- `breadboard jump PART [PART ...]`
	- Builds overlay and connection plans for Jumperless.
	- Writes command logs to:
		- `jump-overlays.pycmd`
		- `jump-connections.pycmd`
	- With `--dry-run`, it does not upload to hardware.

### Built-in help

- `breadboard --help`
- `breadboard help`
- `breadboard <command> --help`

## 4) Most useful options

Common options across commands:

- `--library-path, -l` path to Fritzing-Library
- `--board-size, -b` one of: `half` (default), `full`, `quarter`, `mint`
- `--debug` shows extra diagnostics

Layout options:

- `--output, -o` write SVG file (for `fritz`)
- `--scale` terminal render scale (when no output file)

Jumperless options:

- `--output-dir` where `.pycmd` command files are written
- `--tiny-bmps-dir` location of tiny BMP overlays (default: `tiny-bmps`)
- `--port` serial port (auto-detect when omitted)
- `--baud` serial baud rate (default: `115200`)
- `--dry-run` build plans but skip upload

## 5) Practical workflows

### A) Find a part and inspect it

```bash
breadboard search "rotary encoder"
breadboard info "Rotary Encoder with Knob"
```

### B) Generate a breadboard SVG layout

```bash
breadboard fritz "Adafruit ItsyBitsy nRF52840" "Rotary Encoder with Knob" --output layout.svg
```

Try different board sizes:

```bash
breadboard fritz "Adafruit ItsyBitsy nRF52840" "Rotary Encoder with Knob" -b full -o layout-full.svg
```

### C) Prepare Jumperless commands without uploading

```bash
breadboard jump "Adafruit ItsyBitsy nRF52840" "Rotary Encoder with Knob" --dry-run --output-dir out
```

### D) Upload to Jumperless interactively

```bash
breadboard jump "Adafruit ItsyBitsy nRF52840" "Rotary Encoder with Knob"
```

You will be prompted to:

1. load overlays,
2. place physical parts,
3. then load electrical connections.

## 6) Tiny BMP overlays (for Jumperless)

The `jump` command expects tiny BMP overlays in `tiny-bmps/`.

Generate them from your Fritzing parts:

```bash
python -m breadboard.generate_tiny_bmps generate
```

Useful options:

```bash
python -m breadboard.generate_tiny_bmps generate --pixels-per-inch 10 --output-dir tiny-bmps
python -m breadboard.generate_tiny_bmps generate --limit 50
```

## 7) What is auto-connected today?

- Default net wiring is inferred for common power names (for example `GND`, `3.3V`, `5V`, `VIN_12V`).
- Rotary encoders are auto-wired to detected Adafruit board pins where possible.
- Jumperless plans include row-to-row connections and UART TX/RX links when detected.

These are heuristics and may not match every design exactly, so always review output.

### Where this is implemented

- **Default net wiring (power/ground name heuristics + wire generation)**
	- Main code: [src/breadboard/cli.py](src/breadboard/cli.py)
	- Key symbols: `DEFAULT_GROUND_NAMES_RAW`, `DEFAULT_VOLTAGE_NAMES`, `POWER_PREFIXES`, `_normalize_connector_name`, `_is_default_connectable`, `_default_connector_groups`, `_default_wires`
	- Tests: [tests/test_default_connections.py](tests/test_default_connections.py)

- **Rotary encoder auto-wiring to Adafruit boards**
	- Main code: [src/breadboard/cli.py](src/breadboard/cli.py)
	- Key symbols: `_auto_rotary_encoder_connections`, `_board_pin_lookup`, `_board_ground_ids`, `_pick_board_pin`, `_render_auto_connections`, `_auto_connection_wires`
	- Tests: [tests/test_auto_connections.py](tests/test_auto_connections.py)

- **Jumperless connection plan generation (including UART TX/RX links)**
	- Main code: [src/breadboard/cli.py](src/breadboard/cli.py)
	- Key symbols: `_jump_connections`, `_jump_uart_connections`, `_build_jump_plan`, `_connection_commands`, `_apply_jump_plan`
	- Tests: [tests/test_cli_jump.py](tests/test_cli_jump.py)

## 8) Troubleshooting

- **"Fritzing-Library not found"**
	- Run from this repo root or pass `--library-path`.
- **"Multiple matches for ..."**
	- Use a more specific part name.
- **Layout exceeds board height**
	- Use fewer/larger board size (`-b full`), or fewer tall parts.
- **No serial ports found / upload issues**
	- Use `--dry-run` first, then pass `--port` explicitly.

## 9) Development quick notes

Run tests:

```bash
pytest
```

Main code lives in `src/breadboard/cli.py`.
