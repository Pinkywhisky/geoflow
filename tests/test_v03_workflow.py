from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain import DecisionValidation
from app.dxf import inspect_dxf
from app.storage import DossierNotFoundError, JsonDossierRepository
from app.workflow import (
    associate_candidate,
    attach_import,
    confirm_unit,
    create_dossier,
    safe_filename,
)

from .test_v03_technical import build_technical_dxf


def ready_dossier(tmp_path: Path):
    source = build_technical_dxf(tmp_path / "workflow.dxf")
    control, planches = inspect_dxf(source, source.name)
    dossier = create_dossier("P-TEST", "copropriete")
    attach_import(dossier, source.name, "dxf", control, planches)
    confirm_unit(dossier, "metre")
    return dossier


def test_lot_can_group_multiple_zones_and_levels(tmp_path: Path) -> None:
    dossier = ready_dossier(tmp_path)
    first, second = dossier.controle_technique.zones_candidates
    first_zone = associate_candidate(
        dossier,
        first.id,
        building_code="A",
        level_code="RdC",
        lot_number="1",
        category="principale",
    )
    second_zone = associate_candidate(
        dossier,
        second.id,
        building_code="A",
        level_code="Sous-sol",
        lot_number="1",
        category="secondaire_annexe",
    )

    assert len(dossier.lots) == 1
    assert dossier.lots[0].zone_ids == [first_zone.id, second_zone.id]
    assert len(dossier.lots[0].niveau_ids) == 2
    assert len(dossier.zones) == 2


def test_geometric_and_retained_surfaces_remain_distinct(tmp_path: Path) -> None:
    dossier = ready_dossier(tmp_path)
    candidate = dossier.controle_technique.zones_candidates[0]
    zone = associate_candidate(
        dossier,
        candidate.id,
        building_code="A",
        level_code="RdC",
        lot_number="1",
        category="principale",
        retained_surface=49.5,
        justification="Déduction mesurée et validée.",
    )
    assert zone.surface_geometrique_m2 == pytest.approx(50)
    assert zone.surface_retenue_m2 == pytest.approx(49.5)
    assert zone.decision_surface.propose == pytest.approx(50)
    assert zone.decision_surface.retenu == pytest.approx(49.5)
    assert zone.decision_surface.justification


def test_surface_correction_requires_justification_without_partial_mutation(
    tmp_path: Path,
) -> None:
    dossier = ready_dossier(tmp_path)
    candidate = dossier.controle_technique.zones_candidates[0]
    with pytest.raises(ValidationError, match="justification"):
        associate_candidate(
            dossier,
            candidate.id,
            building_code="A",
            level_code="RdC",
            lot_number="1",
            category="principale",
            retained_surface=49,
        )
    assert dossier.batiments == []
    assert dossier.niveaux == []
    assert dossier.lots == []
    assert dossier.zones == []


def test_decision_model_requires_justification_for_any_correction() -> None:
    with pytest.raises(ValidationError, match="justification"):
        DecisionValidation(champ="unite", propose="metre", retenu="millimetre")


def test_json_repository_round_trip_and_path_safety(tmp_path: Path) -> None:
    dossier = ready_dossier(tmp_path)
    repository = JsonDossierRepository(tmp_path / "json")
    repository.save(dossier)
    restored = repository.get(dossier.id)

    assert restored == dossier
    payload = (tmp_path / "json" / f"{dossier.id}.json").read_text(encoding="utf-8")
    assert payload.endswith(chr(10))
    assert '"schema_version": "1.0"' in payload
    assert "zones_candidates" in payload
    with pytest.raises(DossierNotFoundError):
        repository.get("../private")


def test_uploaded_filename_cannot_traverse_directories() -> None:
    assert safe_filename("../../secret.dxf") == "secret.dxf"
    assert safe_filename("..\\..\\secret.dwg") == "secret.dwg"
