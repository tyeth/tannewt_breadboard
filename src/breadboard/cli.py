from __future__ import annotations

import io
import math
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Automate breadboard layouts using Fritzing parts.")
console = Console()

LIBRARY_PATH_OPTION = typer.Option(
    None, "--library-path", "-l", help="Path to the Fritzing-Library checkout."
)
PARTS_ARGUMENT = typer.Argument(..., help="Parts to place on the breadboard.")
BOARD_SIZE_OPTION = typer.Option(
    "half",
    "--board-size",
    "-b",
    help="Breadboard size preset (half, full, quarter, mint).",
)
OUTPUT_OPTION = typer.Option(
    None,
    "--output",
    "-o",
    help="Where to write the rendered SVG (omit to display in the terminal).",
)
SCALE_OPTION = typer.Option(
    1.0,
    "--scale",
    help="Scale factor for terminal rasterization when no output file is given.",
)
DEBUG_OPTION = typer.Option(
    False,
    "--debug",
    help="Include debug overlays like part courtyards in the output.",
)

BOARD_PART_FILES: dict[str, list[str]] = {
    "half": ["PermaprotoHalfBoard.fzpz"],
    "full": ["PermaProtoFullsizedBoard.fzpz"],
    "quarter": ["PermaprotoQuarterBoard.fzpz", "permaproto_quarter_board.fzpz"],
    "mint": ["Adafruit PermaProto Mint Tin Size Breadboard.fzpz"],
}


@dataclass(frozen=True)
class PartInfo:
    title: str
    label: str
    path: Path


@dataclass
class PartSvg:
    info: PartInfo
    svg_root: ElementTree.Element
    width: float
    height: float
    drawing_bounds: tuple[float, float, float, float] | None
    connectors: list[tuple[float, float]]
    connectors_by_id: dict[str, tuple[float, float]]
    connector_buses: dict[str, str]
    connector_order: list[str]
    size_unit: str | None
    units_per_in: float | None


def _resolve_library_path(library_path: Path | None) -> Path:
    if library_path:
        if library_path.exists():
            return library_path
        raise typer.BadParameter(f"Fritzing-Library not found at {library_path}.")

    cwd_candidate = Path.cwd() / "Fritzing-Library"
    if cwd_candidate.exists():
        return cwd_candidate

    site_root = Path(__file__).resolve().parents[1]
    packaged_candidate = site_root / "Fritzing-Library"
    if packaged_candidate.exists():
        return packaged_candidate

    raise typer.BadParameter(
        "Fritzing-Library not found in the current directory or installed package. "
        "Pass --library-path."
    )


def _parts_root(library_path: Path) -> Path:
    parts_path = library_path / "parts"
    return parts_path if parts_path.exists() else library_path


def _iter_part_files(library_path: Path) -> Iterable[Path]:
    yield from sorted(_parts_root(library_path).rglob("*.fzpz"))


def _read_part_info(part_path: Path) -> PartInfo:
    with zipfile.ZipFile(part_path) as archive:
        fzp_members = [name for name in archive.namelist() if name.endswith(".fzp")]
        if not fzp_members:
            title = part_path.stem
            return PartInfo(title=title, label="", path=part_path)
        xml_data = archive.read(fzp_members[0])
    root = ElementTree.fromstring(xml_data)
    title = root.findtext("title") or part_path.stem
    label = root.findtext("label") or ""
    return PartInfo(title=title.strip(), label=label.strip(), path=part_path)


def _load_part_fzp(part: PartInfo) -> ElementTree.Element:
    with zipfile.ZipFile(part.path) as archive:
        fzp_name = next((name for name in archive.namelist() if name.endswith(".fzp")), None)
        if not fzp_name:
            raise ValueError(f"Missing .fzp metadata in {part.path}.")
        fzp_data = archive.read(fzp_name)
    return ElementTree.fromstring(fzp_data)


def _view_image_entries(fzp_root: ElementTree.Element) -> list[tuple[str, str | None]]:
    entries: list[tuple[str, str | None]] = []
    for view in fzp_root.findall("./views/*"):
        layers = view.find("layers")
        image = layers.get("image") if layers is not None else None
        entries.append((view.tag, image))
    return entries


def _load_parts(library_path: Path) -> list[PartInfo]:
    parts = []
    for part_file in _iter_part_files(library_path):
        try:
            parts.append(_read_part_info(part_file))
        except (ElementTree.ParseError, zipfile.BadZipFile):
            parts.append(PartInfo(title=part_file.stem, label="", path=part_file))
    return parts


def _render_parts(parts: Sequence[PartInfo], library_path: Path) -> None:
    if not parts:
        console.print("No parts found.")
        return

    table = Table(title=f"Fritzing parts ({len(parts)})")
    table.add_column("Title", style="bold")
    table.add_column("Label")
    table.add_column("File")

    for part in parts:
        relative_path = str(part.path.relative_to(_parts_root(library_path)))
        table.add_row(part.title, part.label, relative_path)

    console.print(table)


def _filter_parts(parts: Sequence[PartInfo], query: str) -> list[PartInfo]:
    query_lower = query.lower()
    return [
        part
        for part in parts
        if query_lower in part.title.lower()
        or query_lower in part.label.lower()
        or query_lower in part.path.stem.lower()
    ]


def _resolve_part(parts: Sequence[PartInfo], query: str) -> PartInfo | None:
    query_lower = query.lower()
    exact_matches = [
        part
        for part in parts
        if query_lower
        in {
            part.title.lower(),
            part.label.lower(),
            part.path.stem.lower(),
            part.path.name.lower(),
        }
    ]
    matches = exact_matches or _filter_parts(parts, query)
    if not matches:
        console.print(f"No matches for '{query}'.")
        return None
    if len(matches) > 1:
        console.print(f"Multiple matches for '{query}':")
        for part in matches[:10]:
            console.print(f"- {part.title} ({part.path.name})")
        if len(matches) > 10:
            console.print(f"...and {len(matches) - 10} more")
        return None
    return matches[0]


def _parse_svg_size(value: str | None) -> float | None:
    if not value:
        return None
    match = re.match(r"^([+-]?[0-9]*\.?[0-9]+)([a-z%]*)$", value.strip())
    if not match:
        return None
    magnitude = float(match.group(1))
    unit = match.group(2)
    if not unit or unit == "px":
        return magnitude
    if unit == "in":
        return magnitude * 96.0
    if unit == "mm":
        return magnitude * 96.0 / 25.4
    if unit == "cm":
        return magnitude * 96.0 / 2.54
    if unit == "pt":
        return magnitude * 96.0 / 72.0
    if unit == "pc":
        return magnitude * 96.0 / 6.0
    return None


def _parse_svg_length(value: str | None) -> tuple[float, str] | None:
    if not value:
        return None
    match = re.match(r"^([+-]?[0-9]*\.?[0-9]+)([a-z%]*)$", value.strip())
    if not match:
        return None
    magnitude = float(match.group(1))
    unit = match.group(2) or "px"
    return magnitude, unit


def _svg_primary_unit(svg_root: ElementTree.Element) -> str | None:
    length = _parse_svg_length(svg_root.get("width")) or _parse_svg_length(
        svg_root.get("height")
    )
    if not length:
        return None
    unit = length[1] or "px"
    return unit


