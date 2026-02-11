from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from breadboard.cli import (
    PartInfo,
    PartSvg,
    _default_connector_groups,
    _default_wires,
    _is_default_connectable,
    _normalize_connector_name,
)


def _make_part(
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
        info=PartInfo(title="stub", label="", path=Path("stub.fzpz")),
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


def test_default_connectable_voltage_rules() -> None:
    def norm(name: str) -> str:
        return _normalize_connector_name(name)

    assert _is_default_connectable(norm("GND"))
    assert _is_default_connectable(norm("ground"))
    assert _is_default_connectable(norm("3.3V"))
    assert _is_default_connectable(norm("+5V"))
    assert _is_default_connectable(norm("VDD_5V"))
    assert _is_default_connectable(norm("VIN_12V"))

    assert not _is_default_connectable(norm("VCC"))
    assert not _is_default_connectable(norm("VDD"))
    assert not _is_default_connectable(norm("VIN"))
    assert not _is_default_connectable(norm("VBAT"))
    assert not _is_default_connectable(norm("SCL_3V"))


def test_default_connector_groups_bus_without_power() -> None:
    part = _make_part(
        {"c1": "PAD1", "c2": "PAD2", "c3": "GND"},
        {"c1": "bus0", "c2": "bus0"},
    )

    groups = _default_connector_groups(part)

    assert "BUS:bus0" in groups
    assert set(groups["BUS:bus0"]) == {"c1", "c2"}
    assert "GND" in groups
    assert groups["GND"] == ["c3"]


def test_default_connector_groups_bus_prefers_power() -> None:
    part = _make_part(
        {"c1": "GND", "c2": "PAD2"},
        {"c1": "bus1", "c2": "bus1"},
    )

    groups = _default_connector_groups(part)

    assert "BUS:bus1" not in groups
    assert "GND" in groups
    assert set(groups["GND"]) == {"c1", "c2"}


def test_default_wires_respect_voltage_rules() -> None:
    part_vcc_a = _make_part({"c1": "VCC"})
    part_vcc_b = _make_part({"c1": "VCC"})
    assert _default_wires([part_vcc_a, part_vcc_b], [(0.0, 0.0), (10.0, 0.0)]) == []

    part_gnd_a = _make_part({"c1": "GND"})
    part_gnd_b = _make_part({"c1": "GND"})
    assert len(_default_wires([part_gnd_a, part_gnd_b], [(0.0, 0.0), (10.0, 0.0)])) == 1

    part_5v_a = _make_part({"c1": "VDD_5V"})
    part_5v_b = _make_part({"c1": "VDD_5V"})
    assert len(_default_wires([part_5v_a, part_5v_b], [(0.0, 0.0), (10.0, 0.0)])) == 1
