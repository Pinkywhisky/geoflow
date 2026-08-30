from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import ControleTechnique, Dossier, Planche, VerificationStatus
from app.domain.models import StatutRevue
from app.quick_analysis import (
    QuickAnalysisNotFoundError,
    QuickAnalysisRepository,
    QuickAnalysisSnapshot,
    build_quick_diagnostic,
    prepare_quick_analysis_dossier,
    promote_quick_analysis,
)
from app.reconciliation import reconcile_dossier
from app.storage import JsonDossierRepository

from .test_v03_technical import build_technical_dxf
from .test_v042_reconciliation import ANNEX, candidate, synthetic_dossier


def snapshot_for(dossier: Dossier, identifier: str = "a" * 32) -> QuickAnalysisSnapshot:
    if dossier.reconciliation is None:
        reconcile_dossier(dossier)
    now = datetime.now(timezone.utc)
    return QuickAnalysisSnapshot(
        id=identifier,
        created_at=now,
        expires_at=now + timedelta(hours=24),
        duration_seconds=0.125,
        dossier=dossier,
    )


@pytest.fixture
def quick_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, JsonDossierRepository, QuickAnalysisRepository]:
    dossier_repository = JsonDossierRepository(tmp_path / "dossiers")
    analysis_repository = QuickAnalysisRepository(tmp_path / "analyses")
    monkeypatch.setattr(main, "repository", dossier_repository)
    monkeypatch.setattr(main, "analysis_repository", analysis_repository)
    return TestClient(main.app), dossier_repository, analysis_repository


def test_good_understanding_uses_existing_auto_verified_status() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(synthetic_dossier()))

    assert diagnostic["understanding"]["label"] == "Bonne"
    assert diagnostic["workload"]["label"] == "Faible"
    assert diagnostic["counts"]["auto"] == 1
    assert diagnostic["counts"]["contradictions"] == 0


def test_partial_understanding_requires_human_confirmation() -> None:
    dossier = synthetic_dossier(candidates=[candidate("only", 0, 10)])
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))

    assert diagnostic["understanding"]["label"] == "Partielle"
    assert diagnostic["workload"]["label"] == "Moyenne"
    assert diagnostic["counts"]["review"] == 1


def test_weak_understanding_when_no_business_structure_exists() -> None:
    diagnostic = build_quick_diagnostic(
        snapshot_for(synthetic_dossier(candidates=[], texts=[]))
    )

    assert diagnostic["understanding"]["label"] == "Faible"
    assert diagnostic["workload"]["label"] == "Importante"
    assert diagnostic["counts"]["proposals"] == 0


def test_surface_contradiction_is_prioritized() -> None:
    dossier = synthetic_dossier()
    dossier.controle_technique.textes[-1].contenu = "S=12m²"
    reconcile_dossier(dossier)
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))

    assert diagnostic["counts"]["contradictions"] == 1
    assert diagnostic["surface_anomalies"] == 1
    assert diagnostic["anomalies"][0]["level"] == "critical"


def test_surface_categories_reuse_business_candidates() -> None:
    dossier = synthetic_dossier(
        candidates=[
            candidate("principal-a", -1, 4),
            candidate("principal-b", 1, 6),
            candidate("annex", 30, 2, ANNEX),
        ]
    )
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))

    assert diagnostic["categories"]["principale"] == 2
    assert diagnostic["categories"]["annexe"] == 1


def test_many_technical_contours_are_only_counted() -> None:
    candidates = [candidate(f"technical-{index}", index * 2, 1, "DIVERS") for index in range(100)]
    dossier = synthetic_dossier(candidates=candidates, texts=[])
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))

    assert diagnostic["counts"]["contours"] == 100
    assert diagnostic["counts"]["business"] == 0
    assert diagnostic["lots"] == []