def _svg_viewbox(svg_root: ElementTree.Element) -> tuple[float, float, float, float] | None:
    view_box = svg_root.get("viewBox")
    if not view_box:
        return None
    parts = [float(part) for part in view_box.replace(",", " ").split()]
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _svg_dimensions(svg_root: ElementTree.Element) -> tuple[float, float]:
    width = _parse_svg_size(svg_root.get("width"))
    height = _parse_svg_size(svg_root.get("height"))
    if width is not None and height is not None:
        return width, height
    view_box = _svg_viewbox(svg_root)
    if view_box:
        return view_box[2], view_box[3]
    raise ValueError("SVG is missing width/height information.")


def _svg_drawing_bounds(
    svg_root: ElementTree.Element, width: float, height: float
) -> tuple[float, float, float, float] | None:
    if width <= 0 or height <= 0:
        return None
    parents = _parent_map(svg_root)
    min_x: float | None = None
    min_y: float | None = None
    max_x: float | None = None
    max_y: float | None = None
    for element in svg_root.iter():
        if _skip_bounds_element(element, parents):
            continue
        points = _element_local_points(element)
        if not points:
            continue
        matrix = _accumulated_transform(element, parents)
        for point in points:
            transformed = _apply_matrix(matrix, point)
            if min_x is None:
                min_x = max_x = transformed[0]
                min_y = max_y = transformed[1]
            else:
                min_x = min(min_x, transformed[0])
                min_y = min(min_y, transformed[1])
                max_x = max(max_x, transformed[0])
                max_y = max(max_y, transformed[1])
    if min_x is None or min_y is None or max_x is None or max_y is None:
        return None
    viewbox = _svg_viewbox(svg_root)
    if viewbox:
        min_point = _map_viewbox_point((min_x, min_y), viewbox, (width, height))
        max_point = _map_viewbox_point((max_x, max_y), viewbox, (width, height))
        min_x, min_y = min(min_point[0], max_point[0]), min(min_point[1], max_point[1])
        max_x, max_y = max(min_point[0], max_point[0]), max(min_point[1], max_point[1])
    return min_x, min_y, max_x, max_y


