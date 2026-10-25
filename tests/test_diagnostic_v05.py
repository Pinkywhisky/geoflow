"""Diagnostic page redesign and editable business reference."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import Generation, Planche, StatutValidationDonnees, VerificationStatus
from app.domain.models import StatutRevue
from app.quick_analysis import (
    QuickAnalysisRepository,
    build_quick_diagnostic,
    prepare_quick_analysis_dossier,
)
from app.reconciliation import reconcile_dossier
from app.storage import JsonDossierRepository
from app.workflow import clean_reference, create_dossier, record_data_validation

from .test_quick_analysis import snapshot_for
from .test_v042_reconciliation import (
    ANNEX,
    SURFACE_LAYER,
    candidate,
    synthetic_dossier,
    text,
)

ROOT = Path(__file__).parents[1]


@pytest.fixture
def client_and_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dossiers = JsonDossierRepository(tmp_path / "dossiers")
    analyses = QuickAnalysisRepository(tmp_path / "analyses")
    monkeypatch.setattr(main, "repository", dossiers)
    monkeypatch.setattr(main, "analysis_repository", analyses)
    return TestClient(main.app), dossiers, analyses


def contradictory_dossier():
    dossier = synthetic_dossier()
    dossier.controle_technique.textes[-1].contenu = "S=12m²"
    reconcile_dossier(dossier)
    return dossier


def unresolved_dossier():
    dossier = synthetic_dossier()
    dossier.controle_technique.textes = [
        item for item in dossier.controle_technique.textes if item.calque != SURFACE_LAYER
    ]
    reconcile_dossier(dossier)
    return dossier


def page_for(client: TestClient, analyses: QuickAnalysisRepository, dossier) -> str:
    stored = analyses.create(snapshot_for(dossier).dossier, 1.84)
    response = client.get(f"/analyses/{stored.id}")
    assert response.status_code == 200
    return response.text


# 1-3. Understanding level drives the narrative and the main action.


def test_good_diagnostic_offers_dossier_creation_first() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(synthetic_dossier()))
    assert diagnostic["understanding"]["key"] == "bonne"
    assert diagnostic["conclusion"] == "GeoFlow reconnaît correctement la structure générale du plan."
    assert diagnostic["actions"] == {"create_first": True}


def test_partial_diagnostic_sends_to_points_then_creation() -> None:
    dossier = synthetic_dossier(candidates=[candidate("only", 0, 10)])
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))
    assert diagnostic["understanding"]["key"] == "partielle"
    assert diagnostic["actions"]["label"] == "Examiner les points à vérifier"
    assert diagnostic["actions"]["secondary_label"] == "Créer le dossier"


def test_weak_diagnostic_explains_and_still_allows_creation(client_and_repos) -> None:
    client, _, analyses = client_and_repos
    dossier = synthetic_dossier(candidates=[candidate("divers", 0, 3, "DIVERS")], texts=[])
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))
    assert diagnostic["understanding"]["key"] == "faible"
    assert diagnostic["actions"]["label"] == "Voir pourquoi"
    assert "aucun calque ne correspond" in diagnostic["conclusion"]
    page = page_for(client, analyses, dossier)
    assert "Créer le dossier quand même" in page
    assert 'action="/analyses/' in page and "/dossier\"" in page


# 4-5. Narrative and main counters come from existing statuses only.


def test_narrative_states_auto_verified_share_and_surface_gaps() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(contradictory_dossier()))
    assert diagnostic["facts"] == ["0 lot(s) sur 1 sont auto-vérifiés ; 1 avec un écart de surface."]
    assert diagnostic["duration"] == "0,1 s"


def test_main_counters_never_use_technical_contours() -> None:
    candidates = [candidate("zone-a", -1, 4), candidate("zone-b", 1, 6)] + [
        candidate(f"tech-{index}", 100 + index * 3, 1, "DIVERS") for index in range(40)
    ]
    diagnostic = build_quick_diagnostic(snapshot_for(synthetic_dossier(candidates=candidates)))
    values = {item["key"]: item["value"] for item in diagnostic["key_counts"]}
    assert values == {"proposals": 1, "auto": 1, "confirm": 0, "contradictions": 0}
    assert all(item["value"] != 42 for item in diagnostic["key_counts"])
    assert diagnostic["counts"]["contours"] == 42


def test_unresolved_counter_appears_only_when_needed() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(unresolved_dossier()))
    values = {item["key"]: item["value"] for item in diagnostic["key_counts"]}
    assert values["unresolved"] == 1


# 6-8. Human workload keeps the existing rules and explains them.


def test_low_workload_reason() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(synthetic_dossier()))
    assert diagnostic["workload"]["key"] == "faible"
    assert diagnostic["workload_reason"] == "Aucune association à reprendre avant la création du dossier."


def test_medium_workload_reason_lists_lots_to_confirm() -> None:
    dossier = synthetic_dossier(candidates=[candidate("only", 0, 10)])
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))
    assert diagnostic["workload"]["key"] == "moyenne"
    assert diagnostic["workload_reason"] == "1 lot(s) à confirmer."


def test_high_workload_reason_without_lots() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(synthetic_dossier(candidates=[], texts=[])))
    assert diagnostic["workload"]["key"] == "importante"
    assert "n’a pas pu proposer de lots" in diagnostic["workload_reason"]


def test_annexes_are_reported_apart_from_unmatched_principal_surfaces() -> None:
    dossier = synthetic_dossier(
        candidates=[candidate("zone-a", -1, 4), candidate("zone-b", 1, 6), candidate("annex", 30, 2, ANNEX)]
    )
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))
    assert diagnostic["counts"]["unmatched_annex"] == 1
    assert diagnostic["counts"]["unmatched_principal"] == 0
    titles = [item["title"] for item in diagnostic["checks"]]
    assert "1 annexe à rattacher" in titles
    assert not any("surface principale" in title for title in titles)


# 9. Points to check come before the lot list.


def test_contradiction_is_shown_before_lots(client_and_repos) -> None:
    client, _, analyses = client_and_repos
    page = page_for(client, analyses, contradictory_dossier())
    assert page.index("1 écart de surface") < page.index('id="lots-title"') < page.index("Lot 1")
    assert "À corriger" in page


# 10-13. Lot cards and their "Pourquoi ?" explanation.


def test_auto_verified_lot_has_only_positive_reasons() -> None:
    [row] = build_quick_diagnostic(snapshot_for(synthetic_dossier()))["lots"]
    assert row["status_label"] == "Auto-vérifié"
    assert row["reasons"] and all(reason["positive"] for reason in row["reasons"])
    assert {"title": "Model", "surface": 10.0} in row["planche_surfaces"]


def test_lot_to_confirm_explains_the_missing_condition() -> None:
    dossier = synthetic_dossier(candidates=[candidate("only", 0, 10)])
    [row] = build_quick_diagnostic(snapshot_for(dossier))["lots"]
    assert row["proposal"].statut == VerificationStatus.A_CONFIRMER
    negatives = [reason["text"] for reason in row["reasons"] if not reason["positive"]]
    assert negatives == ["Un seul contour rattaché : la vérification automatique en exige au moins 2"]


def test_unretained_planche_is_the_reason_to_confirm() -> None:
    dossier = synthetic_dossier(planche_status=StatutRevue.CANDIDATE)
    [row] = build_quick_diagnostic(snapshot_for(dossier))["lots"]
    assert "Contour situé sur une planche non retenue" in [reason["text"] for reason in row["reasons"]]


def test_unresolved_lot_is_listed_with_its_cause() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(unresolved_dossier()))
    [row] = diagnostic["lots"]
    assert row["status_label"] == "Non résolu"
    assert "Aucune surface annotée trouvée près du repère" in [reason["text"] for reason in row["reasons"]]
    assert diagnostic["checks"][0]["title"] == "1 lot non résolu"


def test_surface_gap_is_a_negative_reason_and_a_surface_flag(client_and_repos) -> None:
    client, _, analyses = client_and_repos
    dossier = contradictory_dossier()
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))
    [row] = diagnostic["lots"]
    assert any(not reason["positive"] and reason["text"].startswith("Écart de -2,00 m²") for reason in row["reasons"])
    assert diagnostic["surface_anomalies"] == 1
    page = page_for(client, analyses, dossier)
    assert "Contours mesurés" in page and "Annotation sur le plan" in page
    assert "1 surface(s) à vérifier" in page


# 14-16. Structure, technical details and hidden identifiers.


def test_undetermined_structure_is_not_invented(client_and_repos) -> None:
    client, _, analyses = client_and_repos
    page = page_for(client, analyses, synthetic_dossier())
    assert re.search(r"<dt>Bâtiments</dt><dd>Non déterminés</dd>", page)
    assert re.search(r"<dt>Niveaux</dt><dd>Non déterminés</dd>", page)


def test_technical_details_are_collapsed_and_identifiers_hidden(client_and_repos) -> None:
    client, _, analyses = client_and_repos
    page = page_for(client, analyses, synthetic_dossier())
    assert '<details class="panel technical-diagnostic">' in page
    for identifier in ("zone-a", "zone-b", "proposition-lot-1", "Handle", "preuve-"):
        assert identifier not in page


def test_abandoned_versions_are_explained() -> None:
    dossier = synthetic_dossier()
    dossier.planches.append(
        Planche(
            id="old",
            titre="Ancienne version",
            statut=StatutRevue.ABANDONNEE,
            methode_detection="texte_version_abandonnee_a_valider",
        )
    )
    dossier.controle_technique.zones_candidates.append(candidate("old-zone", 50, 8, planche="old"))
    reconcile_dossier(dossier)
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))
    assert diagnostic["versions"] == {"abandoned": ["Ancienne version"], "excluded_contours": 1}
    check = next(item for item in diagnostic["checks"] if "version abandonnée" in item["title"])
    assert "Ancienne version" in check["detail"] and "1 contour exclu" in check["detail"]


# 17-19. Dossier creation reuses the snapshot with an editable reference.


def test_reference_defaults_to_file_name_and_is_prefilled(client_and_repos) -> None:
    client, _, analyses = client_and_repos
    control = synthetic_dossier().controle_technique
    dossier = prepare_quick_analysis_dossier("Résidence Les Tilleuls.dwg", "dwg", control, [])
    assert dossier.reference == "Résidence Les Tilleuls"
    dossier.reconciliation = None
    page = page_for(client, analyses, dossier)
    assert 'name="reference"' in page and 'value="Résidence Les Tilleuls"' in page


def test_creation_uses_typed_reference_without_second_parsing(
    client_and_repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, dossiers, analyses = client_and_repos
    source = synthetic_dossier()
    calls = 0

    def fake_inspection(path: Path, filename: str):
        nonlocal calls
        calls += 1
        return source.controle_technique.model_copy(deep=True), [
            item.model_copy(deep=True) for item in source.planches
        ]

    monkeypatch.setattr(main, "inspect_dxf", fake_inspection)
    monkeypatch.setattr(main, "reconcile_dossier", lambda *_: pytest.fail("no second reconciliation"))
    analyzed = client.post("/analyze", files={"file": ("copro.dxf", b"synthetic")}, follow_redirects=False)
    analysis_id = analyzed.headers["location"].rsplit("/", 1)[-1]
    snapshot = analyses.get(analysis_id)

    created = client.post(
        f"/analyses/{analysis_id}/dossier", data={"reference": "  DOS 2026\t014 "}, follow_redirects=False
    )

    assert created.status_code == 303 and calls == 1
    [dossier] = dossiers.list()
    assert dossier.reference == "DOS 2026 014"
    assert dossier.id != snapshot.dossier.id
    assert dossier.reconciliation == snapshot.dossier.reconciliation
    assert dossier.controle_technique == snapshot.dossier.controle_technique
    assert created.headers["location"] == f"/dossiers/{dossier.id}/dossier"


def test_reference_cleaning_keeps_business_characters() -> None:
    assert clean_reference(" Lot\x00 n°12/B  — Été ") == "Lot n°12/B — Été"
    with pytest.raises(ValueError):
        clean_reference("\x01 \t ")


# 20-23. Renaming keeps the technical identity; references are unique.


def test_rename_keeps_id_documents_and_invalidates_validation(client_and_repos) -> None:
    client, dossiers, _ = client_and_repos
    dossier = synthetic_dossier()
    dossier.id = "a1b2c3"
    reconcile_dossier(dossier)
    record_data_validation(dossier)
    dossier.generations.append(
        Generation(
            id="gen1",
            type_document="etat_descriptif_copropriete",
            template_id="t",
            template_version="1",
            sha256_snapshot="0" * 64,
            statut="brouillon",
            nom_fichier="geoflow_synthetic_copropriete_brouillon.docx",
        )
    )
    dossiers.save(dossier)

    response = client.post(
        f"/dossiers/{dossier.id}/reference",
        data={"reference": "DOS-2026-014"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["reference"] == "DOS-2026-014"
    renamed = dossiers.get("a1b2c3")
    assert renamed.reference == "DOS-2026-014"
    assert renamed.statut_validation_donnees == StatutValidationDonnees.A_VALIDER
    assert renamed.generations[0].nom_fichier == "geoflow_synthetic_copropriete_brouillon.docx"
    assert client.get("/dossiers/a1b2c3/plan").status_code == 200


def test_home_lists_the_new_reference_with_the_plan_name(client_and_repos) -> None:
    client, dossiers, _ = client_and_repos
    dossier = prepare_quick_analysis_dossier("copro.dwg", "dwg", synthetic_dossier().controle_technique, [])
    dossiers.save(dossier)
    client.post(f"/dossiers/{dossier.id}/reference", data={"reference": "DOS-2026-014"})
    home = client.get("/").text
    assert "<strong>DOS-2026-014</strong>" in home
    assert "copro.dwg" in home


def test_reference_collision_is_refused_explicitly(client_and_repos) -> None:
    client, dossiers, analyses = client_and_repos
    first, second = create_dossier("DOS-1", "copropriete"), create_dossier("DOS-2", "copropriete")
    dossiers.save(first)
    dossiers.save(second)

    renamed = client.post(
        f"/dossiers/{second.id}/reference",
        data={"reference": " dos-1 "},
        headers={"Accept": "application/json"},
    )
    assert renamed.status_code == 400
    assert renamed.json()["message"] == "La référence « DOS-1 » est déjà utilisée par un autre dossier."
    assert dossiers.get(second.id).reference == "DOS-2"

    blank = client.post("/dossiers", data={"reference": "DOS-1"})
    assert blank.status_code == 400 and "déjà utilisée" in blank.text

    stored = analyses.create(snapshot_for(synthetic_dossier()).dossier, 0.2)
    conflict = client.post(f"/analyses/{stored.id}/dossier", data={"reference": "DOS-1"})
    assert conflict.status_code == 409
    assert 'aria-invalid="true"' in conflict.text and 'role="alert"' in conflict.text
    assert analyses.get(stored.id)
    assert len(dossiers.list()) == 2


def test_keeping_the_same_reference_is_not_a_collision(client_and_repos) -> None:
    client, dossiers, _ = client_and_repos
    dossier = create_dossier("DOS-1", "copropriete")
    dossiers.save(dossier)
    response = client.post(
        f"/dossiers/{dossier.id}/reference", data={"reference": "DOS-1"}, headers={"Accept": "application/json"}
    )
    assert response.status_code == 200


# 24-25. Themes and responsive layout are structural, not page specific.


def test_diagnostic_styles_rely_on_theme_tokens() -> None:
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    section = css[css.index("/* 8g. Diagnostic") : css.index("/* 8h. Accueil")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", section)
    assert ":root[data-theme=\"dark\"]" in css
    for breakpoint in ("max-width: 900px", "max-width: 700px", "max-width: 480px"):
        assert breakpoint in section


def test_diagnostic_template_is_responsive_and_accessible(client_and_repos) -> None:
    client, _, analyses = client_and_repos
    page = page_for(client, analyses, contradictory_dossier())
    assert '<meta name="viewport"' in page
    assert "data-theme-toggle" in page
    assert '<label for="analysis-reference">' in page
    assert "Point faible :" in page
    assert "Indice favorable :" in page
