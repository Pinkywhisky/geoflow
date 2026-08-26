import math
from pathlib import Path

import ezdxf
import pytest

from app.dxf import analyze_dxf


def _lwpolyline_area(tmp_path: Path, vertices: list[tuple[float, float, float]]) -> float:
    path = tmp_path / "shape.dxf"
    document = ezdxf.new("R2010")
    document.modelspace().add_lwpolyline(vertices, format="xyb", close=True)
    document.saveas(path)
    [result] = analyze_dxf(path)
    return result["area"]


def test_straight_segments_are_unchanged(tmp_path: Path) -> None:
    area = _lwpolyline_area(
        tmp_path, [(0, 0, 0), (4, 0, 0), (4, 3, 0), (0, 3, 0)]
    )
    assert area == pytest.approx(12.0)


def test_quarter_circle(tmp_path: Path) -> None:
    quarter_turn = math.tan(math.pi / 8)
    area = _lwpolyline_area(
        tmp_path, [(0, 0, 0), (1, 0, quarter_turn), (0, 1, 0)]
    )
    assert area == pytest.approx(math.pi / 4)


def test_full_circle_with_four_arcs(tmp_path: Path) -> None:
    radius = 3.0
    quarter_turn = math.tan(math.pi / 8)
    area = _lwpolyline_area(
        tmp_path,
        [
            (radius, 0, quarter_turn),
            (0, radius, quarter_turn),
            (-radius, 0, quarter_turn),
            (0, -radius, quarter_turn),
        ],
    )
    assert area == pytest.approx(math.pi * radius**2)


def test_mixed_straight_segments_and_arc(tmp_path: Path) -> None:
    area = _lwpolyline_area(
        tmp_path, [(0, 0, 1), (2, 0, 0), (2, 1, 0), (0, 1, 0)]
    )
    assert area == pytest.approx(2.0 + math.pi / 2)


def test_reversed_orientation_has_same_positive_area(tmp_path: Path) -> None:
    area = _lwpolyline_area(
        tmp_path, [(0, 1, 0), (2, 1, 0), (2, 0, -1), (0, 0, 0)]
    )
    assert area == pytest.approx(2.0 + math.pi / 2)
    assert area > 0


def test_negative_bulge_semicircle(tmp_path: Path) -> None:
    area = _lwpolyline_area(tmp_path, [(2, 0, -1), (0, 0, 0)])
    assert area == pytest.approx(math.pi / 2)


def test_arc_larger_than_180_degrees(tmp_path: Path) -> None:
    three_quarter_turn = math.tan(3 * math.pi / 8)
    area = _lwpolyline_area(
        tmp_path, [(0, 0, 0), (1, 0, three_quarter_turn), (0, -1, 0)]
    )
    assert area == pytest.approx(3 * math.pi / 4)


def test_zero_bulge_matches_classic_polyline(tmp_path: Path) -> None:
    assert _lwpolyline_area(
        tmp_path, [(0, 0, 0), (5, 0, 0), (5, 2, 0), (0, 2, 0)]
    ) == pytest.approx(10.0)


def test_tiny_bulge_is_numerically_stable(tmp_path: Path) -> None:
    bulge = 1e-12
    area = _lwpolyline_area(
        tmp_path, [(0, 0, bulge), (2, 0, 0), (2, 1, 0), (0, 1, 0)]
    )
    assert area == pytest.approx(2.0 + 4.0 * bulge / 3.0, abs=1e-14)


def test_zero_length_chord_does_not_crash(tmp_path: Path) -> None:
    area = _lwpolyline_area(
        tmp_path, [(0, 0, 1), (0, 0, 0), (1, 0, 0), (0, 1, 0)]
    )
    assert area == pytest.approx(0.5)


def test_classic_2d_polyline_bulge(tmp_path: Path) -> None:
    path = tmp_path / "classic.dxf"
    document = ezdxf.new("R2010")
    document.modelspace().add_polyline2d(
        [(0, 0, 1), (2, 0, 0)], format="xyb", close=True
    )
    document.saveas(path)
    [result] = analyze_dxf(path)
    assert result["area"] == pytest.approx(math.pi / 2)