def _strip_svg_tag(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _skip_bounds_element(
    element: ElementTree.Element,
    parents: dict[ElementTree.Element, ElementTree.Element],
) -> bool:
    skipped_tags = {"defs", "metadata", "title", "desc", "style"}
    current: ElementTree.Element | None = element
    while current is not None:
        tag = _strip_svg_tag(current.tag)
        if tag in skipped_tags:
            return True
        if current.get("display") == "none" or current.get("visibility") == "hidden":
            return True
        current = parents.get(current)
    return False


def _parse_points(points_value: str) -> list[tuple[float, float]]:
    values = [
        float(value)
        for value in re.findall(
            r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", points_value
        )
    ]
    return [
        (values[index], values[index + 1])
        for index in range(0, len(values) - 1, 2)
    ]


def _element_local_points(element: ElementTree.Element) -> list[tuple[float, float]] | None:
    tag = _strip_svg_tag(element.tag)
    if tag == "rect":
        x = _parse_svg_size(element.get("x")) or 0.0
        y = _parse_svg_size(element.get("y")) or 0.0
        width = _parse_svg_size(element.get("width"))
        height = _parse_svg_size(element.get("height"))
        if width is None or height is None:
            return None
        return [
            (x, y),
            (x + width, y),
            (x, y + height),
            (x + width, y + height),
        ]
    if tag == "circle":
        cx = _parse_svg_size(element.get("cx")) or 0.0
        cy = _parse_svg_size(element.get("cy")) or 0.0
        radius = _parse_svg_size(element.get("r")) or 0.0
        return [
            (cx - radius, cy - radius),
            (cx + radius, cy - radius),
            (cx - radius, cy + radius),
            (cx + radius, cy + radius),
        ]
    if tag == "ellipse":
        cx = _parse_svg_size(element.get("cx")) or 0.0
        cy = _parse_svg_size(element.get("cy")) or 0.0
        rx = _parse_svg_size(element.get("rx")) or 0.0
        ry = _parse_svg_size(element.get("ry")) or 0.0
        return [
            (cx - rx, cy - ry),
            (cx + rx, cy - ry),
            (cx - rx, cy + ry),
            (cx + rx, cy + ry),
        ]
    if tag == "line":
        x1 = _parse_svg_size(element.get("x1")) or 0.0
        y1 = _parse_svg_size(element.get("y1")) or 0.0
        x2 = _parse_svg_size(element.get("x2")) or 0.0
        y2 = _parse_svg_size(element.get("y2")) or 0.0
        return [(x1, y1), (x2, y2)]
    if tag in {"polyline", "polygon"}:
        points_value = element.get("points")
        if not points_value:
            return None
        return _parse_points(points_value)
    if tag == "path":
        path_data = element.get("d")
        if not path_data:
            return None
        return _path_points(path_data)
    if tag == "image":
        x = _parse_svg_size(element.get("x")) or 0.0
        y = _parse_svg_size(element.get("y")) or 0.0
        width = _parse_svg_size(element.get("width"))
        height = _parse_svg_size(element.get("height"))
        if width is None or height is None:
            return None
        return [
            (x, y),
            (x + width, y),
            (x, y + height),
            (x + width, y + height),
        ]
    if tag == "text":
        x = _parse_svg_size(element.get("x"))
        y = _parse_svg_size(element.get("y"))
        if x is None or y is None:
            return None
        return [(x, y)]
    if tag == "use":
        x = _parse_svg_size(element.get("x"))
        y = _parse_svg_size(element.get("y"))
        if x is None or y is None:
            return None
        return [(x, y)]
    return None


def _is_command_token(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z]$", token))


def _path_points(path_data: str) -> list[tuple[float, float]]:
    tokens = re.findall(
        r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?",
        path_data,
    )
    points: list[tuple[float, float]] = []
    index = 0
    command: str | None = None
    current_x = 0.0
    current_y = 0.0
    start_x = 0.0
    start_y = 0.0

    def read_numbers() -> list[float]:
        nonlocal index
        numbers: list[float] = []
        while index < len(tokens) and not _is_command_token(tokens[index]):
            numbers.append(float(tokens[index]))
            index += 1
        return numbers

    while index < len(tokens):
        token = tokens[index]
        if _is_command_token(token):
            command = token
            index += 1
        if command is None:
            break
        cmd_upper = command.upper()
        relative = command.islower()
        if cmd_upper == "Z":
            current_x, current_y = start_x, start_y
            continue
        numbers = read_numbers()
        if cmd_upper == "M":
            for idx in range(0, len(numbers) - 1, 2):
                x = numbers[idx]
                y = numbers[idx + 1]
                if relative:
                    x += current_x
                    y += current_y
                current_x, current_y = x, y
                if idx == 0:
                    start_x, start_y = current_x, current_y
                points.append((current_x, current_y))
            continue
        if cmd_upper in {"L", "T"}:
            for idx in range(0, len(numbers) - 1, 2):
                x = numbers[idx]
                y = numbers[idx + 1]
                if relative:
                    x += current_x
                    y += current_y
                current_x, current_y = x, y
                points.append((current_x, current_y))
            continue
        if cmd_upper == "H":
            for value in numbers:
                x = value + current_x if relative else value
                current_x = x
                points.append((current_x, current_y))
            continue
        if cmd_upper == "V":
            for value in numbers:
                y = value + current_y if relative else value
                current_y = y
                points.append((current_x, current_y))
            continue
        if cmd_upper == "C":
            for idx in range(0, len(numbers) - 5, 6):
                x1, y1, x2, y2, x, y = numbers[idx:idx + 6]
                if relative:
                    x1 += current_x
                    y1 += current_y
                    x2 += current_x
                    y2 += current_y
                    x += current_x
                    y += current_y
                points.extend([(x1, y1), (x2, y2), (x, y)])
                current_x, current_y = x, y
            continue
        if cmd_upper == "S":
            for idx in range(0, len(numbers) - 3, 4):
                x2, y2, x, y = numbers[idx:idx + 4]
                if relative:
                    x2 += current_x
                    y2 += current_y
                    x += current_x
                    y += current_y
                points.extend([(x2, y2), (x, y)])
                current_x, current_y = x, y
            continue
        if cmd_upper == "Q":
            for idx in range(0, len(numbers) - 3, 4):
                x1, y1, x, y = numbers[idx:idx + 4]
                if relative:
                    x1 += current_x
                    y1 += current_y
                    x += current_x
                    y += current_y
                points.extend([(x1, y1), (x, y)])
                current_x, current_y = x, y
            continue
        if cmd_upper == "A":
            for idx in range(0, len(numbers) - 6, 7):
                rx, ry, _rotation, _large, _sweep, x, y = numbers[idx:idx + 7]
                if relative:
                    x += current_x
                    y += current_y
                rx = abs(rx)
                ry = abs(ry)
                points.extend(
                    [
                        (current_x - rx, current_y - ry),
                        (current_x + rx, current_y + ry),
                        (x - rx, y - ry),
                        (x + rx, y + ry),
                        (x, y),
                    ]
                )
                current_x, current_y = x, y
            continue
    return points


def _svg_units_per_in(svg_root: ElementTree.Element) -> float | None:
    view_box = _svg_viewbox(svg_root)
    if not view_box:
        return 90.0
    width_px = _parse_svg_size(svg_root.get("width"))
    height_px = _parse_svg_size(svg_root.get("height"))
    candidates: list[float] = []
    if width_px and width_px > 0:
        width_in = width_px / 96.0
        candidates.append(view_box[2] / width_in)
    if height_px and height_px > 0:
        height_in = height_px / 96.0
        candidates.append(view_box[3] / height_in)
    if not candidates:
        return None
    return sum(candidates) / len(candidates)


def _svg_scale_bucket(svg_root: ElementTree.Element) -> tuple[str, str | None]:
    view_box = _svg_viewbox(svg_root)
    units_per_in = _svg_units_per_in(svg_root)
    length = _parse_svg_length(svg_root.get("width")) or _parse_svg_length(svg_root.get("height"))
    unit = length[1] if length else None
    if units_per_in is not None:
        return f"{units_per_in:.1f} units/in", unit
    if view_box is None:
        return "missing viewBox", unit
    return "missing width/height", unit


def _resolve_svg_entry(archive: zipfile.ZipFile, image_path: str) -> str:
    candidate = f"svg.{image_path.replace('/', '.')}"
    if candidate in archive.namelist():
        return candidate
    if image_path in archive.namelist():
        return image_path
    base_name = Path(image_path).name
    for name in archive.namelist():
        if name.endswith(base_name):
            return name
    raise FileNotFoundError(f"Unable to locate SVG asset for {image_path}.")


def _element_center(element: ElementTree.Element) -> tuple[float, float] | None:
    tag_name = element.tag.split("}")[-1]
    if tag_name in {"circle", "ellipse"}:
        cx = element.get("cx")
        cy = element.get("cy")
        if cx and cy:
            return float(cx), float(cy)
    if tag_name == "rect":
        x = float(element.get("x", "0"))
        y = float(element.get("y", "0"))
        width = _parse_svg_size(element.get("width")) or 0.0
        height = _parse_svg_size(element.get("height")) or 0.0
        return x + width / 2, y + height / 2
    return None


def _identity_matrix() -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _matrix_multiply(
    left: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    right: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    return (
        (
            left[0][0] * right[0][0] + left[0][1] * right[1][0] + left[0][2] * right[2][0],
            left[0][0] * right[0][1] + left[0][1] * right[1][1] + left[0][2] * right[2][1],
            left[0][0] * right[0][2] + left[0][1] * right[1][2] + left[0][2] * right[2][2],
        ),
        (
            left[1][0] * right[0][0] + left[1][1] * right[1][0] + left[1][2] * right[2][0],
            left[1][0] * right[0][1] + left[1][1] * right[1][1] + left[1][2] * right[2][1],
            left[1][0] * right[0][2] + left[1][1] * right[1][2] + left[1][2] * right[2][2],
        ),
        (
            left[2][0] * right[0][0] + left[2][1] * right[1][0] + left[2][2] * right[2][0],
            left[2][0] * right[0][1] + left[2][1] * right[1][1] + left[2][2] * right[2][1],
            left[2][0] * right[0][2] + left[2][1] * right[1][2] + left[2][2] * right[2][2],
        ),
    )


def _apply_matrix(
    matrix: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]],
    point: tuple[float, float],
) -> tuple[float, float]:
    x, y = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2],
    )


def _transform_matrix(transform: str | None) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    if not transform:
        return _identity_matrix()
    matrix = _identity_matrix()
    for func, params in re.findall(r"(\w+)\(([^)]*)\)", transform):
        values = [float(value) for value in re.split(r"[ ,]+", params.strip()) if value]
        func = func.lower()
        if func == "translate":
            tx = values[0] if values else 0.0
            ty = values[1] if len(values) > 1 else 0.0
            step = ((1.0, 0.0, tx), (0.0, 1.0, ty), (0.0, 0.0, 1.0))
        elif func == "scale":
            sx = values[0] if values else 1.0
            sy = values[1] if len(values) > 1 else sx
            step = ((sx, 0.0, 0.0), (0.0, sy, 0.0), (0.0, 0.0, 1.0))
        elif func == "matrix" and len(values) == 6:
            a, b, c, d, e, f = values
            step = ((a, c, e), (b, d, f), (0.0, 0.0, 1.0))
        elif func == "rotate" and values:
            angle = values[0]
            radians = angle * 3.141592653589793 / 180.0
            cos_a = float(math.cos(radians))
            sin_a = float(math.sin(radians))
            rotation = ((cos_a, -sin_a, 0.0), (sin_a, cos_a, 0.0), (0.0, 0.0, 1.0))
            if len(values) == 3:
                cx, cy = values[1], values[2]
                translate_to = ((1.0, 0.0, cx), (0.0, 1.0, cy), (0.0, 0.0, 1.0))
                translate_back = ((1.0, 0.0, -cx), (0.0, 1.0, -cy), (0.0, 0.0, 1.0))
                step = _matrix_multiply(translate_to, _matrix_multiply(rotation, translate_back))
            else:
                step = rotation
        else:
            continue
        matrix = _matrix_multiply(step, matrix)
    return matrix


