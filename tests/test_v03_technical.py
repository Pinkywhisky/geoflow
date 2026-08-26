from pathlib import Path

import ezdxf
import pytest
from ezdxf import units

from app.dxf import inspect_dxf
from app.dxf.technical import safe_dxf_string


def build_technical_dxf(path: Path) -> Path:
    document = ezdxf.new("R2010")
    document.units = units.M
    document.header["$INSUNITS"] = units.M
    document.layers.add("80")
    document.layers.add("81")
    trash = document.layers.add("Poubelle")
    trash.dxf.flags = 1
    trash.dxf.color = -7
    trash.dxf.plot = 0
    modelspace = document.modelspace()
    modelspace.add_lwpolyline(
        [(0, 0), (10, 0), (10, 5), (0, 5)],
        close=True,
        dxfattribs={"layer": "80"},
    )
    modelspace.add_lwpolyline(
        [(20, 0), (24, 0), (24, 3), (20, 3)],
        close=True,
        dxfattribs={"layer": "81"},
    )
    modelspace.add_lwpolyline(
        [(0, -5), (4, -5), (4, -2)],
        close=False,
        dxfattribs={"layer": "80"},
    )
    modelspace.add_polyline3d(
        [(30, 0, 0), (34, 0, 1), (34, 4, 2), (30, 4, 0)],
        close=True,
    )
    modelspace.add_text(
        "<script>alert(1)</script>",
        dxfattribs={"layer": "80", "height": 0.5},
    ).set_placement((2, 2))
    modelspace.add_text(
        "VERSION 1 ABANDONNEE",
        dxfattribs={"height": 1},
    ).set_placement((100, 100))
    document.layouts.new("Planche A")
    document.saveas(path)
    return path


def test_unit_layers_candidates_and_provenance(tmp_path: Path) -> None:
    path = build_technical_dxf(tmp_path / "technical.dxf")
    control, planches = inspect_dxf(path, "technical.dxf")

    assert control.unite_detectee == "metre"
    assert control.version_dxf == "AC1024"
    assert control.polylignes_fermees == 2
    assert control.types_entites["LWPOLYLINE"] == 3
    assert [layer.nom for layer in control.calques[:2]] == ["80", "81"]
    trash = next(layer for layer in control.calques if layer.nom == "Poubelle")
    assert trash.exclusion_suggeree is True
    assert trash.visible is False
    assert trash.gele is True
    assert trash.trace is False

    candidate = next(item for item in control.zones_candidates if item.calque == "80")
    assert candidate.surface_geometrique_m2 == pytest.approx(50)
    assert candidate.handle_dxf
    assert candidate.provenance.fichier_source == "technical.dxf"
    assert candidate.provenance.handle_dxf == candidate.handle_dxf
    assert candidate.provenance.calque == "80"
    assert candidate.provenance.planche_region == "model"
    assert len(candidate.sommets) == 4
    assert "<script>alert(1)</script>" in candidate.textes_proches
    assert any("non 2D" in warning for warning in control.avertissements)
    assert any(
        planche.methode_detection == "texte_version_abandonnee_a_valider"
        for planche in planches
    )


def test_dwg_text_sanitizer_removes_controls_and_limits_length() -> None:
    unsafe = "zone" + chr(1) + "<script>" + "x" * 600
    safe = safe_dxf_string(unsafe)
    assert chr(1) not in safe
    assert len(safe) == 500
    assert "<script>" in safe


def test_bulges_are_kept_in_candidate_geometry(tmp_path: Path) -> None:
    path = tmp_path / "bulge-source.dxf"
    document = ezdxf.new("R2010")
    document.units = units.M
    document.header["$INSUNITS"] = units.M
    document.modelspace().add_lwpolyline(
        [(0, 0, 1), (2, 0, 0)],
        format="xyb",
        close=True,
    )
    document.saveas(path)

    control, _ = inspect_dxf(path, path.name)
    [candidate] = control.zones_candidates
    assert candidate.sommets[0].bulge == 1
    assert candidate.surface_geometrique_m2 == pytest.approx(3.141592653589793 / 2)