def test_unknown_unit_produces_a_diagnostic_instead_of_crashing() -> None:
    control = ControleTechnique(version_dxf="AC1024", unite_detectee="sans_unite")
    planches = [
        Planche(
            id="model",
            titre="Model",
            methode_detection="layout_dxf",
        )
    ]
    dossier = prepare_quick_analysis_dossier(
        "unknown-unit.dxf", "dxf", control, planches
    )
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))

    assert dossier.plan_importe.unite_confirmee is False
    assert diagnostic["understanding"]["label"] == "Faible"
    assert any("unité" in item["message"].lower() for item in diagnostic["anomalies"])


def test_abandoned_planche_is_reported() -> None:
    dossier = synthetic_dossier()
    dossier.planches.append(
        Planche(
            id="abandoned",
            titre="Version abandonnée",
            statut=StatutRevue.ABANDONNEE,
            methode_detection="texte_version_abandonnee_a_valider",
        )
    )
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))

    assert diagnostic["structure"]["abandoned_planches"] == 1
    assert any("abandonnée" in item["message"] for item in diagnostic["anomalies"])


def test_snapshot_is_persistent_across_repository_instances(tmp_path: Path) -> None:
    root = tmp_path / "analyses"
    created = QuickAnalysisRepository(root).create(
        snapshot_for(synthetic_dossier()).dossier,
        0.2,
    )

    restored = QuickAnalysisRepository(root).get(created.id)

    assert restored.dossier.model_dump() == created.dossier.model_dump()
    assert restored.duration_seconds == pytest.approx(0.2)
    assert [path.name for path in root.iterdir()] == [f"{created.id}.json"]


def test_expired_snapshot_is_cleaned(tmp_path: Path) -> None:
    repository = QuickAnalysisRepository(tmp_path / "analyses")
    expired = snapshot_for(synthetic_dossier(), "b" * 32).model_copy(
        update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    )
    repository.root.mkdir(parents=True)
    path = repository.root / f"{expired.id}.json"
    path.write_text(expired.model_dump_json(), encoding="utf-8")

    with pytest.raises(QuickAnalysisNotFoundError):
        repository.get(expired.id)
    assert not path.exists()


def test_promoting_snapshot_preserves_analysis_without_recalculation() -> None:
    original = snapshot_for(synthetic_dossier())
    promoted = promote_quick_analysis(original)

    assert promoted.id != original.dossier.id
    assert promoted.controle_technique == original.dossier.controle_technique
    assert promoted.reconciliation == original.dossier.reconciliation
    assert promoted.plan_importe == original.dossier.plan_importe


def test_quick_analysis_dxf_simple(quick_client, tmp_path: Path) -> None:
    client, _, analysis_repository = quick_client
    source = build_technical_dxf(tmp_path / "simple.dxf")

    with source.open("rb") as upload:
        response = client.post("/analyze", files={"file": (source.name, upload)})

    assert response.status_code == 200
    assert "Analyse terminée" in response.text
    assert "Compréhension du plan" in response.text
    assert "Points à vérifier" in response.text
    assert len(list(analysis_repository.root.glob("*.json"))) == 1