def _parent_map(svg_root: ElementTree.Element) -> dict[ElementTree.Element, ElementTree.Element]:
    parents: dict[ElementTree.Element, ElementTree.Element] = {}
    for parent in svg_root.iter():
        for child in list(parent):
            parents[child] = parent
    return parents


def _resolve_center_element(
    element: ElementTree.Element,
) -> tuple[ElementTree.Element, tuple[float, float]] | None:
    for candidate in element.iter():
        center = _element_center(candidate)
        if center:
            return candidate, center
    return None


def _accumulated_transform(
    element: ElementTree.Element,
    parents: dict[ElementTree.Element, ElementTree.Element],
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    matrix = _identity_matrix()
    current: ElementTree.Element | None = element
    while current is not None:
        matrix = _matrix_multiply(_transform_matrix(current.get("transform")), matrix)
        current = parents.get(current)
    return matrix


def _find_connector_points(
    svg_root: ElementTree.Element, connector_svg_ids: Mapping[str, str]
) -> dict[str, tuple[float, float]]:
    elements_by_id = {
        element.get("id"): element for element in svg_root.iter() if element.get("id")
    }
    parents = _parent_map(svg_root)
    points: dict[str, tuple[float, float]] = {}
    for connector_id, svg_id in connector_svg_ids.items():
        element = elements_by_id.get(svg_id)
        if element is None:
            continue
        resolved = _resolve_center_element(element)
        if not resolved:
            continue
        center_element, center = resolved
        matrix = _accumulated_transform(center_element, parents)
        points[connector_id] = _apply_matrix(matrix, center)
    return points


def _map_viewbox_point(
    point: tuple[float, float],
    viewbox: tuple[float, float, float, float],
    display_size: tuple[float, float],
) -> tuple[float, float]:
    min_x, min_y, view_width, view_height = viewbox
    display_width, display_height = display_size
    scale_x = display_width / view_width
    scale_y = display_height / view_height
    return (point[0] - min_x) * scale_x, (point[1] - min_y) * scale_y


def _connector_svg_ids(fzp_root: ElementTree.Element) -> dict[str, str]:
    connector_svg_ids: dict[str, str] = {}
    for connector in fzp_root.findall(".//connector"):
        connector_id = connector.get("id")
        if not connector_id:
            continue
        point = connector.find("./views/breadboardView/p")
        if point is None:
            point = connector.find(".//breadboardView/p")
        if point is None:
            continue
        svg_id = point.get("svgId")
        if svg_id:
            connector_svg_ids[connector_id] = svg_id
    return connector_svg_ids


def _connector_bus_membership(fzp_root: ElementTree.Element) -> dict[str, str]:
    membership: dict[str, str] = {}
    for bus in fzp_root.findall(".//buses/bus"):
        bus_id = bus.get("id")
        if not bus_id:
            continue
        for node in bus.findall("nodeMember"):
            connector_id = node.get("connectorId")
            if connector_id:
                membership[connector_id] = bus_id
    return membership


def _load_part_breadboard_svg(part: PartInfo) -> ElementTree.Element:
    with zipfile.ZipFile(part.path) as archive:
        fzp_name = next((name for name in archive.namelist() if name.endswith(".fzp")), None)
        if not fzp_name:
            raise ValueError(f"Missing .fzp metadata in {part.path}.")
        fzp_data = archive.read(fzp_name)
        fzp_root = ElementTree.fromstring(fzp_data)
        breadboard_layers = fzp_root.find(".//breadboardView/layers")
        if breadboard_layers is None or not breadboard_layers.get("image"):
            raise ValueError(f"Missing breadboard view in {part.path}.")
        image_path = breadboard_layers.get("image")
        svg_entry = _resolve_svg_entry(archive, image_path)
        svg_data = archive.read(svg_entry)
    return ElementTree.fromstring(svg_data)


def _load_part_svg(part: PartInfo) -> PartSvg:
    with zipfile.ZipFile(part.path) as archive:
        fzp_name = next((name for name in archive.namelist() if name.endswith(".fzp")), None)
        if not fzp_name:
            raise ValueError(f"Missing .fzp metadata in {part.path}.")
        fzp_data = archive.read(fzp_name)
        fzp_root = ElementTree.fromstring(fzp_data)
        breadboard_layers = fzp_root.find(".//breadboardView/layers")
        if breadboard_layers is None or not breadboard_layers.get("image"):
            raise ValueError(f"Missing breadboard view in {part.path}.")
        image_path = breadboard_layers.get("image")
        svg_entry = _resolve_svg_entry(archive, image_path)
        svg_data = archive.read(svg_entry)
        connector_svg_ids = _connector_svg_ids(fzp_root)
        connector_buses = _connector_bus_membership(fzp_root)
    svg_root = ElementTree.fromstring(svg_data)
    width, height = _svg_dimensions(svg_root)
    connectors_by_id = _find_connector_points(svg_root, connector_svg_ids)
    viewbox = _svg_viewbox(svg_root)
    if viewbox:
        connectors_by_id = {
            connector_id: _map_viewbox_point(point, viewbox, (width, height))
            for connector_id, point in connectors_by_id.items()
        }
    connector_order = [
        connector_id
        for connector_id in connector_svg_ids
        if connector_id in connectors_by_id
    ]
    connectors = [
        connectors_by_id[connector_id]
        for connector_id in connector_order
    ]
    drawing_bounds = _svg_drawing_bounds(svg_root, width, height)
    return PartSvg(
        info=part,
        svg_root=svg_root,
        width=width,
        height=height,
        drawing_bounds=drawing_bounds,
        connectors=connectors,
        connectors_by_id=connectors_by_id,
        connector_buses=connector_buses,
        connector_order=connector_order,
        size_unit=_svg_primary_unit(svg_root),
        units_per_in=_svg_units_per_in(svg_root),
    )


def _svg_namespace(svg_root: ElementTree.Element) -> str | None:
    if svg_root.tag.startswith("{"):
        return svg_root.tag.split("}")[0].strip("{")
    return None


def _svg_tag(namespace: str | None, tag: str) -> str:
    return f"{{{namespace}}}{tag}" if namespace else tag


def _local_anchor(part: PartSvg) -> tuple[float, float]:
    if part.connectors:
        return part.connectors[0]
    return part.width / 2, part.height / 2


def _row_centers(points: Sequence[tuple[float, float]], tolerance: float) -> list[float]:
    if not points:
        return []
    sorted_values = sorted(point[1] for point in points)
    clusters = [[sorted_values[0]]]
    for value in sorted_values[1:]:
        if abs(value - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _connector_x_bounds(
    points: Sequence[tuple[float, float]],
    fallback_width: float,
) -> tuple[float, float]:
    if not points:
        return 0.0, fallback_width
    xs = [point[0] for point in points]
    return min(xs), max(xs)


def _row_groups(
    placements: Sequence[tuple[float, float]],
    tolerance: float,
) -> list[list[int]]:
    groups: list[list[int]] = []
    centers: list[float] = []
    for index, (_x, y) in enumerate(placements):
        for group_index, center in enumerate(centers):
            if abs(y - center) <= tolerance:
                groups[group_index].append(index)
                count = len(groups[group_index])
                centers[group_index] = (center * (count - 1) + y) / count
                break
        else:
            groups.append([index])
            centers.append(y)
    return groups


def _evenly_space_rows(
    parts: Sequence[PartSvg],
    placements: list[tuple[float, float]],
    board_connectors: Mapping[str, tuple[float, float]],
    board_width: float,
    courtyard_padding: float,
    row_tolerance: float,
) -> None:
    plug_left, plug_right = _connector_x_bounds(
        list(board_connectors.values()),
        board_width,
    )
    if plug_right <= plug_left:
        return
    rows = _row_groups(placements, row_tolerance)
    for row in rows:
        if len(row) <= 1:
            continue
        row_sorted = sorted(row, key=lambda index: placements[index][0])
        plug_bounds = [
            _connector_x_bounds(parts[index].connectors, parts[index].width)
            for index in row_sorted
        ]
        plug_widths = [bounds[1] - bounds[0] for bounds in plug_bounds]
        total_plug_width = sum(plug_widths)
        available_span = plug_right - plug_left
        if available_span <= 0:
            continue
        gap = (available_span - total_plug_width) / (len(row_sorted) - 1)
        min_gap = 0.0
        for pair_index in range(len(row_sorted) - 1):
            left_index = row_sorted[pair_index]
            right_index = row_sorted[pair_index + 1]
            left_part = parts[left_index]
            _left_plug_min, left_plug_max = plug_bounds[pair_index]
            right_plug_min, _right_plug_max = plug_bounds[pair_index + 1]
            required_gap = (
                left_part.width
                + courtyard_padding
                - (-courtyard_padding)
                - left_plug_max
                + right_plug_min
            )
            min_gap = max(min_gap, required_gap)
        if gap < min_gap:
            gap = min_gap
        if gap < 0.0:
            gap = 0.0
        cursor = plug_left
        for index, (plug_min, plug_max) in zip(row_sorted, plug_bounds, strict=False):
            placements[index] = (cursor - plug_min, placements[index][1])
            cursor += (plug_max - plug_min) + gap


def _snap_two_row_part(
    placement: tuple[float, float],
    part_row_centers: Sequence[float],
    board_row_centers: Sequence[float],
    _tolerance: float,
) -> tuple[float, float]:
    if len(part_row_centers) != 2 or len(board_row_centers) < 2:
        return placement
    part_rows = sorted(part_row_centers)
    board_rows = sorted(board_row_centers)
    board_center = (board_rows[0] + board_rows[-1]) / 2
    row_deltas = [
        board_rows[index + 1] - board_rows[index]
        for index in range(len(board_rows) - 1)
        if board_rows[index + 1] > board_rows[index]
    ]
    if not row_deltas:
        return placement
    board_pitch = min(row_deltas)
    if board_pitch <= 0:
        return placement
    part_spacing = part_rows[1] - part_rows[0]
    row_steps = max(1, round(part_spacing / board_pitch))
    target_lower_row = board_center - (row_steps * board_pitch) / 2
    grid_origin = board_rows[0]
    snapped_lower_row = (
        grid_origin
        + round((target_lower_row - grid_origin) / board_pitch) * board_pitch
    )
    offset = snapped_lower_row - (placement[1] + part_rows[0])
    return (placement[0], placement[1] + offset)


def _scale_part_svg(part: PartSvg, target_units_per_in: float | None) -> PartSvg:
    if target_units_per_in is None:
        return part
    physical_units = {"in", "mm", "cm", "pt", "pc"}
    if part.size_unit in physical_units:
        scale = target_units_per_in / 96.0
    else:
        source_units_per_in = part.units_per_in or 96.0
        scale = target_units_per_in / source_units_per_in
    if math.isclose(scale, 1.0):
        return part
    scaled_width = part.width * scale
    scaled_height = part.height * scale
    scaled_connectors_by_id = {
        connector_id: (point[0] * scale, point[1] * scale)
        for connector_id, point in part.connectors_by_id.items()
    }
    scaled_connectors = [
        scaled_connectors_by_id[connector_id]
        for connector_id in part.connector_order
        if connector_id in scaled_connectors_by_id
    ]
    scaled_bounds = (
        tuple(value * scale for value in part.drawing_bounds)
        if part.drawing_bounds
        else None
    )
    part.svg_root.set("width", f"{scaled_width}")
    part.svg_root.set("height", f"{scaled_height}")
    return PartSvg(
        info=part.info,
        svg_root=part.svg_root,
        width=scaled_width,
        height=scaled_height,
        drawing_bounds=scaled_bounds,
        connectors=scaled_connectors,
        connectors_by_id=scaled_connectors_by_id,
        connector_buses=part.connector_buses,
        connector_order=part.connector_order,
        size_unit=part.size_unit,
        units_per_in=part.units_per_in,
    )


def _courtyard_bounds(
    part: PartSvg,
    placement: tuple[float, float],
    padding: float,
) -> tuple[float, float, float, float]:
    x, y = placement
    bounds = part.drawing_bounds or (0.0, 0.0, part.width, part.height)
    min_x, min_y, max_x, max_y = bounds
    return (
        x + min_x - padding,
        y + min_y - padding,
        x + max_x + padding,
        y + max_y + padding,
    )


def _bounds_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] <= second[0]
        or first[0] >= second[2]
        or first[3] <= second[1]
        or first[1] >= second[3]
    )


