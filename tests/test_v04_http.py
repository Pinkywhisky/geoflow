from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

import app.main as main
from app.documents.generator import DOCUMENT_MIME
from app.domain import Dossier
from app.storage import JsonDossierRepository
from app.workflow import record_data_validation


FIXTURE = Path(__file__).parent / "fixtures" / "copropriete_complete.json"


@pytest.fixture
def document_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, JsonDossierRepository]:
    repository = JsonDossierRepository(tmp_path / "data")
    monkeypatch.setattr(main, "repository", repository)
    return TestClient(main.app), repository


def complete_dossier() -> Dossier:
    dossier = Dossier.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    record_data_validation(dossier)
    return dossier


def test_generation_page_shows_read_only_summary(
    document_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = document_client
    dossier = complete_dossier()
    repository.save(dossier)
    response = client.get(f"/dossiers/{dossier.id}/documents")
    assert response.status_code == 200
    assert dossier.reference in response.text
    assert "2" in response.text
    assert "182.00 m²" in response.text
    assert "Générer le brouillon Word" in response.text
    assert "ne relit ni le DXF ni le DWG" in response.text


def test_generation_page_for_unknown_dossier_is_404(
    document_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, _ = document_client
    response = client.get("/dossiers/missing/documents")
    assert response.status_code == 404


def test_incomplete_dossier_page_lists_blockers(
    document_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = document_client
    dossier = Dossier(id="incomplete", reference="DEMO-INCOMPLETE")
    repository.save(dossier)
    response = client.get(f"/dossiers/{dossier.id}/documents")
    assert response.status_code == 409
    assert "Dossier à valider avant génération" in response.text
    assert "Retour à la synthèse" in response.text


def test_blocked_generation_does_not_create_an_artifact(
    document_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = document_client
    dossier = Dossier(id="blocked", reference="DEMO-BLOCKED")
    repository.save(dossier)
    response = client.post(
        f"/dossiers/{dossier.id}/documents/generate",
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "Dossier à valider avant génération" in response.text
    assert not (repository.root / "dossiers" / dossier.id).exists()


def test_valid_generation_is_persisted_and_downloadable(
    document_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = document_client
    dossier = complete_dossier()
    repository.save(dossier)
    response = client.post(
        f"/dossiers/{dossier.id}/documents/generate",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/dossiers/{dossier.id}/documents"

    restored = repository.get(dossier.id)
    assert restored.statut == "document_brouillon"
    assert len(restored.generations) == 1
    generation = restored.generations[0]
    download = client.get(
        f"/dossiers/{dossier.id}/documents/{generation.id}/download"
    )
    assert download.status_code == 200
    assert download.headers["content-type"] == DOCUMENT_MIME
    disposition = download.headers["content-disposition"]
    assert "attachment" in disposition
    assert generation.nom_fichier in disposition
    document = Document(BytesIO(download.content))
    assert document.core_properties.author == "GeoFlow"


def test_unknown_generation_cannot_select_an_arbitrary_path(
    document_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = document_client
    dossier = complete_dossier()
    repository.save(dossier)
    response = client.get(
        f"/dossiers/{dossier.id}/documents/not-a-generation/download"
    )
    assert response.status_code == 404
    encoded_traversal = client.get(
        f"/dossiers/{dossier.id}/documents/%2E%2E/download"
    )
    assert encoded_traversal.status_code in {404, 405}


def test_non_copropriete_creation_remains_rejected(
    document_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, _ = document_client
    response = client.post(
        "/dossiers",
        data={"reference": "DEMO-OTHER", "dossier_type": "autre"},
    )
    assert response.status_code == 400
    assert "Seuls les dossiers de copropriete" in response.text