def test_quick_analysis_dwg_uses_existing_conversion_boundary(
    quick_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = quick_client
    converted = build_technical_dxf(tmp_path / "converted.dxf")

    @contextmanager
    def fake_source(source: Path, file_type: str):
        assert file_type == "dwg"
        yield converted

    monkeypatch.setattr(main, "dxf_source", fake_source)
    response = client.post("/analyze", files={"file": ("plan.dwg", b"synthetic")})

    assert response.status_code == 200
    assert "plan.dwg" in response.text
    assert "DWG lisible" in response.text


def test_invalid_quick_analysis_has_a_safe_error(quick_client) -> None:
    client, _, _ = quick_client

    response = client.post("/analyze", files={"file": ("invalid.dxf", b"invalid")})

    assert response.status_code == 422
    assert "Réessayez avec un autre fichier" in response.text
    assert "Traceback" not in response.text


def test_no_zone_is_a_valid_weak_diagnostic(
    quick_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = quick_client
    monkeypatch.setattr(
        main,
        "inspect_dxf",
        lambda path, filename: (
            ControleTechnique(version_dxf="AC1024", unite_detectee="metre"),
            [
                Planche(
                    id="model",
                    titre="Model",
                    methode_detection="layout_dxf",
                )
            ],
        ),
    )

    response = client.post("/analyze", files={"file": ("empty.dxf", b"synthetic")})

    assert response.status_code == 200
    assert "Aucun contour 2D exploitable" in response.text
    assert "Compréhension du plan</span>\n        <strong>Faible" in response.text


def test_auto_verified_lot_and_evidence_are_visible(quick_client) -> None:
    client, _, analysis_repository = quick_client
    stored = analysis_repository.create(
        snapshot_for(synthetic_dossier()).dossier,
        0.1,
    )

    response = client.get(f"/analyses/{stored.id}")

    assert response.status_code == 200
    assert "Lot 1" in response.text
    assert "Auto-vérifié" in response.text
    assert "Pourquoi cette proposition ?" in response.text
    assert "somme contours principaux" in response.text
    assert "zone-a" not in response.text


def test_create_dossier_does_not_parse_a_second_time(
    quick_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, dossier_repository, analysis_repository = quick_client
    source = synthetic_dossier()
    calls = 0

    def fake_inspection(path: Path, filename: str):
        nonlocal calls
        calls += 1
        return source.controle_technique.model_copy(deep=True), [
            item.model_copy(deep=True) for item in source.planches
        ]

    monkeypatch.setattr(main, "inspect_dxf", fake_inspection)
    analyzed = client.post(
        "/analyze",
        files={"file": ("reuse.dxf", b"synthetic")},
        follow_redirects=False,
    )
    analysis_id = analyzed.headers["location"].rsplit("/", 1)[-1]

    created = client.post(
        f"/analyses/{analysis_id}/dossier",
        follow_redirects=False,
    )

    assert created.status_code == 303
    assert calls == 1
    [dossier] = dossier_repository.list()
    assert dossier.controle_technique is not None
    with pytest.raises(QuickAnalysisNotFoundError):
        analysis_repository.get(analysis_id)


def test_delete_analysis_removes_snapshot(quick_client) -> None:
    client, _, analysis_repository = quick_client
    stored = analysis_repository.create(
        snapshot_for(synthetic_dossier()).dossier,
        0.1,
    )

    response = client.post(
        f"/analyses/{stored.id}/delete",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?analysis_deleted=1"
    with pytest.raises(QuickAnalysisNotFoundError):
        analysis_repository.get(stored.id)


def test_uploaded_filename_is_sanitized(quick_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, analysis_repository = quick_client
    source = synthetic_dossier()
    monkeypatch.setattr(
        main,
        "inspect_dxf",
        lambda path, filename: (
            source.controle_technique.model_copy(deep=True),
            [item.model_copy(deep=True) for item in source.planches],
        ),
    )

    response = client.post(
        "/analyze",
        files={"file": ("..\\..\\private-plan.dxf", b"synthetic")},
    )

    assert response.status_code == 200
    [stored_path] = analysis_repository.root.glob("*.json")
    stored = analysis_repository.get(stored_path.stem)
    assert stored.dossier.plan_importe.nom_fichier_original == "private-plan.dxf"
    assert ".." not in response.text


def test_template_is_responsive_and_keeps_technical_details_secondary(
    quick_client,
) -> None:
    client, _, analysis_repository = quick_client
    stored = analysis_repository.create(
        snapshot_for(synthetic_dossier()).dossier,
        0.1,
    )

    response = client.get(f"/analyses/{stored.id}")

    assert '<meta name="viewport"' in response.text
    assert '<details class="panel technical-diagnostic">' in response.text
    assert "Millièmes" in response.text
    assert "non analysés dans l’analyse rapide" in response.text.casefold()


def test_index_presents_analysis_as_diagnostic(quick_client) -> None:
    client, _, _ = quick_client

    response = client.get("/")

    assert "Pré-diagnostic autonome" in response.text
    assert "Obtenir le diagnostic" in response.text
    assert "Analyse rapide v0.2" not in response.text
