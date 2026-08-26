from pathlib import Path

import ezdxf
import pytest

from app.dxf import DxfAnalysisError, analyze_dxf

SAMPLE = Path(__file__).parents[1] / "samples" / "exemple_geometre.dxf"


def test_sample_areas() -> None:
    results = analyze_dxf(SAMPLE)
    assert len(results) == 3
    assert sorted(item["area"] for item in results) == pytest.approx([38, 72, 80])
    assert sum(item["area"] for item in results) == pytest.approx(190)


def test_invalid_dxf(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.dxf"
    invalid.write_text("not a DXF", encoding="utf-8")
    with pytest.raises(DxfAnalysisError):
        analyze_dxf(invalid)


def test_no_closed_polyline(tmp_path: Path) -> None:
    path = tmp_path / "open.dxf"
    document = ezdxf.new("R2010")
    document.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 3)], close=False)
    document.saveas(path)
    assert analyze_dxf(path) == []
