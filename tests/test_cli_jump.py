from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from breadboard.cli import _apply_jump_plan, _uart_name_rank, app

runner = CliRunner()


def test_uart_name_rank_prefers_unprefixed_names() -> None:
    assert _uart_name_rank("TX", "TX") == 0
    assert _uart_name_rank("TX_D1", "TX") == 1
    assert _uart_name_rank("UART_TX", "TX") == 3

    assert _uart_name_rank("RX", "RX") == 0
    assert _uart_name_rank("RX_D0", "RX") == 1
    assert _uart_name_rank("GPS_RX", "RX") == 3


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_jump_writes_overlay_and_connection_command_files(tmp_path: Path) -> None:
    library_path = _repo_root() / "Fritzing-Library"

    result = runner.invoke(
        app,
        [
            "jump",
            "Adafruit ItsyBitsy nRF52840",
            "Rotary Encoder with Knob",
            "--library-path",
            str(library_path),
            "--output-dir",
            str(tmp_path),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output

    overlays_path = tmp_path / "jump-overlays.pycmd"
    connections_path = tmp_path / "jump-connections.pycmd"

    assert overlays_path.exists()
    assert connections_path.exists()

    overlays_text = overlays_path.read_text(encoding="utf-8")
    connections_text = connections_path.read_text(encoding="utf-8")

    assert "> overlay_clear_all()" in overlays_text
    assert "overlay_set(" in overlays_text

    assert "> nodes_clear()" in connections_text
    assert "connect(" in connections_text


def test_jump_rotary_connections_use_unique_rows(tmp_path: Path) -> None:
    library_path = _repo_root() / "Fritzing-Library"

    result = runner.invoke(
        app,
        [
            "jump",
            "Adafruit ItsyBitsy nRF52840",
            "Rotary Encoder with Knob",
            "--library-path",
            str(library_path),
            "--output-dir",
            str(tmp_path),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output

    connections_path = tmp_path / "jump-connections.pycmd"
    commands = connections_path.read_text(encoding="utf-8")

    rows: list[int] = []
    for match in re.finditer(r"connect\((\d+),\s*(\d+)\)", commands):
        rows.append(int(match.group(1)))
        rows.append(int(match.group(2)))

    assert rows
    assert len(rows) == len(set(rows))


def test_jump_adds_uart_internal_connections_for_tx_rx_pins(tmp_path: Path) -> None:
    library_path = _repo_root() / "Fritzing-Library"

    result = runner.invoke(
        app,
        [
            "jump",
            "Adafruit ItsyBitsy nRF52840",
            "--library-path",
            str(library_path),
            "--output-dir",
            str(tmp_path),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output

    commands = (tmp_path / "jump-connections.pycmd").read_text(encoding="utf-8")

    assert re.search(r"connect\((\d+),\s*UART_RX\)", commands)
    assert re.search(r"connect\((\d+),\s*UART_TX\)", commands)


class _FakeSerial:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self._responses: list[bytes] = [b"ok\n"] * 32

    @property
    def in_waiting(self) -> int:
        if not self._responses:
            return 0
        return len(self._responses[0])

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    def read(self, size: int) -> bytes:
        if not self._responses:
            return b""
        return self._responses.pop(0)

    def flush(self) -> None:
        return None


def test_apply_jump_plan_uses_python_api_commands(capsys) -> None:
    serial_port = _FakeSerial()
    plan = {
        "overlays": [
            {
                "name": "part-0-1",
                "x": 1,
                "y": 1,
                "width": 2,
                "height": 2,
                "colors": [[0x112233, 0x445566], [0x778899, 0x000000]],
            }
        ],
        "connections": [{"from": 11, "to": 22}, {"from": 44, "to": "UART_RX"}],
    }

    _apply_jump_plan(serial_port, plan, include_connections=False, debug=True)
    _apply_jump_plan(serial_port, plan, include_connections=True, debug=True)

    output = capsys.readouterr().out
    assert "> overlay_clear_all()" in output
    assert "> overlay_set(" in output
    assert "> nodes_clear()" in output
    assert "> connect(11, 22)" in output
    assert "> connect(44, UART_RX)" in output

    serial_payload = b"".join(serial_port.writes)
    assert b"> overlay_clear_all()\n" in serial_payload
    assert b"> overlay_set(" in serial_payload
    assert b"> nodes_clear()\n" in serial_payload
    assert b"> connect(11, 22)\n" in serial_payload
    assert b"> connect(44, UART_RX)\n" in serial_payload
    assert b"L\n" not in serial_payload
