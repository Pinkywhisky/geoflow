"""Drawing profiles: inference, live check, persistence, security, legacy."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import BBox, CandidateZone, ControleTechnique, Sommet, TexteDxf
from app.profiles.evaluation import evaluate_profile
from app.profiles.forms import ProfileFormError, profile_from_form
from app.profiles.inference import infer_profile, unmapped_layer_suggestions
from app.profiles.models import DrawingProfile
from app.profiles.patterns import layer_matches, surface_value, template_value
from app.profiles.storage import ProfileNotFoundError, ProfileRepository
from app.quick_analysis import QuickAnalysisRepository
from app.storage import JsonDossierRepository

from .profile_fixtures import ARCHIVE, BALCONY, CAVE, COMMON, PRINCIPAL, TERRACE, build_charter_dxf
from .test_quick_analysis import snapshot_for
from .test_v042_reconciliation import synthetic_dossier
from .test_v042_reconciliation import EXCLUDED as LEGACY_EXCLUDED
from .test_v042_reconciliation import PRINCIPAL as LEGACY_PRINCIPAL
from .test_v042_reconciliation import candidate

ROOT = Path(__file__).parents[1]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    profiles = ProfileRepository(tmp_path / "profiles")
    analyses = QuickAnalysisRepository(tmp_path / "analyses")
    monkeypatch.setattr(main, "repository", JsonDossierRepository(tmp_path / "dossiers"))
    monkeypatch.setattr(main, "analysis_repository", analyses)
    monkeypatch.setattr(main, "profile_repository", profiles)
    return TestClient(main.app), profiles, analyses, tmp_path


def upload(client, path: Path, url: str = "/profils/analyse"):
    return client.post(url, files={"file": (path.name, path.read_bytes())}, follow_redirects=False)


def analysis_id(response) -> str:
    return re.search(r"analyse=([a-f0-9]{32})", response.headers["location"]).group(1)


def form_values(html: str) -> dict[str, list[str]]:
    """What a browser would submit for the profile form, in document order."""

    html = re.sub(r"<template.*?</template>", "", html, flags=re.S)
    form = re.search(r'<form\s+id="profile-form".*?</form>', html, re.S).group(0)
    values: dict[str, list[str]] = {}
    for tag in re.finditer(r"<input\b([^>]*)>|<select\b([^>]*)>(.*?)</select>", form, re.S):
        attributes_text = tag.group(1) if tag.group(1) is not None else tag.group(2)
        attributes = dict(re.findall(r'([a-z-]+)="([^"]*)"', attributes_text))
        name = attributes.get("name")
        if not name:
            continue
        if tag.group(1) is None:
            selected = re.search(r'<option value="([^"]*)" selected', tag.group(3))
            values.setdefault(name, []).append(selected.group(1) if selected else "")
            continue
        kind = attributes.get("type", "text")
        if kind in {"checkbox", "radio"} and " checked" not in attributes_text:
            continue
        values.setdefault(name, []).append(attributes.get("value", ""))
    return values


def as_form(values: dict[str, list[str]]) -> dict[str, list[str]]:
    # httpx only encodes repeated fields from a mapping of lists.
    return {name: list(items) for name, items in values.items() if items}


def draft(tmp_path: Path, **options):
    """Profile inferred from the charter fixture, through the real pipeline."""
    path = build_charter_dxf(tmp_path / "plan.dxf", **options)
    from app.dxf import inspect_dxf
    from app.quick_analysis import prepare_quick_analysis_dossier

    control, planches = inspect_dxf(path, path.name)
    dossier = prepare_quick_analysis_dossier(path.name, "dxf", control, planches)
    profile, explanations = infer_profile(dossier)
    return profile, explanations, dossier


def check(profile, dossier):
    return evaluate_profile(profile, dossier.controle_technique, dossier.planches, unit_declared=dossier.plan_importe.unite_confirmee)


def lines(report, key):
    return [line["text"] for line in report[key]]


# 1-8. Creation from a plan and explainable pre-filling -------------------


def test_profile_wizard_is_created_from_a_plan(env) -> None:
    client, profiles, _, tmp_path = env
    response = upload(client, build_charter_dxf(tmp_path / "plan.dxf"))
    assert response.status_code == 303 and response.headers["location"].startswith("/profils/nouveau?analyse=")
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "Modifier" not in page.text
    assert page.text.count('data-step="') == 5
    assert profiles.list() == ([], 0)


def test_wizard_fields_are_prefilled_and_editable(env) -> None:
    client, _, _, tmp_path = env
    page = client.get(upload(client, build_charter_dxf(tmp_path / "plan.dxf")).headers["location"]).text
    values = form_values(page)
    assert values["role_surface_principale"] == [PRINCIPAL]
    assert values["role_annexe"] == [BALCONY, CAVE]
    assert values["role_partie_commune"] == [COMMON]
    assert values["role_archive"] == [ARCHIVE]
    assert values["lot_pattern"] == ["LOT {numero}"] and values["surface_pattern"] == ["{surface} m²"]
    assert values["level_observed"] == ["RDC", "R+1", "SOUS SOL"]
    assert values["level_value"] == ["RDC", "R+1", "Sous-sol"]
    assert values["unit"] == ["metre"] and values["geometry_source"] == ["model"]
    assert 'disabled' not in re.search(r'<input name="lot_pattern"[^>]*>', page).group(0)


def test_declared_and_missing_units(tmp_path: Path) -> None:
    profile, explanations, _ = draft(tmp_path)
    assert (profile.units.unit, explanations["unit"]["guessed"]) == ("metre", False)
    profile, explanations, _ = draft(tmp_path / "nounit", units=0)
    assert explanations["unit"]["guessed"] is True
    assert profile.units.unit == "metre"
    assert "compatibles avec des mètres carrés" in explanations["unit"]["reason"]


def test_unknown_unit_is_shown_as_a_suggestion(env) -> None:
    client, _, _, tmp_path = env
    page = client.get(upload(client, build_charter_dxf(tmp_path / "plan.dxf", units=0)).headers["location"]).text
    assert "Le plan ne déclare pas explicitement son unité" in page


def test_legacy_classification_and_exclusions_are_reused() -> None:
    dossier = synthetic_dossier(candidates=[candidate("zone-a", -1, 4), candidate("zone-b", 1, 6), candidate("ex", 9, 2, LEGACY_EXCLUDED)])
    snapshot_for(dossier)
    profile, explanations = infer_profile(dossier)
    assert profile.layer_roles.surface_principale == [LEGACY_PRINCIPAL]
    assert "déjà classés" in explanations["roles"]["surface_principale"][LEGACY_PRINCIPAL]
    assert profile.layer_roles.exclusion == [LEGACY_EXCLUDED]


def test_charter_roles_are_explained(tmp_path: Path) -> None:
    profile, explanations, _ = draft(tmp_path)
    assert "contiennent un numéro" in explanations["roles"]["surface_principale"][PRINCIPAL]
    assert "évoque ce rôle" in explanations["roles"]["annexe"][CAVE]
    assert explanations["roles"]["partie_commune"][COMMON]
    assert profile.name == "Nouveau profil de dessin"


# 9-11, 20. Live check reacts to edits and never saves ---------------------


def preview(client, aid, values):
    return client.post(f"/profils/apercu?analyse={aid}", data=as_form(values), headers={"Accept": "application/json"})


def test_live_check_follows_role_edits_without_saving(env) -> None:
    client, profiles, _, tmp_path = env
    created = upload(client, build_charter_dxf(tmp_path / "plan.dxf"))
    aid = analysis_id(created)
    values = form_values(client.get(created.headers["location"]).text)

    base = preview(client, aid, values).json()
    assert base["ok"] and "8 contours fermés reconnus" in base["fragments"]["role_annexe"]

    values["role_partie_commune"] = []
    removed = preview(client, aid, values).json()
    assert "Aucun calque associé." in removed["fragments"]["role_partie_commune"]

    values["role_annexe"] = [CAVE]
    narrowed = preview(client, aid, values).json()
    assert "4 contours fermés reconnus" in narrowed["fragments"]["role_annexe"]

    values["role_hors_copropriete"] = ["A-SURF-*"]
    widened = preview(client, aid, values).json()
    assert "correspond à plusieurs rôles" in widened["fragments"]["report"]
    assert profiles.list() == ([], 0)


def test_live_check_reports_invalid_input(env) -> None:
    client, _, _, tmp_path = env
    created = upload(client, build_charter_dxf(tmp_path / "plan.dxf"))
    values = form_values(client.get(created.headers["location"]).text)
    values["lot_pattern"] = ["LOT"]
    values["lot_radius"] = ["abc"]
    response = preview(client, analysis_id(created), values)
    assert response.status_code == 422
    assert any("{numero}" in message for message in response.json()["messages"])
    assert any("Numéro de lot" in message for message in response.json()["messages"])


# 12-19. Identification rules ------------------------------------------------


def test_lot_numbers_and_patterns(tmp_path: Path) -> None:
    profile, _, dossier = draft(tmp_path)
    report = check(profile, dossier)
    assert "8 / 8 avec un numéro de lot · 4 lots" in lines(report, "lots")
    assert ("LOT 1", "1") in report["lot_examples"]
    bare = profile.model_copy(update={"lot_identification": profile.lot_identification.model_copy(update={"pattern": "{numero}"})})
    assert check(bare, dossier)["status"] == "incomplet"


def test_annotated_surfaces_and_tolerance(tmp_path: Path) -> None:
    profile, _, dossier = draft(tmp_path)
    report = check(profile, dossier)
    assert "4 / 4 lots avec une surface annotée" in lines(report, "surfaces")
    text = next(item for item in dossier.controle_technique.textes if item.contenu == "54,00 m²")
    text.contenu = "54,03 m²"
    assert "4 cohérente(s) avec la géométrie dans la tolérance." in lines(check(profile, dossier), "surfaces")
    strict = profile.model_copy(
        update={
            "surface_identification": profile.surface_identification.model_copy(
                update={"tolerance_abs_m2": 0.01, "tolerance_rel_pct": 0}
            )
        }
    )
    assert any("1 écart(s) hors tolérance" in item for item in lines(check(strict, dossier), "surfaces"))


def test_level_aliases_map_observed_values(tmp_path: Path) -> None:
    profile, _, dossier = draft(tmp_path)
    assert "3 niveaux reconnus : R+1, RDC, Sous-sol" in lines(check(profile, dossier), "levels")
    renamed = [alias.model_copy(update={"value": "Rez-de-chaussée"}) if alias.observed == "RDC" else alias for alias in profile.levels.aliases]
    report = check(profile.model_copy(update={"levels": profile.levels.model_copy(update={"aliases": renamed})}), dossier)
    assert any("Rez-de-chaussée" in item for item in lines(report, "levels"))
    nothing = profile.model_copy(update={"levels": profile.levels.model_copy(update={"aliases": []})})
    assert check(nothing, dossier)["status"] == "avertissements"


def test_source_priority_decides_conflicts() -> None:
    zone = CandidateZone(
        id="z", type_entite="LWPOLYLINE", calque="SURF", handle_dxf="z", planche_region="model",
        surface_geometrique_unites=100, surface_geometrique_m2=100,
        bbox=BBox(min_x=0, min_y=0, max_x=10, max_y=10),
        sommets=[Sommet(x=0, y=0), Sommet(x=10, y=0), Sommet(x=10, y=10), Sommet(x=0, y=10)],
        provenance={"fichier_source": "f", "methode_detection": "test"},
    )
    texts = [
        TexteDxf(contenu="LOT 1", x=1, y=1, calque="LOTS", handle_dxf="t1", planche_region="model"),
        TexteDxf(contenu="LOT 2", x=5, y=-0.4, calque="LOTS", handle_dxf="t2", planche_region="model"),
    ]
    control = ControleTechnique(version_dxf="AC1024", unite_detectee="metre", zones_candidates=[zone], textes=texts)
    profile = DrawingProfile(name="P", layer_roles={"surface_principale": ["SURF"]}, lot_identification={"pattern": "LOT {numero}", "sources": ["texte_dans_zone", "texte_proche"]})
    inside_first = evaluate_profile(profile, control, [], unit_declared=True)
    assert "1 / 1 avec un numéro de lot · 1 lot" in lines(inside_first, "lots")
    assert any("sources se contredisent" in item for item in lines(inside_first, "lots"))
    assert inside_first["lot_values"] == ["1"]
    near_first = profile.model_copy(
        update={"lot_identification": profile.lot_identification.model_copy(update={"sources": ["texte_proche", "texte_dans_zone"]})}
    )
    assert evaluate_profile(near_first, control, [], unit_declared=True)["lot_values"] == ["2"]


def test_version_markers_are_never_deterministic(tmp_path: Path) -> None:
    profile, _, dossier = draft(tmp_path, marker=True)
    assert profile.versions.markers == ["VERSION * ABANDONNEE"]
    report = check(profile, dossier)
    assert "Calques d’archive : 1 contour écarté." in lines(report, "versions")
    assert any("pas de déterminer de manière certaine" in item for item in lines(report, "versions"))
    assert report["status"] == "avertissements"


def test_annexes_without_explicit_lot(tmp_path: Path) -> None:
    profile, _, dossier = draft(tmp_path)
    assert profile.annexes.lot_source == "numero_dans_annexe"
    report = check(profile, dossier)
    assert "4 avec un numéro de lot inscrit" in lines(report, "annexes")
    nothing = profile.model_copy(update={"annexes": profile.annexes.model_copy(update={"lot_source": "aucune", "fallback": "rien"})})
    assert "8 sans proposition de lot" in lines(check(nothing, dossier), "annexes")


# 21-23. Classification ------------------------------------------------------


def test_profile_classification(tmp_path: Path) -> None:
    complete, _, dossier = draft(tmp_path / "complete", with_balconies=False)
    report = check(complete, dossier)
    assert report["status"] == "complet", report["conclusion"]
    assert report["conclusion"] == "Ce profil permet une lecture déterministe de ce plan."
    warned, _, dossier = draft(tmp_path / "warned")
    assert check(warned, dossier)["status"] == "avertissements"
    empty = warned.model_copy(update={"layer_roles": warned.layer_roles.model_copy(update={"surface_principale": []})})
    report = check(empty, dossier)
    assert report["status"] == "incomplet" and "surface principale" in report["conclusion"]


# 24-29. Persistence --------------------------------------------------------


def save_new(client, tmp_path, **options):
    created = upload(client, build_charter_dxf(tmp_path / "plan.dxf", **options))
    aid = analysis_id(created)
    values = form_values(client.get(created.headers["location"]).text)
    values["name"] = ["Cabinet Dupont - Copropriété"]
    saved = client.post(f"/profils/enregistrer?analyse={aid}", data=as_form(values), follow_redirects=False)
    assert saved.status_code == 303
    return re.search(r"/profils/([a-f0-9]{32})", saved.headers["location"]).group(1), aid, values


def test_saved_profile_holds_rules_only(env) -> None:
    client, profiles, _, tmp_path = env
    profile_id, _, _ = save_new(client, tmp_path)
    stored = (tmp_path / "profiles" / f"{profile_id}.json").read_text(encoding="utf-8")
    profile = DrawingProfile.model_validate_json(stored)
    assert profile.schema_version == 2 and profile.name == "Cabinet Dupont - Copropriété"
    assert profile.last_check.status == "avertissements"
    for leak in ("plan.dxf", "54,00", "LOT 1", "PLAN DE COPROPRIETE", "x\":", "sommets"):
        assert leak not in stored
    assert ProfileRepository(tmp_path / "profiles").get(profile_id) == profile


def test_rename_keeps_id_and_reopen_uses_same_wizard(env) -> None:
    client, profiles, _, tmp_path = env
    profile_id, aid, values = save_new(client, tmp_path)
    values["name"] = ["Cabinet Dupont v2"]
    response = client.post(f"/profils/{profile_id}", data=as_form(values), follow_redirects=False)
    assert response.status_code == 303
    [profile], _ = profiles.list()
    assert (profile.id, profile.name) == (profile_id, "Cabinet Dupont v2")
    page = client.get(f"/profils/{profile_id}").text
    assert 'value="Cabinet Dupont v2"' in page and page.count('data-step="') == 5
    assert form_values(page)["role_surface_principale"] == [PRINCIPAL]


def test_duplicate_and_delete(env) -> None:
    client, profiles, _, tmp_path = env
    profile_id, _, _ = save_new(client, tmp_path)
    copy = client.post(f"/profils/{profile_id}/dupliquer", follow_redirects=False)
    copy_id = copy.headers["location"].rsplit("/", 1)[-1]
    assert copy_id != profile_id and profiles.get(copy_id).name.endswith("(copie)")
    assert client.post(f"/profils/{profile_id}/supprimer", follow_redirects=False).status_code == 303
    with pytest.raises(ProfileNotFoundError):
        profiles.get(profile_id)
    assert [item.id for item in profiles.list()[0]] == [copy_id]
    assert client.get(f"/profils/{profile_id}").status_code == 404


def test_listing_shows_status_and_skips_corrupted_files(env) -> None:
    client, profiles, _, tmp_path = env
    save_new(client, tmp_path)
    (tmp_path / "profiles" / ("0" * 32 + ".json")).write_text("{not json", encoding="utf-8")
    page = client.get("/profils").text
    assert "Cabinet Dupont - Copropriété" in page and "Utilisable avec avertissements" in page
    assert "1 fichier(s) de profil illisible(s)" in page
    assert client.get("/profils/" + "0" * 32).status_code == 404


# 30-32. Another plan: suggestions, never silent changes ------------------


def test_testing_on_another_plan_suggests_without_changing(env) -> None:
    client, profiles, _, tmp_path = env
    profile_id, _, _ = save_new(client, tmp_path)
    before = profiles.get(profile_id)
    other = build_charter_dxf(tmp_path / "other" / "autre.dxf", with_terrace=True)
    response = upload(client, other, f"/profils/{profile_id}/tester")
    assert response.status_code == 303 and "#etape-5" in response.headers["location"]
    page = client.get(response.headers["location"]).text
    assert f"« {TERRACE} »" in page and "Ajouter au profil" in page
    after = profiles.get(profile_id)
    assert after == before


def test_unmapped_layer_suggestion_is_explained(tmp_path: Path) -> None:
    profile, _, _ = draft(tmp_path)
    _, _, other = draft(tmp_path / "other", with_terrace=True)
    [suggestion] = unmapped_layer_suggestions(profile, other)
    assert suggestion == {"layer": TERRACE, "role": "annexe", "count": 1, "reason": "son nom évoque ce rôle"}


# 33-36. Invalid input and security -----------------------------------------


def test_invalid_upload_is_reported(env) -> None:
    client, _, _, _ = env
    response = client.post("/profils/analyse", files={"file": ("notes.txt", b"hello")})
    assert response.status_code == 400 and "Seuls les fichiers" in response.text
    assert client.get("/profils/nouveau?analyse=" + "f" * 32).status_code == 404


def test_malformed_forms_are_refused() -> None:
    with pytest.raises(ProfileFormError) as error:
        profile_from_form({"name": "x", "geometry_source": "partout", "unit": "parsec"})
    assert any("Source géométrique" in item for item in error.value.messages)
    assert any("Unité" in item for item in error.value.messages)
    with pytest.raises(ValueError):
        DrawingProfile.model_validate({"name": "x", "schema_version": 3})
    with pytest.raises(ValueError):
        DrawingProfile.model_validate({"name": "x", "id": "../../etc/passwd"})


def test_patterns_are_a_controlled_mini_format() -> None:
    assert not layer_matches("80-(.*)+", "80-SUPERFICIE")
    assert layer_matches("80-(.*)+", "80-(.*)+")
    assert layer_matches("archive_*", "ARCHIVE_2023") and not layer_matches("archive_?", "ARCHIVE_2023")
    for template in ("{numero}{numero}", "{__class__}", "{numero", "LOT {surface}", "x" * 61 + "{numero}"):
        with pytest.raises(ValueError):
            DrawingProfile(name="x", lot_identification={"pattern": template})
    with pytest.raises(ValueError):
        DrawingProfile(name="x", layer_roles={"annexe": ["*" * 3]})
    with pytest.raises(ValueError):
        DrawingProfile(name="x", layer_roles={"annexe": [f"L{index}" for index in range(51)]})
    started = time.perf_counter()
    assert template_value("{niveau} PLAN", "A" * 5000) is None
    assert surface_value("S={surface} m²", "S=" + "9" * 5000 + "m²") is None
    assert time.perf_counter() - started < 0.1
    assert DrawingProfile(name="a\x00b\x1f c").name == "a b c"


# 37-40. No second parsing, legacy unchanged, layout ------------------------


def test_preview_and_save_never_read_the_plan_again(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _, tmp_path = env
    calls = 0
    original = main.inspect_dxf

    def counting(path, filename):
        nonlocal calls
        calls += 1
        return original(path, filename)

    monkeypatch.setattr(main, "inspect_dxf", counting)
    profile_id, aid, values = save_new(client, tmp_path)
    preview(client, aid, values)
    client.post(f"/profils/{profile_id}?analyse={aid}", data=as_form(values))
    client.get(f"/profils/{profile_id}?analyse={aid}")
    assert calls == 1


def test_profiles_do_not_change_the_legacy_analysis(env) -> None:
    client, _, analyses, tmp_path = env
    plan = build_charter_dxf(tmp_path / "legacy.dxf")
    before = client.post("/analyze", files={"file": ("legacy.dxf", plan.read_bytes())}, follow_redirects=False)
    first = analyses.get(before.headers["location"].rsplit("/", 1)[-1]).dossier.reconciliation
    save_new(client, tmp_path / "with-profile")
    after = client.post("/analyze", files={"file": ("legacy.dxf", plan.read_bytes())}, follow_redirects=False)
    assert after.headers["location"].endswith("/profil")
    aid = after.headers["location"].split("/")[2]
    client.post(f"/analyses/{aid}/profil", data={"profile": "legacy"})
    second = analyses.get(aid).dossier.reconciliation
    assert first == second


def test_profile_styles_are_responsive_and_themed() -> None:
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    section = css[css.index("/* 8i. Profils de dessin") : css.index("/* 9. Plan preview")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", section)
    assert "@media (max-width: 700px)" in section
    assert ".js .profile-step:not(.is-active)" in section


def test_home_links_to_profiles_as_a_secondary_entry(env) -> None:
    client, _, _, _ = env
    home = client.get("/").text
    assert 'href="/profils"' in home and home.index('href="/profils"') < home.index("data-drop-form")
    assert client.get("/profils").status_code == 200
