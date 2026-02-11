from xml.etree import ElementTree

from breadboard.cli import _svg_drawing_bounds


def test_svg_drawing_bounds_ignores_non_rendered_shapes() -> None:
    svg = ElementTree.fromstring(
        """
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect x="0" y="0" width="100" height="100" fill="none" stroke="none"/>
          <rect x="10" y="20" width="30" height="40" fill="#000"/>
        </svg>
        """
    )

    assert _svg_drawing_bounds(svg, 100.0, 100.0) == (10.0, 20.0, 40.0, 60.0)


def test_svg_drawing_bounds_respects_inherited_fill_none() -> None:
    svg = ElementTree.fromstring(
        """
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <g fill="none" stroke="none">
            <rect x="0" y="0" width="100" height="100"/>
          </g>
          <line x1="5" y1="90" x2="95" y2="90" stroke="#000"/>
        </svg>
        """
    )

    assert _svg_drawing_bounds(svg, 100.0, 100.0) == (5.0, 90.0, 95.0, 90.0)


def test_svg_drawing_bounds_clips_to_viewport() -> None:
    svg = ElementTree.fromstring(
        """
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect x="-20" y="10" width="40" height="20" fill="#000"/>
        </svg>
        """
    )

    assert _svg_drawing_bounds(svg, 100.0, 100.0) == (0.0, 10.0, 20.0, 30.0)