def _bus_id(connector_id: str, bus_map: Mapping[str, str]) -> str:
    return bus_map.get(connector_id, connector_id)


def _find_board_connector_id(
    point: tuple[float, float],
    board_connectors: Mapping[str, tuple[float, float]],
    tolerance: float,
) -> str | None:
    closest_id: str | None = None
    closest_distance = tolerance * tolerance
    for connector_id, connector_point in board_connectors.items():
        dx = point[0] - connector_point[0]
        dy = point[1] - connector_point[1]
        distance = dx * dx + dy * dy
        if distance <= closest_distance:
            if closest_id is None or distance < closest_distance:
                closest_id = connector_id
                closest_distance = distance
    return closest_id


def _placement_bus_conflict(
    part: PartSvg,
    placement: tuple[float, float],
    board_connectors: Mapping[str, tuple[float, float]],
    board_buses: Mapping[str, str],
    tolerance: float = 1.0,
) -> bool:
    bus_to_part_connectors: dict[str, list[str]] = {}
    for connector_id, connector_point in part.connectors_by_id.items():
        placed_point = (
            placement[0] + connector_point[0],
            placement[1] + connector_point[1],
        )
        board_connector_id = _find_board_connector_id(
            placed_point, board_connectors, tolerance
        )
        if board_connector_id is None:
            continue
        board_bus_id = _bus_id(board_connector_id, board_buses)
        bus_to_part_connectors.setdefault(board_bus_id, []).append(connector_id)
    for part_connectors in bus_to_part_connectors.values():
        if len(part_connectors) <= 1:
            continue
        part_bus_ids = {
            _bus_id(connector_id, part.connector_buses) for connector_id in part_connectors
        }
        if len(part_bus_ids) > 1:
            return True
    return False


