# breadboard

A CLI for exploring Fritzing parts and automating breadboard layouts.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

```bash
breadboard list
breadboard search "arduino"
```

Use `--library-path` if the `Fritzing-Library` checkout is not in the current
working directory.
