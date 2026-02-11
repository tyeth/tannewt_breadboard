from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from xml.etree import ElementTree

import cairosvg
import typer
from PIL import Image, ImageDraw

from breadboard.cli import PartInfo, _load_part_svg

app = typer.Typer(help="Generate tiny 24-bit BMPs from Fritzing breadboard SVGs.")

# Hi-res SVG render scale used before grid sampling.
# Keep this moderate to avoid large memory spikes.
HIRES_RENDER_SCALE = 16


@dataclass(frozen=True)
class SvgAsset:
    source_part: Path
    slug: str
    svg_data: bytes
    width_in: float
    height_in: float
    svg_width: float
    svg_height: float
    drawing_bounds: tuple[float, float, float, float] | None
    connector_points: list[tuple[float, float]]
    anchor_point: tuple[float, float] | None


def _parse_svg_length(value: str | None) -> tuple[float, str] | None:
    if not value:
        return None
    match = re.match(r"^([+-]?[0-9]*\.?[0-9]+)([a-z%]*)$", value.strip())
    if not match:
        return None
    magnitude = float(match.group(1))
    unit = match.group(2).lower() or "px"
    return magnitude, unit


def _length_to_inches(length: tuple[float, str]) -> float | None:
    value, unit = length
    if unit == "in":
        return value
    if unit == "mm":
        return value / 25.4
    if unit == "cm":
        return value / 2.54
    if unit == "pt":
        return value / 72.0
    if unit == "pc":
        return value / 6.0
    if unit == "px":
        return value / 96.0
    return None


def _viewbox(svg_root: ElementTree.Element) -> tuple[float, float, float, float] | None:
    raw = svg_root.get("viewBox")
    if not raw:
        return None
    values = [float(part) for part in raw.replace(",", " ").split()]
    if len(values) != 4:
        return None
    return values[0], values[1], values[2], values[3]


def _svg_inches(svg_data: bytes) -> tuple[float, float]:
    svg_root = ElementTree.fromstring(svg_data)
    width_length = _parse_svg_length(svg_root.get("width"))
    height_length = _parse_svg_length(svg_root.get("height"))

    width_in = _length_to_inches(width_length) if width_length else None
    height_in = _length_to_inches(height_length) if height_length else None
    if width_in is not None and height_in is not None:
        return width_in, height_in

    box = _viewbox(svg_root)
    if box is None:
        raise ValueError("SVG is missing width/height and viewBox.")

    # Fritzing SVGs usually map ~90 internal units to 1 inch.
    # If px sizing is available, derive the actual ratio; otherwise use 90.
    units_per_in_candidates: list[float] = []
    if width_length and width_length[1] == "px":
        px_in = width_length[0] / 96.0
        if px_in > 0:
            units_per_in_candidates.append(box[2] / px_in)
    if height_length and height_length[1] == "px":
        px_in = height_length[0] / 96.0
        if px_in > 0:
            units_per_in_candidates.append(box[3] / px_in)

    units_per_in = (
        sum(units_per_in_candidates) / len(units_per_in_candidates)
        if units_per_in_candidates
        else 90.0
    )
    return box[2] / units_per_in, box[3] / units_per_in


def _slug(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip())
    text = text.strip("-").lower()
    return text or "part"


def _resolve_svg_entry(archive: zipfile.ZipFile, image_path: str) -> str:
    candidate = f"svg.{image_path.replace('/', '.')}"
    if candidate in archive.namelist():
        return candidate
    if image_path in archive.namelist():
        return image_path
    basename = Path(image_path).name
    for name in archive.namelist():
        if name.endswith(basename):
            return name
    raise FileNotFoundError(f"Unable to locate SVG asset for {image_path}.")


