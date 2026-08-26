from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import Dossier, Generation, StatutValidationDonnees
from app.storage import DossierNotFoundError, JsonDossierRepository
from app.workflow import record_data_validation

from .test_v03_technical import build_technical_dxf


FIXTURE = Path(__file__).parent / "fixtures" / "copropriete_complete.json"


@pytest.fixture
def assistant_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, JsonDossierRepository]:
    repository = JsonDossierRepository(tmp_path / "data")
    monkeypatch.setattr(main, "repository", repository)
    return TestClient(main.app), repository


def create_dossier(client: TestClient, reference: str = "UX-TEST") -> tuple[str, str]:
    response = client.post(
        "/dossiers",
        data={"reference": reference, "dossier_type": "copropriete"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/plan")
    dossier_id = response.headers["location"].split("/")[-2]
    return dossier_id, f"/dossiers/{dossier_id}"


def import_plan(client: TestClient, base_url: str, source: Path) -> None:
    with source.open("rb") as upload:
        response = client.post(
            f"{base_url}/import",
            files={"file": (source.name, upload, "application/dxf")},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"{base_url}/plan"


def complete_dossier() -> Dossier:
    return Dossier.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_creation_redirects_to_plan_and_displays_six_consistent_steps(
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, _ = assistant_client
    _, base_url = create_dossier(client)
    page = client.get(f"{base_url}/plan")

    assert page.status_code == 200
    for label in (
        "1. Dossier",
        "2. Plan",
        "3. Contrôle",
        "4. Lots &amp; surfaces",
        "5. Synthèse",
        "6. Document",
    ):
        assert label in page.text
    assert 'aria-current="step"' in page.text
    assert "Précédent" in page.text
    assert "Suivant" in page.text


def test_future_steps_are_rejected_before_plan_import(
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, _ = assistant_client
    _, base_url = create_dossier(client)

    for route in ("controle", "lots", "synthese", "documents"):
        response = client.get(f"{base_url}/{route}")
        assert response.status_code == 409
    plan = client.get(f"{base_url}/plan")
    assert '<button type="button" disabled>Suivant</button>' in plan.text


def test_import_accepts_detected_unit_without_extra_confirmation(
    tmp_path: Path,
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier_id, base_url = create_dossier(client)
    source = build_technical_dxf(tmp_path / "assistant.dxf")
    import_plan(client, base_url, source)

    dossier = repository.get(dossier_id)
    assert dossier.plan_importe.unite_confirmee is True
    assert dossier.plan_importe.unite_retenue == dossier.plan_importe.unite_detectee
    decision = next(item for item in dossier.validations if item.champ == "unite_du_plan")
    assert decision.propose == decision.retenu
    assert decision.justification is None

    control = client.get(f"{base_url}/controle")
    assert control.status_code == 200
    assert "Mettre à jour" not in control.text
    assert ">Valider<" not in control.text


def test_unit_autosave_requires_justification_only_for_a_correction(
    tmp_path: Path,
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier_id, base_url = create_dossier(client)
    import_plan(client, base_url, build_technical_dxf(tmp_path / "units.dxf"))

    unchanged = client.post(
        f"{base_url}/unite",
        data={"unit": "metre", "justification": ""},
        headers={"accept": "application/json"},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["message"] == "Enregistré"

    rejected = client.post(
        f"{base_url}/unite",
        data={"unit": "millimetre", "justification": ""},
        headers={"accept": "application/json"},
    )
    assert rejected.status_code == 400
    assert "justification" in rejected.json()["message"].lower()
    assert repository.get(dossier_id).plan_importe.unite_retenue == "metre"

    corrected = client.post(
        f"{base_url}/unite",
        data={
            "unit": "millimetre",
            "justification": "Le cartouche confirme les millimètres.",
        },
        headers={"accept": "application/json"},
    )
    assert corrected.status_code == 200
    restored = repository.get(dossier_id)
    assert restored.plan_importe.unite_retenue == "millimetre"
    assert restored.validations[-1].justification


def test_planche_and_layer_choices_are_autosaved_and_survive_navigation(
    tmp_path: Path,
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier_id, base_url = create_dossier(client)
    import_plan(client, base_url, build_technical_dxf(tmp_path / "choices.dxf"))
    dossier = repository.get(dossier_id)
    planche = dossier.planches[0]
    layer = dossier.controle_technique.calques[0]

    planche_response = client.post(
        f"{base_url}/planches/{planche.id}",
        data={"status": "retenue"},
        headers={"accept": "application/json"},
    )
    layer_response = client.post(
        f"{base_url}/calques",
        data={"layer_name": layer.nom, "status": "exclue"},
        headers={"accept": "application/json"},
    )
    assert planche_response.status_code == layer_response.status_code == 200

    client.get(f"{base_url}/plan")
    control = client.get(f"{base_url}/controle")
    assert control.status_code == 200
    restored = repository.get(dossier_id)
    assert restored.planches[0].statut.value == "retenue"
    assert restored.controle_technique.calques[0].statut.value == "exclue"
    assert 'class="small">Valider' not in control.text


def test_zone_association_persists_and_surface_correction_still_needs_reason(
    tmp_path: Path,
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier_id, base_url = create_dossier(client)
    import_plan(client, base_url, build_technical_dxf(tmp_path / "zones.dxf"))
    dossier = repository.get(dossier_id)
    first, second = dossier.controle_technique.zones_candidates[:2]

    associated = client.post(
        f"{base_url}/zones/{first.id}",
        data={
            "building_code": "A",
            "level_code": "RdC",
            "lot_number": "1",
            "category": "principale",
            "retained_surface": str(first.surface_geometrique_m2),
            "justification": "",
        },
        follow_redirects=False,
    )
    assert associated.status_code == 303
    assert repository.get(dossier_id).lots[0].zone_ids

    rejected = client.post(
        f"{base_url}/zones/{second.id}",
        data={
            "building_code": "A",
            "level_code": "Sous-sol",
            "lot_number": "1",
            "category": "secondaire_annexe",
            "retained_surface": str(second.surface_geometrique_m2 - 1),
            "justification": "",
        },
    )
    assert rejected.status_code == 400
    restored = repository.get(dossier_id)
    assert len(restored.zones) == 1
    assert len(restored.lots) == 1


def test_lots_page_filters_and_paginates_large_candidate_sets(
    tmp_path: Path,
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier_id, base_url = create_dossier(client)
    import_plan(client, base_url, build_technical_dxf(tmp_path / "many.dxf"))
    dossier = repository.get(dossier_id)
    source_candidate = dossier.controle_technique.zones_candidates[0]
    dossier.controle_technique.zones_candidates = [
        source_candidate.model_copy(
            deep=True,
            update={
                "id": f"candidate-{index:03d}",
                "handle_dxf": f"H{index:03d}",
                "calque": "Poubelle" if index == 59 else f"LOT-{index % 3}",
            },
        )
        for index in range(60)
    ]
    repository.save(dossier)

    first_page = client.get(f"{base_url}/lots?assignment=all")
    second_page = client.get(f"{base_url}/lots?assignment=all&page=2")
    filtered = client.get(f"{base_url}/lots?assignment=all&layer=Poubelle")
    assert first_page.status_code == second_page.status_code == filtered.status_code == 200
    assert first_page.text.count('<article class="candidate') == 25
    assert second_page.text.count('<article class="candidate') == 25
    assert "Page 1 sur 3" in first_page.text
    assert "candidate-059" in filtered.text
    assert "1 résultat(s)" in filtered.text


def test_summary_reuses_blockers_and_final_validation_rejects_incomplete_data(
    tmp_path: Path,
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, _ = assistant_client
    _, base_url = create_dossier(client)
    import_plan(client, base_url, build_technical_dxf(tmp_path / "blocked.dxf"))

    summary = client.get(f"{base_url}/synthese")
    validation = client.post(
        f"{base_url}/synthese/validate", follow_redirects=False
    )
    assert summary.status_code == 200
    assert "Aucune planche n&#39;est marquée comme retenue." in summary.text
    assert "Aucune surface retenue n&#39;est disponible." in summary.text
    assert validation.status_code == 409
    assert "validation est impossible" in validation.text


def test_successful_final_validation_unlocks_document(
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier = complete_dossier()
    repository.save(dossier)

    locked = client.get(f"/dossiers/{dossier.id}/documents")
    validated = client.post(
        f"/dossiers/{dossier.id}/synthese/validate",
        follow_redirects=False,
    )
    restored = repository.get(dossier.id)
    document = client.get(f"/dossiers/{dossier.id}/documents")

    assert locked.status_code == 409
    assert validated.status_code == 303
    assert validated.headers["location"] == f"/dossiers/{dossier.id}/documents"
    assert restored.statut_validation_donnees == StatutValidationDonnees.VALIDE
    assert restored.sha256_validation_donnees
    assert document.status_code == 200
    assert "Générer le brouillon Word" in document.text


def test_change_after_validation_requires_revalidation_and_keeps_history(
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier = complete_dossier()
    dossier.generations.append(
        Generation(
            id="historique-v04",
            type_document="etat_descriptif_copropriete",
            template_id="copropriete_draft_v1",
            template_version="1.0",
            sha256_snapshot="a" * 64,
            nom_fichier="historique.docx",
        )
    )
    record_data_validation(dossier)
    repository.save(dossier)
    planche = dossier.planches[0]

    response = client.post(
        f"/dossiers/{dossier.id}/planches/{planche.id}",
        data={"status": "retenue"},
        headers={"accept": "application/json"},
    )
    restored = repository.get(dossier.id)

    assert response.status_code == 200
    assert restored.statut_validation_donnees == StatutValidationDonnees.A_VALIDER
    assert restored.sha256_validation_donnees is None
    assert [item.id for item in restored.generations] == ["historique-v04"]
    assert client.get(f"/dossiers/{dossier.id}/documents").status_code == 409


def test_autosave_rejects_unknown_dossier(
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, _ = assistant_client
    response = client.post(
        "/dossiers/missing/calques",
        data={"layer_name": "LOT", "status": "retenue"},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 404


def test_trash_action_deletes_json_and_generated_artifacts(
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    dossier = Dossier(id="delete-me", reference="À SUPPRIMER")
    repository.save(dossier)
    repository.save_generation_artifacts(
        dossier.id,
        "generation-test",
        "brouillon.docx",
        b"docx",
        b"{}\n",
    )
    generated_root = repository.root / "dossiers" / dossier.id

    index = client.get("/")
    assert 'aria-label="Supprimer le dossier À SUPPRIMER"' in index.text
    assert "data-delete-button" in index.text

    deleted = client.post(
        f"/dossiers/{dossier.id}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/?deleted=1"
    assert not generated_root.exists()
    with pytest.raises(DossierNotFoundError):
        repository.get(dossier.id)


def test_delete_unknown_or_unsafe_dossier_is_refused(
    assistant_client: tuple[TestClient, JsonDossierRepository],
) -> None:
    client, repository = assistant_client
    assert client.post("/dossiers/missing/delete").status_code == 404
    with pytest.raises(DossierNotFoundError):
        repository.delete("../private")
