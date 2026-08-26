import math
from pathlib import Path

import ezdxf
import pytest

from app.dxf import analyze_dxf


def test_closed_3d_polyline_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "3d.dxf"
    document = ezdxf.new("R2010")
    document.modelspace().add_polyline3d(
        [(0, 0, 0), (4, 0, 1), (4, 3, 2), (0, 3, 1)], close=True
    )
    document.saveas(path)

    assert analyze_dxf(path) == []
    assert "Ignoring non-2D POLYLINE" in caplog.text


def test_bulge_semicircle_area(tmp_path: Path) -> None:
    path = tmp_path / "bulge.dxf"
    document = ezdxf.new("R2010")
    document.modelspace().add_lwpolyline(
        [(0, 0, 1), (2, 0, 0)], format="xyb", close=True
    )
    document.saveas(path)

    [result] = analyze_dxf(path)
    assert result["area"] == pytest.approx(math.pi / 2)