def _svg_asset_from_part(part_path: Path) -> SvgAsset | None:
    with zipfile.ZipFile(part_path) as archive:
        fzp_name = next((name for name in archive.namelist() if name.endswith(".fzp")), None)
        if not fzp_name:
            return None
        fzp_root = ElementTree.fromstring(archive.read(fzp_name))
        layers = fzp_root.find(".//breadboardView/layers")
        if layers is None:
            return None
        image_path = layers.get("image")
        if not image_path:
            return None

        title = (fzp_root.findtext("title") or part_path.stem).strip()
        svg_entry = _resolve_svg_entry(archive, image_path)
        svg_data = archive.read(svg_entry)

    width_in, height_in = _svg_inches(svg_data)
    part_svg = _load_part_svg(PartInfo(title=title, label="", path=part_path))
    return SvgAsset(
        source_part=part_path,
        slug=_slug(title),
        svg_data=svg_data,
        width_in=width_in,
        height_in=height_in,
        svg_width=part_svg.width,
        svg_height=part_svg.height,
        drawing_bounds=part_svg.drawing_bounds,
        connector_points=list(part_svg.connectors_by_id.values()),
        anchor_point=part_svg.connectors[0] if part_svg.connectors else None,
    )


def _iter_parts(library_path: Path) -> list[Path]:
    parts_root = library_path / "parts" if (library_path / "parts").exists() else library_path
    return sorted(parts_root.rglob("*.fzpz"))


def _prepare_hires_for_sampling(image: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int] | None]:
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return Image.new("RGB", (1, 1), (0, 0, 0)), None

    black = Image.new("RGBA", image.size, (0, 0, 0, 255))
    hi_res = Image.alpha_composite(black, image).convert("RGB")
    return hi_res, bbox


def _trim_black_border(image: Image.Image) -> Image.Image:
    width, height = image.size
    pixels = image.load()

    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    for y in range(height):
        for x in range(width):
            if pixels[x, y] != (0, 0, 0):
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)

    if max_x < 0 or max_y < 0:
        return image
    return image.crop((min_x, min_y, max_x + 1, max_y + 1))


def _trim_horizontal_black_border(image: Image.Image) -> Image.Image:
    trimmed, _x_shift = _trim_horizontal_black_border_with_shift(image)
    return trimmed


def _trim_horizontal_black_border_with_shift(
    image: Image.Image,
    preserve_cols: set[int] | None = None,
) -> tuple[Image.Image, int]:
    """Return (image, x_shift) where new_x = old_x + x_shift."""
    width, height = image.size
    pixels = image.load()

    min_x = width
    max_x = -1
    for y in range(height):
        for x in range(width):
            if pixels[x, y] != (0, 0, 0):
                min_x = min(min_x, x)
                max_x = max(max_x, x)

    if preserve_cols:
        valid_cols = [col for col in preserve_cols if 0 <= col < width]
        if valid_cols:
            min_x = min(min_x, min(valid_cols))
            max_x = max(max_x, max(valid_cols))

    if max_x < 0:
        return image, 0
    return image.crop((min_x, 0, max_x + 1, height)), -min_x


def _crop_vertical_to_target_height(image: Image.Image, target_height: int) -> Image.Image:
    cropped, _y_shift = _crop_vertical_to_target_height_with_shift(image, target_height)
    return cropped


def _crop_vertical_to_target_height_with_shift(
    image: Image.Image,
    target_height: int,
) -> tuple[Image.Image, int]:
    """Return (image, y_shift) where new_y = old_y + y_shift."""
    return _crop_vertical_to_target_height_preserving_rows_with_shift(
        image,
        target_height,
        preserve_rows=None,
    )


def _crop_vertical_to_target_height_preserving_rows_with_shift(
    image: Image.Image,
    target_height: int,
    preserve_rows: set[int] | None,
) -> tuple[Image.Image, int]:
    """Return (image, y_shift) where new_y = old_y + y_shift."""
    if target_height <= 0:
        return image, 0

    if preserve_rows:
        min_row = min(preserve_rows)
        max_row = max(preserve_rows)
        target_height = max(target_height, max_row - min_row + 1)

    if image.height == target_height:
        return image, 0

    if image.height < target_height:
        padded = Image.new("RGB", (image.width, target_height), (0, 0, 0))
        offset = (target_height - image.height) // 2
        padded.paste(image, (0, offset))
        return padded, offset

    if preserve_rows:
        min_row = min(preserve_rows)
        max_row = max(preserve_rows)
        center = (min_row + max_row) / 2.0
        start = int(round(center - target_height / 2.0))
    else:
        pixels = image.load()
        min_y = image.height
        max_y = -1
        for y in range(image.height):
            row_nonblack = any(pixels[x, y] != (0, 0, 0) for x in range(image.width))
            if row_nonblack:
                min_y = min(min_y, y)
                max_y = max(max_y, y)

        if max_y < 0:
            start = (image.height - target_height) // 2
        else:
            center = (min_y + max_y) / 2.0
            start = int(round(center - target_height / 2.0))

    start = max(0, min(start, image.height - target_height))
    return image.crop((0, start, image.width, start + target_height)), -start


