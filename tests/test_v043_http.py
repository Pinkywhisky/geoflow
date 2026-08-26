from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import Dossier, StatutValidationDonnees
from app.storage import JsonDossierRepository
from app.workflow import record_data_validation


FIXTURE = Path(__file__).parent / "fixtures" / "copropriete_complete.json"


@pytest.fixture
def business_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, JsonDossierRepository, str]:
    repository = JsonDossierRepository(tmp_path / "data")
    monkeypatch.setattr(main, "repository", repository)
    dossier = Dossier.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    repository.save(dossier)
    return TestClient(main.app), repository, dossier.id


def test_dossier_page_exposes_address_date_and_multiple_parcels(
    business_client,
) -> None:
    client, _, dossier_id = business_client
    response = client.get(f"/dossiers/{dossier_id}/dossier")

    assert response.status_code == 200
    assert 'name="voie"' in response.text
    assert 'name="code_postal"' in response.text
    assert 'name="date_plan"' in response.text
    assert response.text.count('class="parcel-row"') == 2
    assert "Sauvegarde automatique" in response.text


def test_address_autosave_updates_canonical_data_and_invalidates(
    business_client,
) -> None:
    client, repository, dossier_id = business_client
    dossier = repository.get(dossier_id)
    record_data_validation(dossier)
    repository.save(dossier)

    response = client.post(
        f"/dossiers/{dossier_id}/dossier/details",
        data={
            "numero": "18",
            "voie": "rue du Plan",
            "complement": "Bâtiment C",
            "code_postal": "75001",
            "commune": "Paris",
            "departement": "Paris",
            "date_plan": "2026-08-20",
        },
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Enregistré"
    restored = repository.get(dossier_id)
    assert restored.adresse.numero == "18"
    assert restored.adresse.voie == "rue du Plan"
    assert restored.adresse.code_postal == "75001"
    assert restored.statut_validation_donnees == StatutValidationDonnees.A_VALIDER


def test_parcel_can_be_added_edited_and_deleted(business_client) -> None:
    client, repository, dossier_id = business_client
    added = client.post(
        f"/dossiers/{dossier_id}/cadastre",
        data={"commune": "Paris", "section": "AO", "numero": "163"},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert len(repository.get(dossier_id).references_cadastrales) == 3

    edited = client.post(
        f"/dossiers/{dossier_id}/cadastre/2",
        data={"commune": "Paris", "section": "AO", "numero": "164"},
        headers={"Accept": "application/json"},
    )
    assert edited.status_code == 200
    assert repository.get(dossier_id).references_cadastrales[2].numero == "164"

    deleted = client.post(
        f"/dossiers/{dossier_id}/cadastre/2/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert len(repository.get(dossier_id).references_cadastrales) == 2


def test_unknown_parcel_is_rejected_without_traceback(business_client) -> None:
    client, _, dossier_id = business_client
    response = client.post(
        f"/dossiers/{dossier_id}/cadastre/99/delete",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["status"] == "error"


def test_lots_page_exposes_lot_centric_business_fields(
    business_client,
) -> None:
    client, _, dossier_id = business_client
    response = client.get(
        f"/dossiers/{dossier_id}/lots?proposal_status=all"
    )

    assert response.status_code == 200
    assert 'name="usage"' in response.text
    assert 'name="designation"' in response.text
    assert "Millièmes généraux" in response.text
    assert response.text.count('class="lot-metadata-card"') == 2


def test_lot_metadata_and_millieme_autosave(business_client) -> None:
    client, repository, dossier_id = business_client
    response = client.post(
        f"/dossiers/{dossier_id}/lots/lot-1/metadata",
        data={
            "usage": "Appartement",
            "designation": "Appartement comprenant entrée et séjour.",
            "millieme_value": "400",
            "millieme_base": "1000",
            "millieme_status": "valide",
        },
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    restored = repository.get(dossier_id)
    assert restored.lots[0].usage == "Appartement"
    assert restored.lots[0].designation.startswith("Appartement comprenant")
    assert restored.milliemes[0].valide


def test_validated_complete_grid_total_is_1000(business_client) -> None:
    client, repository, dossier_id = business_client
    for lot_id, value in (("lot-1", "400"), ("lot-2", "600")):
        response = client.post(
            f"/dossiers/{dossier_id}/lots/{lot_id}/metadata",
            data={
                "usage": "Appartement",
                "designation": f"Désignation {lot_id}",
                "millieme_value": value,
                "millieme_base": "1000",
                "millieme_status": "valide",
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
    response = client.post(
        f"/dossiers/{dossier_id}/milliemes/status",
        data={"complete": "true"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    restored = repository.get(dossier_id)
    assert restored.grille_milliemes_complete
    assert sum(item.valeur for item in restored.milliemes) == 1000

    summary = client.get(f"/dossiers/{dossier_id}/synthese")
    assert "Compléter les millièmes" not in summary.text


def test_summary_warnings_link_to_editable_steps(business_client) -> None:
    client, repository, dossier_id = business_client
    dossier = repository.get(dossier_id)
    dossier.adresse = None
    dossier.references_cadastrales = []
    dossier.milliemes = []
    for lot in dossier.lots:
        lot.usage = None
        lot.designation = None
    repository.save(dossier)

    response = client.get(f"/dossiers/{dossier_id}/synthese")
    assert f"/dossiers/{dossier_id}/dossier#adresse" in response.text
    assert f"/dossiers/{dossier_id}/dossier#cadastre" in response.text
    assert f"/dossiers/{dossier_id}/lots#donnees-lots" in response.text
    assert f"/dossiers/{dossier_id}/lots#milliemes" in response.text
    assert "Fonctionnalité non disponible dans cette version" in response.text


def test_metadata_change_after_validation_returns_to_review(
    business_client,
) -> None:
    client, repository, dossier_id = business_client
    dossier = repository.get(dossier_id)
    record_data_validation(dossier)
    repository.save(dossier)

    response = client.post(
        f"/dossiers/{dossier_id}/lots/lot-1/metadata",
        data={
            "usage": "Bureau",
            "designation": "Bureau au rez-de-chaussée.",
            "millieme_value": "",
            "millieme_base": "1000",
            "millieme_status": "a_confirmer",
        },
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert (
        repository.get(dossier_id).statut_validation_donnees
        == StatutValidationDonnees.A_VALIDER
    )