def _layout_parts(
    parts: Sequence[PartSvg],
    board_width: float,
    board_height: float,
    board_connectors: Mapping[str, tuple[float, float]],
    board_buses: Mapping[str, str],
    margin: float = 20.0,
    spacing: float = 20.0,
    courtyard_padding: float = 6.0,
) -> tuple[list[tuple[float, float]], list[tuple[float, float, float, float]]]:
    placements: list[tuple[float, float]] = []
    placement_courtyards: list[tuple[float, float, float, float]] = []
    cursor_x = margin
    cursor_y = margin
    row_height = 0.0
    row_tolerance = 1.0
    board_row_centers = _row_centers(board_connectors.values(), row_tolerance)
    available_connectors = list(board_connectors)
    for part in parts:
        if cursor_x + part.width + margin > board_width:
            cursor_x = margin
            cursor_y += row_height + spacing
            row_height = 0.0
        part_row_centers = _row_centers(part.connectors, row_tolerance)
        placement = _snap_two_row_part(
            (cursor_x, cursor_y),
            part_row_centers,
            board_row_centers,
            row_tolerance,
        )
        anchor_x, anchor_y = _local_anchor(part)
        placed_on_connector = False
        if available_connectors:
            candidates = sorted(
                available_connectors,
                key=lambda connector_id: (
                    board_connectors[connector_id][0] - (placement[0] + anchor_x)
                )
                ** 2
                + (
                    board_connectors[connector_id][1] - (placement[1] + anchor_y)
                )
                ** 2,
            )
            for connector_id in candidates:
                target_x, target_y = board_connectors[connector_id]
                candidate = _snap_two_row_part(
                    (target_x - anchor_x, target_y - anchor_y),
                    part_row_centers,
                    board_row_centers,
                    row_tolerance,
                )
                bounds = _courtyard_bounds(part, candidate, courtyard_padding)
                if any(
                    _bounds_overlap(bounds, existing)
                    for existing in placement_courtyards
                ):
                    continue
                if _placement_bus_conflict(
                    part, candidate, board_connectors, board_buses
                ):
                    continue
                placement = candidate
                used_connector_id = _find_board_connector_id(
                    _anchor_point(part, placement),
                    board_connectors,
                    row_tolerance,
                )
                if used_connector_id and used_connector_id in available_connectors:
                    available_connectors.remove(used_connector_id)
                elif connector_id in available_connectors:
                    available_connectors.remove(connector_id)
                placement_courtyards.append(bounds)
                placed_on_connector = True
                break
        if not placed_on_connector:
            placement = _snap_two_row_part(
                placement,
                part_row_centers,
                board_row_centers,
                row_tolerance,
            )
            bounds = _courtyard_bounds(part, placement, courtyard_padding)
            while any(
                _bounds_overlap(bounds, existing)
                for existing in placement_courtyards
            ):
                placement = (placement[0] + spacing, placement[1])
                if placement[0] + part.width + margin > board_width:
                    placement = (margin, placement[1] + row_height + spacing)
                    cursor_y = placement[1]
                    row_height = 0.0
                placement = _snap_two_row_part(
                    placement,
                    part_row_centers,
                    board_row_centers,
                    row_tolerance,
                )
                bounds = _courtyard_bounds(part, placement, courtyard_padding)
            placement_courtyards.append(bounds)
        placements.append(placement)
        cursor_x += part.width + spacing
        row_height = max(row_height, part.height)
        if cursor_y + row_height + margin > board_height:
            console.print(
                f"[yellow]Warning:[/yellow] '{part.info.title}' exceeds the board "
                "height. Consider a larger board size."
            )
    _evenly_space_rows(
        parts,
        placements,
        board_connectors,
        board_width,
        courtyard_padding,
        row_tolerance,
    )
    courtyards = [
        _courtyard_bounds(part, placement, courtyard_padding)
        for part, placement in zip(parts, placements, strict=False)
    ]
    return placements, courtyards


def _anchor_point(part: PartSvg, placement: tuple[float, float]) -> tuple[float, float]:
    anchor_x, anchor_y = _local_anchor(part)
    return placement[0] + anchor_x, placement[1] + anchor_y