def _remove_top_black_rows_with_shift(image: Image.Image) -> tuple[Image.Image, int]:
    """Move content up so first non-black row is y=0, preserving height."""
    if image.height <= 0 or image.width <= 0:
        return image, 0

    pixels = image.load()
    top_black_rows = 0
    for y in range(image.height):
        if all(pixels[x, y] == (0, 0, 0) for x in range(image.width)):
            top_black_rows += 1
        else:
            break

    if top_black_rows <= 0:
        return image, 0
    if top_black_rows >= image.height:
        return image, 0

    shifted = Image.new("RGB", image.size, (0, 0, 0))
    moved = image.crop((0, top_black_rows, image.width, image.height))
    shifted.paste(moved, (0, 0))
    return shifted, -top_black_rows


def _cluster_values(values: list[float], tolerance: float) -> list[float]:
    if not values:
        return []
    sorted_values = sorted(values)
    clusters: list[list[float]] = [[sorted_values[0]]]
    for value in sorted_values[1:]:
        if abs(value - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _median_pitch(centers: list[float]) -> float | None:
    if len(centers) < 2:
        return None
    diffs = [
        centers[index + 1] - centers[index]
        for index in range(len(centers) - 1)
        if centers[index + 1] > centers[index]
    ]
    if not diffs:
        return None
    return float(median(diffs))


def _sample_pixel(
    src: object,
    x: int,
    y: int,
    bbox: tuple[int, int, int, int] | None,
    width: int,
    height: int,
) -> tuple[int, int, int]:
    if x < 0 or x >= width or y < 0 or y >= height:
        return (0, 0, 0)
    if bbox is not None:
        left, top, right, bottom = bbox
        if x < left or x >= right or y < top or y >= bottom:
            return (0, 0, 0)
    return src[x, y]


def _force_connector_pixels(
    image: Image.Image,
    connector_cells: set[tuple[int, int]],
    x_shift: int = 0,
    y_shift: int = 0,
) -> None:
    pixels = image.load()
    for col, row in connector_cells:
        x = col + x_shift
        y = row + y_shift
        if 0 <= x < image.width and 0 <= y < image.height:
            pixels[x, y] = (255, 255, 255)


def _stamp_cell(
    image: Image.Image,
    cell: tuple[int, int] | None,
    color: tuple[int, int, int],
    x_shift: int = 0,
    y_shift: int = 0,
) -> None:
    if cell is None or image.width <= 0 or image.height <= 0:
        return
    x = cell[0] + x_shift
    y = cell[1] + y_shift
    x = max(0, min(image.width - 1, x))
    y = max(0, min(image.height - 1, y))
    image.putpixel((x, y), color)


def _render_fallback_tiny_bmp(asset: SvgAsset, pixels_per_in: float) -> tuple[Image.Image, Image.Image | None]:
    width_px = max(1, int(math.ceil(asset.width_in * pixels_per_in)))
    height_px = max(1, int(math.ceil(asset.height_in * pixels_per_in)))
    render_scale = HIRES_RENDER_SCALE
    render_width = width_px * render_scale
    render_height = height_px * render_scale

    png = cairosvg.svg2png(
        bytestring=asset.svg_data,
        output_width=render_width,
        output_height=render_height,
        background_color="transparent",
    )
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    hi_res, bbox = _prepare_hires_for_sampling(image)
    src = hi_res.load()

    out = Image.new("RGB", (width_px, height_px))
    dst = out.load()
    for y in range(height_px):
        sample_y = min(
            hi_res.height - 1,
            max(0, int(round((y + 0.5) * hi_res.height / height_px))),
        )
        for x in range(width_px):
            sample_x = min(
                hi_res.width - 1,
                max(0, int(round((x + 0.5) * hi_res.width / width_px))),
            )
            dst[x, y] = _sample_pixel(
                src,
                sample_x,
                sample_y,
                bbox,
                hi_res.width,
                hi_res.height,
            )
    return _trim_black_border(out), None


def _detect_grid(
    connector_points: list[tuple[float, float]],
) -> tuple[float, float, float, float, list[float], list[float]] | None:
    if not connector_points:
        return None
    x_values = [point[0] for point in connector_points]
    y_values = [point[1] for point in connector_points]
    x_tolerance = max(0.25, (max(x_values) - min(x_values)) / 500.0)
    y_tolerance = max(0.25, (max(y_values) - min(y_values)) / 500.0)
    x_centers = _cluster_values(x_values, x_tolerance)
    y_centers = _cluster_values(y_values, y_tolerance)

    x_pitch = _median_pitch(x_centers)
    y_pitch = _median_pitch(y_centers)
    if x_pitch is None or x_pitch <= 0:
        return None

    # 0.1" grid is square in breadboard coordinates.
    if y_pitch is None or y_pitch <= 0:
        y_pitch = x_pitch
    else:
        y_pitch = min(y_pitch, x_pitch)

    anchor_x = min(x_centers)
    anchor_y = min(y_centers)
    return anchor_x, anchor_y, x_pitch, y_pitch, x_centers, y_centers


def _draw_debug_grid_overlay(
    hi_res: Image.Image,
    render_scale: int,
    connector_points: list[tuple[float, float]],
    anchor_x: float,
    anchor_y: float,
    x_pitch: float,
    y_pitch: float,
    min_col: int,
    max_col: int,
    min_row: int,
    max_row: int,
) -> Image.Image:
    debug = hi_res.convert("RGBA")
    draw = ImageDraw.Draw(debug, "RGBA")

    for col in range(min_col, max_col + 1):
        local_x = anchor_x + col * x_pitch
        x_px = int(round(local_x * render_scale))
        draw.line([(x_px, 0), (x_px, debug.height - 1)], fill=(255, 0, 0, 100), width=1)

    for row in range(min_row, max_row + 1):
        local_y = anchor_y + row * y_pitch
        y_px = int(round(local_y * render_scale))
        draw.line([(0, y_px), (debug.width - 1, y_px)], fill=(0, 180, 255, 100), width=1)

    radius = max(2, render_scale // 6)
    for x, y in connector_points:
        cx = int(round(x * render_scale))
        cy = int(round(y * render_scale))
        draw.ellipse(
            [(cx - radius, cy - radius), (cx + radius, cy + radius)],
            outline=(0, 255, 0, 220),
            width=1,
        )

    return debug


def _render_tiny_bmp(asset: SvgAsset, pixels_per_in: float) -> tuple[Image.Image, Image.Image | None]:
    render_scale = HIRES_RENDER_SCALE
    render_width = max(1, int(math.ceil(asset.svg_width * render_scale)))
    render_height = max(1, int(math.ceil(asset.svg_height * render_scale)))

    png = cairosvg.svg2png(
        bytestring=asset.svg_data,
        output_width=render_width,
        output_height=render_height,
        background_color="transparent",
    )
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    hi_res, bbox = _prepare_hires_for_sampling(image)
    src = hi_res.load()

    detected = _detect_grid(asset.connector_points)
    if detected is None:
        return _render_fallback_tiny_bmp(asset, pixels_per_in)

    anchor_x, anchor_y, x_pitch, y_pitch, _x_centers, _y_centers = detected

    if asset.drawing_bounds is not None:
        min_x, min_y, max_x, max_y = asset.drawing_bounds
    elif bbox is not None:
        min_x = bbox[0] / render_scale
        min_y = bbox[1] / render_scale
        max_x = bbox[2] / render_scale
        max_y = bbox[3] / render_scale
    else:
        min_x = 0.0
        min_y = 0.0
        max_x = asset.svg_width
        max_y = asset.svg_height

    min_col = int(math.floor((min_x - anchor_x) / x_pitch))
    max_col = int(math.ceil((max_x - anchor_x) / x_pitch))
    min_row = int(math.floor((min_y - anchor_y) / y_pitch))
    max_row = int(math.ceil((max_y - anchor_y) / y_pitch))

    grid_width = max(1, max_col - min_col + 1)
    grid_height = max(1, max_row - min_row + 1)
    out = Image.new("RGB", (grid_width, grid_height))
    dst = out.load()

    connector_cells: set[tuple[int, int]] = set()
    for connector_x, connector_y in asset.connector_points:
        col = int(round((connector_x - anchor_x) / x_pitch)) - min_col
        row = int(round((connector_y - anchor_y) / y_pitch)) - min_row
        if 0 <= col < grid_width and 0 <= row < grid_height:
            connector_cells.add((col, row))

    anchor_cell: tuple[int, int] | None = None
    if asset.anchor_point is not None:
        anchor_col = int(round((asset.anchor_point[0] - anchor_x) / x_pitch)) - min_col
        anchor_row = int(round((asset.anchor_point[1] - anchor_y) / y_pitch)) - min_row
        if 0 <= anchor_col < grid_width and 0 <= anchor_row < grid_height:
            anchor_cell = (anchor_col, anchor_row)

    for row in range(grid_height):
        local_y = anchor_y + (min_row + row) * y_pitch
        sample_y = int(round(local_y * render_scale))
        for col in range(grid_width):
            local_x = anchor_x + (min_col + col) * x_pitch
            sample_x = int(round(local_x * render_scale))
            color = _sample_pixel(
                src,
                sample_x,
                sample_y,
                bbox,
                hi_res.width,
                hi_res.height,
            )
            if color == (0, 0, 0) and (col, row) in connector_cells:
                color = (255, 255, 255)
            dst[col, row] = color

    debug = _draw_debug_grid_overlay(
        hi_res,
        render_scale,
        asset.connector_points,
        anchor_x,
        anchor_y,
        x_pitch,
        y_pitch,
        min_col,
        max_col,
        min_row,
        max_row,
    )

    # Remove black perimeter and then ensure connector LEDs always light.
    trimmed, x_shift = _trim_horizontal_black_border_with_shift(out)
    target_height = int(round((max(_y_centers) - min(_y_centers)) / x_pitch)) + 1
    cropped, y_shift = _crop_vertical_to_target_height_with_shift(trimmed, target_height)
    cropped, top_shift = _remove_top_black_rows_with_shift(cropped)
    y_shift += top_shift

    _force_connector_pixels(cropped, connector_cells, x_shift=x_shift, y_shift=y_shift)
    # Mark the layout anchor (first connector used by placement) in pink for alignment debugging.
    _stamp_cell(cropped, anchor_cell, (255, 0, 255), x_shift=x_shift, y_shift=y_shift)
    return cropped, debug


@app.command()
def generate(
    library_path: Path = typer.Option(Path("Fritzing-Library"), "--library-path", "-l"),
    output_dir: Path = typer.Option(Path("tiny-bmps"), "--output-dir", "-o"),
    pixels_per_in: float = typer.Option(10.0, "--pixels-per-inch", help="0.1in => 10 px/in"),
    limit: int | None = typer.Option(None, "--limit", help="Generate only first N parts."),
) -> None:
    """Render all part breadboard SVGs to tiny 24-bit BMPs."""
    if pixels_per_in <= 0:
        raise typer.BadParameter("--pixels-per-inch must be > 0")
    if not library_path.exists():
        raise typer.BadParameter(f"Library path does not exist: {library_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = output_dir / "_grid-debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    for part_path in _iter_parts(library_path):
        if limit is not None and written >= limit:
            break
        try:
            asset = _svg_asset_from_part(part_path)
            if asset is None:
                skipped += 1
                continue
            bmp, debug = _render_tiny_bmp(asset, pixels_per_in=pixels_per_in)
            out_path = output_dir / f"{asset.slug}.bmp"
            bmp.save(out_path, format="BMP")
            if debug is not None:
                debug.save(debug_dir / f"{asset.slug}.png", format="PNG")
            written += 1
        except Exception:
            skipped += 1

    typer.echo(f"Wrote {written} BMP files to {output_dir}")
    typer.echo(f"Wrote grid debug overlays to {debug_dir}")
    if skipped:
        typer.echo(f"Skipped {skipped} parts")


if __name__ == "__main__":
    app()
