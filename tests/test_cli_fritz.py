from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import re

import pytest
from typer.testing import CliRunner

from breadboard.cli import app

runner = CliRunner()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _find_by_id(root: ElementTree.Element, element_id: str) -> ElementTree.Element | None:
    for element in root.iter():
        if element.get("id") == element_id:
            return element
    return None


def test_fritz_renders_svg(tmp_path: Path) -> None:
    library_path = _repo_root() / "Fritzing-Library"
    output_path = tmp_path / "layout.svg"

    result = runner.invoke(
        app,
        [
            "fritz",
            "2.1mm DC Barrel Jack",
            "--library-path",
            str(library_path),
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert output_path.exists()

    tree = ElementTree.parse(output_path)
    root = tree.getroot()

    assert _find_by_id(root, "parts") is not None
    assert _find_by_id(root, "wires") is not None
    assert _find_by_id(root, "part-0") is not None


def _translate_from_transform(transform: str | None) -> tuple[float, float]:
    if not transform:
        return 0.0, 0.0
    match = re.search(r"translate\(([^)]+)\)", transform)
    if not match:
        return 0.0, 0.0
    parts = [part for part in match.group(1).replace(",", " ").split() if part]
    if not parts:
        return 0.0, 0.0
    x = float(parts[0])
    y = float(parts[1]) if len(parts) > 1 else 0.0
    return x, y


def test_fritz_matches_reference_positions(tmp_path: Path) -> None:
    library_path = _repo_root() / "Fritzing-Library"
    output_path = tmp_path / "layout.svg"
    expected_positions = {
        "part-group-0": (7.387749, 62.999874),
        "part-group-1": (238.499996, 62.999979),
    }

    result = runner.invoke(
        app,
        [
            "fritz",
            "Adafruit ItsyBitsy nRF52840",
            "Rotary Encoder with Knob",
            "--library-path",
            str(library_path),
            "--output",
            str(output_path),
            "--debug",
        ],
    )

    assert result.exit_code == 0, result.output

    rendered_root = ElementTree.parse(output_path).getroot()

    for element_id, (expected_x, expected_y) in expected_positions.items():
        rendered = _find_by_id(rendered_root, element_id)
        assert rendered is not None, f"Missing {element_id} in rendered SVG"
        rendered_x, rendered_y = _translate_from_transform(rendered.get("transform"))
        assert rendered_x == pytest.approx(expected_x, abs=1e-3)
        assert rendered_y == pytest.approx(expected_y, abs=1e-3)


def test_fritz_rejects_unknown_board_size(tmp_path: Path) -> None:
    library_path = _repo_root() / "Fritzing-Library"
    output_path = tmp_path / "layout.svg"

    result = runner.invoke(
        app,
        [
            "fritz",
            "2.1mm DC Barrel Jack",
            "--library-path",
            str(library_path),
            "--output",
            str(output_path),
            "--board-size",
            "giant",
        ],
    )

    assert result.exit_code == 2
    assert "Unknown board size" in result.output