def _assemble_layout_svg(
    board_svg: PartSvg,
    parts: Sequence[PartSvg],
    placements: Sequence[tuple[float, float]],
    courtyards: Sequence[tuple[float, float, float, float]],
    show_debug: bool,
) -> ElementTree.Element:
    root = board_svg.svg_root
    if root.get("viewBox") is None:
        root.set("viewBox", f"0 0 {board_svg.width} {board_svg.height}")
    namespace = _svg_namespace(root)

    parts_group = ElementTree.Element(_svg_tag(namespace, "g"), {"id": "parts"})
    wires_group = ElementTree.Element(
        _svg_tag(namespace, "g"),
        {"id": "wires", "stroke": "#d33", "stroke-width": "2", "fill": "none"},
    )
    debug_group = ElementTree.Element(
        _svg_tag(namespace, "g"),
        {
            "id": "courtyards",
            "stroke": "#2b7",
            "stroke-width": "1.5",
            "fill": "none",
            "stroke-dasharray": "4 2",
        },
    )
    connectors_group = ElementTree.Element(
        _svg_tag(namespace, "g"),
        {"id": "debug-connectors", "stroke-width": "1.5"},
    )
    buses_group = ElementTree.Element(
        _svg_tag(namespace, "g"),
        {"id": "debug-buses", "stroke-width": "2", "fill": "none"},
    )
    debug_colors = [
        "#d62728",
        "#1f77b4",
        "#2ca02c",
        "#ff7f0e",
        "#9467bd",
        "#8c564b",
        "#e377c2",
        "#7f7f7f",
        "#bcbd22",
        "#17becf",
    ]
    board_connector_group: ElementTree.Element | None = None
    board_bus_group: ElementTree.Element | None = None

    if show_debug:
        board_color = "#555"
        board_connector_group = ElementTree.Element(
            _svg_tag(namespace, "g"),
            {
                "id": "connectors-board",
                "stroke": board_color,
                "fill": board_color,
                "fill-opacity": "0.45",
            },
        )
        board_bus_group = ElementTree.Element(
            _svg_tag(namespace, "g"),
            {
                "id": "buses-board",
                "stroke": board_color,
            },
        )
        board_bus_points: dict[str, list[tuple[float, float]]] = {}
        for connector_id, point in board_svg.connectors_by_id.items():
            circle = ElementTree.Element(
                _svg_tag(namespace, "circle"),
                {"cx": f"{point[0]}", "cy": f"{point[1]}", "r": "2"},
            )
            board_connector_group.append(circle)
            bus_id = _bus_id(connector_id, board_svg.connector_buses)
            board_bus_points.setdefault(bus_id, []).append(point)
        for bus_id, points in board_bus_points.items():
            if len(points) <= 1:
                continue
            sorted_points = sorted(points, key=lambda value: (value[0], value[1]))
            polyline = ElementTree.Element(
                _svg_tag(namespace, "polyline"),
                {
                    "points": " ".join(
                        f"{point[0]},{point[1]}" for point in sorted_points
                    ),
                    "data-bus": bus_id,
                },
            )
            board_bus_group.append(polyline)

    for index, (part, placement, courtyard) in enumerate(
        zip(parts, placements, courtyards, strict=False)
    ):
        x, y = placement
        part_group = ElementTree.Element(
            _svg_tag(namespace, "g"),
            {"id": f"part-group-{index}", "transform": f"translate({x},{y})"},
        )
        part_root = part.svg_root
        part_root.set("x", "0")
        part_root.set("y", "0")
        part_root.set("id", f"part-{index}")
        part_group.append(part_root)

        label_text = part.info.label or part.info.title
        label = ElementTree.Element(
            _svg_tag(namespace, "text"),
            {"x": "0", "y": f"{part.height + 14}"},
        )
        label.text = label_text
        part_group.append(label)
        parts_group.append(part_group)

        if show_debug:
            left, top, right, bottom = courtyard
            rect = ElementTree.Element(
                _svg_tag(namespace, "rect"),
                {
                    "x": f"{left}",
                    "y": f"{top}",
                    "width": f"{right - left}",
                    "height": f"{bottom - top}",
                },
            )
            debug_group.append(rect)

            color = debug_colors[index % len(debug_colors)]
            connector_group = ElementTree.Element(
                _svg_tag(namespace, "g"),
                {
                    "id": f"connectors-{index}",
                    "stroke": color,
                    "fill": color,
                    "fill-opacity": "0.65",
                },
            )
            bus_group = ElementTree.Element(
                _svg_tag(namespace, "g"),
                {
                    "id": f"buses-{index}",
                    "stroke": color,
                },
            )
            bus_points: dict[str, list[tuple[float, float]]] = {}
            for connector_id, point in part.connectors_by_id.items():
                placed_point = (x + point[0], y + point[1])
                circle = ElementTree.Element(
                    _svg_tag(namespace, "circle"),
                    {
                        "cx": f"{placed_point[0]}",
                        "cy": f"{placed_point[1]}",
                        "r": "3",
                    },
                )
                connector_group.append(circle)
                bus_id = _bus_id(connector_id, part.connector_buses)
                bus_points.setdefault(bus_id, []).append(placed_point)
            for bus_id, points in bus_points.items():
                if len(points) <= 1:
                    continue
                sorted_points = sorted(points, key=lambda value: (value[0], value[1]))
                polyline = ElementTree.Element(
                    _svg_tag(namespace, "polyline"),
                    {
                        "points": " ".join(
                            f"{point[0]},{point[1]}" for point in sorted_points
                        ),
                        "data-bus": bus_id,
                    },
                )
                bus_group.append(polyline)
            connectors_group.append(connector_group)
            buses_group.append(bus_group)

    for index in range(len(parts) - 1):
        start_x, start_y = _anchor_point(parts[index], placements[index])
        end_x, end_y = _anchor_point(parts[index + 1], placements[index + 1])
        wire = ElementTree.Element(
            _svg_tag(namespace, "path"),
            {"d": f"M {start_x} {start_y} L {end_x} {end_y}"},
        )
        wires_group.append(wire)

    if show_debug:
        if board_bus_group is not None:
            root.append(board_bus_group)
        if board_connector_group is not None:
            root.append(board_connector_group)
    root.append(parts_group)
    root.append(wires_group)
    if show_debug:
        root.append(debug_group)
        root.append(buses_group)
        root.append(connectors_group)
    return root


def _svg_bytes(svg_root: ElementTree.Element) -> bytes:
    namespace = _svg_namespace(svg_root)
    if namespace:
        ElementTree.register_namespace("", namespace)
    buffer = io.BytesIO()
    tree = ElementTree.ElementTree(svg_root)
    tree.write(buffer, encoding="utf-8", xml_declaration=True)
    return buffer.getvalue()


def _write_svg(svg_root: ElementTree.Element, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_svg_bytes(svg_root))


def _render_svg_to_terminal(svg_root: ElementTree.Element, scale: float) -> None:
    if scale <= 0:
        raise typer.BadParameter("Scale must be greater than zero.")
    try:
        import cairosvg
        from PIL import Image as PilImage
        from textual_image.renderable import Image as TerminalImage
    except ImportError as exc:
        raise typer.BadParameter(
            "Terminal rendering requires cairosvg, pillow, and textual-image."
        ) from exc

    png_bytes = cairosvg.svg2png(bytestring=_svg_bytes(svg_root), scale=scale)
    image = PilImage.open(io.BytesIO(png_bytes))
    console.print(TerminalImage(image))


def _resolve_board_part_path(library_path: Path, board_size: str) -> Path:
    normalized = board_size.lower()
    if normalized not in BOARD_PART_FILES:
        options = ", ".join(sorted(BOARD_PART_FILES))
        raise typer.BadParameter(f"Unknown board size '{board_size}'. Choose from: {options}.")
    parts_root = _parts_root(library_path)
    for filename in BOARD_PART_FILES[normalized]:
        candidate = parts_root / filename
        if candidate.exists():
            return candidate
    raise typer.BadParameter(f"Board part for '{board_size}' not found in {parts_root}.")


@app.command("list")
def list_parts(
    library_path: Path | None = LIBRARY_PATH_OPTION,
) -> None:
    """List all available Fritzing parts."""
    library_root = _resolve_library_path(library_path)
    parts = _load_parts(library_root)
    parts_sorted = sorted(parts, key=lambda part: part.title.lower())
    _render_parts(parts_sorted, library_root)


@app.command()
def search(
    query: str,
    library_path: Path | None = LIBRARY_PATH_OPTION,
) -> None:
    """Search for parts by name, label, or filename."""
    library_root = _resolve_library_path(library_path)
    parts = _load_parts(library_root)
    matches = _filter_parts(parts, query)
    matches_sorted = sorted(matches, key=lambda part: part.title.lower())
    _render_parts(matches_sorted, library_root)


