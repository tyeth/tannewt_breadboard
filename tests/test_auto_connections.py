from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from breadboard.cli import PartInfo, PartSvg, _auto_rotary_encoder_connections


def _make_part(
    title: str,
    connector_names: dict[str, str],
    connector_buses: dict[str, str] | None = None,
) -> PartSvg:
    connector_buses = connector_buses or {}
    connector_order = list(connector_names)
    connectors_by_id = {
        connector_id: (float(index), 0.0)
        for index, connector_id in enumerate(connector_order)
    }
    connectors = [connectors_by_id[connector_id] for connector_id in connector_order]
    return PartSvg(
        info=PartInfo(title=title, label="", path=Path("stub.fzpz")),
        svg_root=ElementTree.Element("svg"),
        width=10.0,
        height=10.0,
        drawing_bounds=None,
        connectors=connectors,
        connectors_by_id=connectors_by_id,
        connector_names=connector_names,
        connector_buses=connector_buses,
        connector_order=connector_order,
        size_unit=None,
        units_per_in=None,
    )


def test_rotary_encoder_auto_connections_use_unique_board_connectors() -> None:
    board = _make_part(
        "Adafruit Test Board",
        {
            "b_d2": "D2",
            "b_d3": "D3",
            "b_d4": "D4",
            "b_gnd1": "GND",
            "b_gnd2": "GND2",
        },
    )
    encoder = _make_part(
        "Rotary Encoder with Knob",
        {
            "e_a": "EncoderPinA",
            "e_b": "EncoderPinB",
            "e_c": "EncoderPinC",
            "e_sw1": "Switch1",
            "e_sw2": "Switch2",
        },
    )

    connections = _auto_rotary_encoder_connections([board, encoder])

    assert len(connections) == 5
    board_connector_ids = [connection[4] for connection in connections]
    assert len(set(board_connector_ids)) == len(board_connector_ids)


def test_rotary_encoder_auto_connections_do_not_share_single_ground_pin() -> None:
    board = _make_part(
        "Adafruit Test Board",
        {
            "b_d2": "D2",
            "b_d3": "D3",
            "b_d4": "D4",
            "b_gnd1": "GND",
        },
    )
    encoder = _make_part(
        "Rotary Encoder with Knob",
        {
            "e_a": "EncoderPinA",
            "e_b": "EncoderPinB",
            "e_c": "EncoderPinC",
            "e_sw1": "Switch1",
            "e_sw2": "Switch2",
        },
    )

    connections = _auto_rotary_encoder_connections([board, encoder])

    ground_connections = [connection for connection in connections if connection[5] == "GND"]
    assert len(ground_connections) == 1


def test_rotary_encoder_auto_connections_use_unique_itsybitsy_pins_across_encoders() -> None:
    board = _make_part(
        "Adafruit Test Board",
        {
            "b_d2": "D2",
            "b_d3": "D3",
            "b_d4": "D4",
            "b_d5": "D5",
            "b_d6": "D6",
            "b_d7": "D7",
            "b_gnd1": "GND",
            "b_gnd2": "GND2",
            "b_gnd3": "GND3",
            "b_gnd4": "GND4",
        },
    )
    encoder1 = _make_part(
        "Rotary Encoder with Knob",
        {
            "e1_a": "EncoderPinA",
            "e1_b": "EncoderPinB",
            "e1_c": "EncoderPinC",
            "e1_sw1": "Switch1",
            "e1_sw2": "Switch2",
        },
    )
    encoder2 = _make_part(
        "Rotary Encoder with Knob",
        {
            "e2_a": "EncoderPinA",
            "e2_b": "EncoderPinB",
            "e2_c": "EncoderPinC",
            "e2_sw1": "Switch1",
            "e2_sw2": "Switch2",
        },
    )

    connections = _auto_rotary_encoder_connections([board, encoder1, encoder2])

    board_connector_ids = [connection[4] for connection in connections]
    assert len(set(board_connector_ids)) == len(board_connector_ids)
