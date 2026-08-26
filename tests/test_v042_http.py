from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import VerificationStatus
from app.reconciliation import reconcile_dossier
from app.storage import JsonDossierRepository

from .test_v042_reconciliation import synthetic_dossier


@pytest.fixture
def reconciliation_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, JsonDossierRepository, str]:
    repository = JsonDossierRepository(tmp_path / "data")
    monkeypatch.setattr(main, "repository", repository)
    dossier = synthetic_dossier()
    reconcile_dossier(dossier)
    repository.save(dossier)
    return TestClient(main.app), repository, dossier.id


def test_lots_page_is_lot_centric_by_default(
    reconciliation_client,
) -> None:
    client, _, dossier_id = reconciliation_client
    response = client.get(
        f"/dossiers/{dossier_id}/lots?proposal_status=all"
    )
    assert response.status_code == 200
    assert "Contours analysés" in response.text
    assert "Zones métier possibles" in response.text
    assert "Lots proposés" in response.text
    assert '<article class="lot-proposal' in response.text
    assert '<article class="candidate' not in response.text


def test_auto_verified_proposal_exposes_explanation(
    reconciliation_client,
) -> None:
    client, _, dossier_id = reconciliation_client
    response = client.get(
        f"/dossiers/{dossier_id}/lots?proposal_status=auto_verifie"
    )
    assert response.status_code == 200
    assert "Auto-vérifié" in response.text
    assert "Pourquoi cette proposition ?" in response.text
    assert "somme contours principaux" in response.text
    assert "surface annotee" in response.text


def test_technical_contours_are_in_separate_view(
    reconciliation_client,
) -> None:
    client, _, dossier_id = reconciliation_client
    response = client.get(
        f"/dossiers/{dossier_id}/lots?view=technical&assignment=all"
    )
    assert response.status_code == 200
    assert "Détails techniques" in response.text
    assert response.text.count('<article class="candidate') == 2
    assert '<article class="lot-proposal' not in response.text


def test_auto_proposal_can_be_marked_for_review(
    reconciliation_client,
) -> None:
    client, repository, dossier_id = reconciliation_client
    response = client.post(
        f"/dossiers/{dossier_id}/lots/proposition-lot-1/review",
        data={"reason": "Le repère doit être comparé au plan signé."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    restored = repository.get(dossier_id)
    [proposal] = restored.reconciliation.lot_proposals
    assert proposal.statut == VerificationStatus.A_REVOIR
    assert proposal.statut_automatique == VerificationStatus.AUTO_VERIFIE
    assert proposal.decision_manuelle.motif


def test_review_requires_a_reason(
    reconciliation_client,
) -> None:
    client, repository, dossier_id = reconciliation_client
    response = client.post(
        f"/dossiers/{dossier_id}/lots/proposition-lot-1/review",
        data={"reason": "  "},
    )
    assert response.status_code == 400
    assert "motif" in response.text.lower()
    assert (
        repository.get(dossier_id)
        .reconciliation.lot_proposals[0]
        .statut
        == VerificationStatus.AUTO_VERIFIE
    )


def test_confirm_proposal_creates_one_lot_with_selected_zones(
    reconciliation_client,
) -> None:
    client, repository, dossier_id = reconciliation_client
    response = client.post(
        f"/dossiers/{dossier_id}/lots/proposition-lot-1/confirm",
        data={
            "candidate_ids": ["zone-a", "zone-b"],
            "building_code": "A",
            "level_code": "RdC",
            "lot_number": "1",
            "category": "principale",
            "reason": "Concordance vérifiée visuellement sur le plan signé.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    restored = repository.get(dossier_id)
    assert len(restored.lots) == 1
    assert len(restored.zones) == 2
    [proposal] = restored.reconciliation.lot_proposals
    assert proposal.statut == VerificationStatus.CONFIRME_MANUEL
    assert proposal.statut_automatique == VerificationStatus.AUTO_VERIFIE
    assert proposal.decision_manuelle.candidate_zone_ids_retenus == [
        "zone-a",
        "zone-b",
    ]


def test_confirmation_can_correct_number_and_zone_selection(
    reconciliation_client,
) -> None:
    client, repository, dossier_id = reconciliation_client
    response = client.post(
        f"/dossiers/{dossier_id}/lots/proposition-lot-1/confirm",
        data={
            "candidate_ids": ["zone-a"],
            "building_code": "B",
            "level_code": "R+1",
            "lot_number": "42",
            "category": "principale",
            "reason": "Le plan signé ne retient qu'un des deux contours.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    restored = repository.get(dossier_id)
    assert restored.lots[0].numero == "42"
    assert len(restored.zones) == 1
    decision = restored.reconciliation.lot_proposals[0].decision_manuelle
    assert decision.numero_retenu == "42"
    assert decision.surface_retenue_m2 == pytest.approx(4)


def test_contradiction_is_visible_and_blocks_summary_validation(
    reconciliation_client,
) -> None:
    client, repository, dossier_id = reconciliation_client
    dossier = repository.get(dossier_id)
    dossier.controle_technique.textes[-1].contenu = "S=12m²"
    reconcile_dossier(dossier)
    repository.save(dossier)

    lots = client.get(
        f"/dossiers/{dossier_id}/lots?proposal_status=contradictoire"
    )
    validation = client.post(
        f"/dossiers/{dossier_id}/synthese/validate",
        follow_redirects=False,
    )
    assert "Contradictoire" in lots.text
    assert validation.status_code == 409
    assert "Réconciliation" in validation.text


def test_export_contains_rules_evidence_and_global_checks(
    reconciliation_client,
) -> None:
    client, _, dossier_id = reconciliation_client
    payload = client.get(f"/dossiers/{dossier_id}/export").json()
    reconciliation = payload["reconciliation"]
    assert reconciliation["version_regles"] == "1.0.0"
    assert reconciliation["preuves"]
    assert reconciliation["controles_globaux"]