@app.command()
def summarize(
    library_path: Path | None = LIBRARY_PATH_OPTION,
) -> None:
    """Summarize SVG scale metadata for all parts."""
    library_root = _resolve_library_path(library_path)
    parts = _load_parts(library_root)
    buckets: dict[str, list[PartInfo]] = defaultdict(list)
    bucket_units: dict[str, set[str]] = defaultdict(set)
    errors: list[tuple[PartInfo, str]] = []

    for part in parts:
        try:
            svg_root = _load_part_breadboard_svg(part)
            bucket, unit = _svg_scale_bucket(svg_root)
        except (ValueError, FileNotFoundError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            errors.append((part, str(exc)))
            continue
        buckets[bucket].append(part)
        if unit:
            bucket_units[bucket].add(unit)

    table = Table(title=f"Part scale summary ({len(parts)})")
    table.add_column("Bucket", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Width/height units")
    table.add_column("Examples")

    for bucket, bucket_parts in sorted(
        buckets.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        units = ", ".join(sorted(bucket_units[bucket])) if bucket_units[bucket] else "-"
        examples = ", ".join(part.title for part in bucket_parts[:3]) or "-"
        table.add_row(bucket, str(len(bucket_parts)), units, examples)

    console.print(table)

    if errors:
        error_table = Table(title=f"Unreadable parts ({len(errors)})")
        error_table.add_column("Part")
        error_table.add_column("Error")
        for part, message in errors[:10]:
            error_table.add_row(part.title, message)
        console.print(error_table)
        if len(errors) > 10:
            console.print(f"...and {len(errors) - 10} more.")


@app.command()
def info(
    query: str,
    library_path: Path | None = LIBRARY_PATH_OPTION,
) -> None:
    """Show detailed metadata for a part."""
    library_root = _resolve_library_path(library_path)
    parts = _load_parts(library_root)
    part = _resolve_part(parts, query)
    if not part:
        raise typer.Exit(code=1)

    try:
        fzp_root = _load_part_fzp(part)
    except (ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    title = fzp_root.findtext("title") or part.title
    label = fzp_root.findtext("label") or part.label
    author = fzp_root.findtext("author") or ""
    version = fzp_root.findtext("version") or ""
    date = fzp_root.findtext("date") or ""

    info_table = Table(title="Part info")
    info_table.add_column("Field", style="bold")
    info_table.add_column("Value")
    info_table.add_row("Title", title)
    if label:
        info_table.add_row("Label", label)
    if author:
        info_table.add_row("Author", author)
    if version:
        info_table.add_row("Version", version)
    if date:
        info_table.add_row("Date", date)
    info_table.add_row("File", str(part.path))
    console.print(info_table)

    properties = fzp_root.findall("./properties/property")
    if properties:
        properties_table = Table(title="Properties")
        properties_table.add_column("Name", style="bold")
        properties_table.add_column("Value")
        for prop in properties:
            name = prop.get("name") or ""
            value = (prop.text or "").strip()
            properties_table.add_row(name, value)
        console.print(properties_table)

    view_entries = _view_image_entries(fzp_root)
    views_table = Table(title="Views")
    views_table.add_column("View", style="bold")
    views_table.add_column("SVG")
    for view_tag, image in view_entries:
        views_table.add_row(view_tag, image or "-")
    console.print(views_table)

    view_svgs: dict[str, ElementTree.Element] = {}
    view_errors: list[str] = []
    try:
        with zipfile.ZipFile(part.path) as archive:
            for view_tag, image in view_entries:
                if not image:
                    continue
                try:
                    svg_entry = _resolve_svg_entry(archive, image)
                    svg_data = archive.read(svg_entry)
                    view_svgs[view_tag] = ElementTree.fromstring(svg_data)
                except (KeyError, FileNotFoundError, ElementTree.ParseError) as exc:
                    view_errors.append(f"{view_tag}: {exc}")
    except zipfile.BadZipFile as exc:
        view_errors.append(f"archive: {exc}")

    breadboard_svg = view_svgs.get("breadboardView")
    if breadboard_svg is None:
        console.print("Breadboard SVG: [red]unavailable[/red]")
    else:
        units_per_in = _svg_units_per_in(breadboard_svg)
        width = breadboard_svg.get("width") or ""
        height = breadboard_svg.get("height") or ""
        view_box = breadboard_svg.get("viewBox") or ""
        dpi_value = f"{units_per_in:.2f} units/in" if units_per_in else "unknown"
        svg_table = Table(title="Breadboard SVG")
        svg_table.add_column("Field", style="bold")
        svg_table.add_column("Value")
        svg_table.add_row("Width", width or "-")
        svg_table.add_row("Height", height or "-")
        svg_table.add_row("viewBox", view_box or "-")
        svg_table.add_row("Scale", dpi_value)
        console.print(svg_table)

    for view_tag, svg_root in view_svgs.items():
        console.print(f"[bold]{view_tag}[/bold]")
        _render_svg_to_terminal(svg_root, 1.0)

    if view_errors:
        error_table = Table(title="View errors")
        error_table.add_column("View", style="bold")
        error_table.add_column("Error")
        for message in view_errors:
            if ":" in message:
                view_name, error = message.split(":", 1)
            else:
                view_name, error = "view", message
            error_table.add_row(view_name.strip(), error.strip())
        console.print(error_table)

    connectors = fzp_root.findall(".//connector")
    bus_map = _connector_bus_membership(fzp_root)
    bus_groups: dict[str, list[str]] = defaultdict(list)
    for connector in connectors:
        connector_id = connector.get("id") or ""
        name = connector.get("name") or connector_id
        bus_id = _bus_id(connector_id, bus_map)
        bus_groups[bus_id].append(name)

    bus_table = Table(title=f"Connector buses ({len(bus_groups)})")
    bus_table.add_column("Bus", style="bold")
    bus_table.add_column("Pins")
    bus_table.add_column("Count", justify="right")
    for bus_id, names in sorted(bus_groups.items()):
        sorted_names = ", ".join(sorted(names))
        bus_table.add_row(bus_id, sorted_names, str(len(names)))
    console.print(bus_table)


@app.command()
def help(ctx: typer.Context) -> None:
    """Show help for the CLI."""
    console.print(app.get_help(ctx))


@app.command()
def fritz(
    parts: list[str] = PARTS_ARGUMENT,
    board_size: str = BOARD_SIZE_OPTION,
    output: Path | None = OUTPUT_OPTION,
    scale: float = SCALE_OPTION,
    debug: bool = DEBUG_OPTION,
    library_path: Path | None = LIBRARY_PATH_OPTION,
) -> None:
    """Render an SVG layout showing the selected parts on a breadboard."""
    library_root = _resolve_library_path(library_path)
    catalog = _load_parts(library_root)

    selected_parts: list[PartInfo] = []
    for query in parts:
        part = _resolve_part(catalog, query)
        if not part:
            raise typer.Exit(code=1)
        selected_parts.append(part)

    board_part_path = _resolve_board_part_path(library_root, board_size)
    board_part = PartInfo(title=board_size, label="", path=board_part_path)

    try:
        board_svg = _load_part_svg(board_part)
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    part_svgs: list[PartSvg] = []
    for part in selected_parts:
        try:
            part_svgs.append(_load_part_svg(part))
        except (ValueError, FileNotFoundError) as exc:
            raise typer.BadParameter(str(exc)) from exc

    board_units_per_in = board_svg.units_per_in
    scaled_parts = [_scale_part_svg(part, board_units_per_in) for part in part_svgs]

    placements, courtyards = _layout_parts(
        scaled_parts,
        board_svg.width,
        board_svg.height,
        board_svg.connectors_by_id,
        board_svg.connector_buses,
    )
    svg_root = _assemble_layout_svg(
        board_svg,
        scaled_parts,
        placements,
        courtyards,
        debug,
    )

    console.print(
        f"Rendered layout with {len(scaled_parts)} part(s) on a {board_size} board."
    )
    if output is None:
        _render_svg_to_terminal(svg_root, scale)
        console.print("Displayed layout in the terminal.")
    else:
        _write_svg(svg_root, output)
        console.print(f"SVG written to [bold]{output}[/bold].")


@app.command()
def jump(
    query: str,
    library_path: Path | None = LIBRARY_PATH_OPTION,
) -> None:
    """Connect to a Jumperless board (placeholder)."""
    library_root = _resolve_library_path(library_path)
    parts = _load_parts(library_root)
    part = _resolve_part(parts, query)
    if not part:
        raise typer.Exit(code=1)
    console.print("Jumperless integration is not implemented yet.")
    console.print(f"Target part: [bold]{part.title}[/bold]")


if __name__ == "__main__":
    app()
