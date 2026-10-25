"""Daily use of drawing profiles: choice after upload, real application,
hybrid fallback, provenance, revisions and frozen snapshots in dossiers."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import ezdxf
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.domain import Dossier, ProfileUsage, VerificationStatus
from app.dxf import inspect_dxf
from app.profiles.compatibility import Compatibility, recommend
from app.profiles.inference import infer_profile
from app.profiles.models import RULE_FIELDS, DrawingProfile
from app.profiles.storage import ProfileNotFoundError, ProfileRepository
from app.profiles.usage import usage_from_profile
from app.quick_analysis import QuickAnalysisRepository, prepare_quick_analysis_dossier
from app.reconciliation import reconcile_dossier
from app.storage import JsonDossierRepository
from app.workflow import (
    apply_profile_choice,
    has_current_data_validation,
    record_data_validation,
    validation_snapshot_sha256,
)

from .profile_fixtures import COMMON, LOT_TEXT, PRINCIPAL, build_charter_dxf

ROOT = Path(__file__).parents[1]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    profiles = ProfileRepository(tmp_path / "profiles")
    analyses = QuickAnalysisRepository(tmp_path / "analyses")
    dossiers = JsonDossierRepository(tmp_path / "dossiers")
    monkeypatch.setattr(main, "repository", dossiers)
    monkeypatch.setattr(main, "analysis_repository", analyses)
    monkeypatch.setattr(main, "profile_repository", profiles)
    monkeypatch.setattr(main, "compatibility_cache", main.CompatibilityCache())
    inspections: list[str] = []
    real_inspect = main.inspect_dxf

    def counting_inspect(path, filename):
        inspections.append(filename)
        return real_inspect(path, filename)

    monkeypatch.setattr(main, "inspect_dxf", counting_inspect)
    return {
        "client": TestClient(main.app),
        "profiles": profiles,
        "analyses": analyses,
        "dossiers": dossiers,
        "tmp": tmp_path,
        "inspections": inspections,
    }


def inferred(tmp_path: Path, name: str = "Cabinet Dupont", **options) -> DrawingProfile:
    path = build_charter_dxf(tmp_path / f"source-{name.replace(' ', '_')}.dxf", **options)
    control, planches = inspect_dxf(path, path.name)
    profile, _ = infer_profile(prepare_quick_analysis_dossier(path.name, "dxf", control, planches))
    return profile.model_copy(update={"name": name})


def save(env, profile: DrawingProfile, **updates) -> DrawingProfile:
    return env["profiles"].save(profile.model_copy(update=updates) if updates else profile)


def deposit(env, path: Path):
    response = env["client"].post("/analyze", files={"file": (path.name, path.read_bytes())}, follow_redirects=False)
    assert response.status_code == 303
    return response, response.headers["location"].split("/")[2]


def choose(env, aid: str, profile: str, **extra):
    return env["client"].post(f"/analyses/{aid}/profil", data={"profile": profile, **extra}, follow_redirects=False)


def checked_value(html: str) -> str | None:
    match = re.search(r'name="profile" value="([^"]+)" required checked', html)
    return match.group(1) if match else None


def plan_text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def without_role(profile: DrawingProfile, role: str) -> DrawingProfile:
    return profile.model_copy(update={"layer_roles": profile.layer_roles.model_copy(update={role: []})})


def charter_with_legacy_lot(path: Path) -> Path:
    """Charter plan plus one lot drawn with the automatic engine's own
    conventions, which the profile does not describe."""

    build_charter_dxf(path)
    document = ezdxf.readfile(path)
    msp = document.modelspace()
    for name in ("80-LOGEMENT", "05-LOTS", "83-SURFACES"):
        document.layers.add(name)
    for x in (100, 106):
        msp.add_lwpolyline([(x, 0), (x + 6, 0), (x + 6, 6), (x, 6)], close=True, dxfattribs={"layer": "80-LOGEMENT"})
    msp.add_text("7", dxfattribs={"layer": "05-LOTS", "height": 0.3}).set_placement((106, 3))
    msp.add_text("LOT 7", dxfattribs={"layer": "0", "height": 0.3}).set_placement((106, 2))
    msp.add_text("S = 72,00 m2", dxfattribs={"layer": "83-SURFACES", "height": 0.3}).set_placement((106, 4))
    msp.add_text("IDENTIFICATION DES LOTS", dxfattribs={"layer": "0", "height": 0.5}).set_placement((100, 10))
    document.saveas(path)
    return path


def priority_plan(path: Path) -> Path:
    """One principal zone holding « LOT 12 » near a corner, while « LOT 13 »
    lies outside, closer to the zone's centre."""

    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = 6
    msp = document.modelspace()
    for name in (PRINCIPAL, LOT_TEXT, "T-SURFACE"):
        document.layers.add(name)
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True, dxfattribs={"layer": PRINCIPAL})
    msp.add_text("LOT 12", dxfattribs={"layer": LOT_TEXT, "height": 0.3}).set_placement((0.5, 0.5))
    msp.add_text("LOT 13", dxfattribs={"layer": LOT_TEXT, "height": 0.3}).set_placement((5, 10.5))
    msp.add_text("100,00 m²", dxfattribs={"layer": "T-SURFACE", "height": 0.3}).set_placement((5, 5))
    document.saveas(path)
    return path


def priority_profile(sources: list[str]) -> DrawingProfile:
    return DrawingProfile.model_validate(
        {
            "name": "Priorités",
            "layer_roles": {"surface_principale": [PRINCIPAL]},
            "lot_identification": {"layers": [LOT_TEXT], "pattern": "LOT {numero}", "sources": sources, "radius": 12},
            "surface_identification": {"layers": ["T-SURFACE"], "pattern": "{surface} m²", "sources": ["texte_dans_zone"]},
        }
    )


def analysed(path: Path, profile: DrawingProfile | None) -> Dossier:
    control, planches = inspect_dxf(path, path.name)
    dossier = prepare_quick_analysis_dossier(path.name, "dxf", control, planches)
    apply_profile_choice(dossier, usage_from_profile(profile) if profile else None)
    return dossier


# 1, 8, 34. Without any profile the plan goes straight to the automatic analysis.


def test_upload_without_profile_is_analysed_automatically(env) -> None:
    response, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    assert response.headers["location"] == f"/analyses/{aid}"
    snapshot = env["analyses"].get(aid)
    assert snapshot.dossier.profile_usage == ProfileUsage(
        mode="legacy", analysed_at=snapshot.dossier.profile_usage.analysed_at
    )
    page = env["client"].get(f"/analyses/{aid}").text
    assert "Analyse automatique / Legacy" in page and "Créer un profil depuis ce plan" in page
    assert len(env["inspections"]) == 1


# 2, 9, 25-26. A single compatible profile is preselected on a light screen.


def test_single_profile_is_preselected_with_its_reasons(env) -> None:
    profile = save(env, inferred(env["tmp"]))
    response, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    assert response.headers["location"] == f"/analyses/{aid}/profil"
    assert env["analyses"].get(aid).dossier.reconciliation is None  # business analysis not run yet
    page = env["client"].get(response.headers["location"]).text
    assert checked_value(page) == profile.id
    text = plan_text(page)
    assert "Quel profil utiliser ?" in text and "Compatible" in text and "Recommandé" in text
    assert "Unité conforme" in text and "Numéro de lot reconnu sur 8 zones (4 lots)" in text
    assert "<summary>Changer de profil</summary>" in page and 'value="legacy"' in page
    assert "Créer un nouveau profil à partir de ce plan" in text


# 3-4. Several profiles: the one clearly better on facts is recommended.


def test_clearly_better_profile_is_recommended(env) -> None:
    good = save(env, inferred(env["tmp"], "Charte actuelle"))
    partial = save(env, without_role(inferred(env["tmp"], "Ancienne charte"), "partie_commune"))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    page = env["client"].get(f"/analyses/{aid}/profil").text
    assert checked_value(page) == good.id
    assert page.index(good.name) < page.index(partial.name)
    poorer = page[page.index(f'id="choice-{partial.id}"') :]
    assert "Compatible avec avertissements" in poorer[:2000] and COMMON in poorer[:4000]


# 5, 14. No arbitrary choice when nothing is clearly better.


def test_no_preselection_without_a_clear_winner(env) -> None:
    first = save(env, inferred(env["tmp"], "Charte A"))
    second = save(env, first.model_copy(update={"id": "b" * 32, "name": "Charte B"}))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    page = env["client"].get(f"/analyses/{aid}/profil").text
    assert checked_value(page) is None and "Recommandé" not in page
    assert "<details" not in page  # the full list, not a single proposal
    missing = choose(env, aid, "")
    assert missing.status_code == 422 and env["analyses"].get(aid).dossier.reconciliation is None
    assert second.id in page


# 6. A default profile is preselected, unless it fits the plan poorly.


def test_default_profile_is_preselected_when_usable(env) -> None:
    good = save(env, inferred(env["tmp"], "Charte actuelle"))
    weaker = save(env, without_role(inferred(env["tmp"], "Charte maison"), "partie_commune"))
    assert env["client"].post(f"/profils/{weaker.id}/defaut", follow_redirects=False).status_code == 303
    assert "Par défaut" in env["client"].get("/profils").text
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    page = env["client"].get(f"/analyses/{aid}/profil").text
    assert checked_value(page) == weaker.id and "Par défaut" in page
    poor = save(env, without_role(inferred(env["tmp"], "Charte vide"), "surface_principale"))
    env["profiles"].set_default(poor.id)
    _, other = deposit(env, build_charter_dxf(env["tmp"] / "plan2.dxf"))
    assert checked_value(env["client"].get(f"/analyses/{other}/profil").text) == good.id
    env["client"].post("/profils/defaut/retirer")
    assert env["profiles"].default_id() is None


# 7. A recently used profile comes first, without being imposed.


def test_recent_profile_is_listed_first_but_not_selected(env) -> None:
    first = save(env, inferred(env["tmp"], "Charte A"))
    second = save(env, first.model_copy(update={"id": "c" * 32, "name": "Charte B"}))
    env["profiles"].mark_used(second.id)
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    page = env["client"].get(f"/analyses/{aid}/profil").text
    assert page.index('id="choice-' + second.id) < page.index('id="choice-' + first.id)
    assert "Utilisé récemment" in page and checked_value(page) is None


# 8, 13, 31. Legacy stays available and the choice can be changed.


def test_profile_can_be_changed_and_legacy_restores_the_automatic_result(env) -> None:
    profile = save(env, inferred(env["tmp"]))
    plan = build_charter_dxf(env["tmp"] / "plan.dxf")
    _, aid = deposit(env, plan)
    assert choose(env, aid, profile.id).headers["location"] == f"/analyses/{aid}"
    profiled = env["analyses"].get(aid).dossier
    assert profiled.profile_usage.profile_id == profile.id and len(profiled.reconciliation.lot_proposals) == 4
    page = env["client"].get(f"/analyses/{aid}/profil").text
    assert checked_value(page) == profile.id
    choose(env, aid, "legacy")
    switched = env["analyses"].get(aid).dossier
    assert switched.profile_usage.mode == "legacy"
    assert switched.reconciliation == analysed(plan, None).reconciliation
    assert len(env["inspections"]) == 1


# 10, 11, 12, 30. Warnings, incomplete profiles and poorly fitting profiles.


def test_profile_with_warnings_lists_uncovered_layers(env) -> None:
    profile = save(env, without_role(inferred(env["tmp"]), "partie_commune"))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    text = plan_text(env["client"].get(f"/analyses/{aid}/profil").text)
    assert "Compatible avec avertissements" in text
    assert f"1 calque avec des contours non couverts par le profil : {COMMON}" in text
    assert choose(env, aid, profile.id).status_code == 303  # usable without confirmation


def test_incomplete_profile_is_flagged_and_never_recommended(env) -> None:
    incomplete = save(env, without_role(inferred(env["tmp"], "Cabinet X"), "surface_principale"))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    page = env["client"].get(f"/analyses/{aid}/profil").text
    assert checked_value(page) is None and "Recommandé" not in page
    assert "Peu compatible" in page and "Aucune surface principale reconnue" in page
    assert incomplete.id in page


def test_poorly_fitting_profile_needs_explicit_confirmation(env) -> None:
    poor = save(env, without_role(inferred(env["tmp"]), "surface_principale"))
    stored = (env["profiles"].root / f"{poor.id}.json").read_bytes()
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    warned = choose(env, aid, poor.id)
    assert warned.status_code == 200
    text = plan_text(warned.text)
    assert "Ce dessin ne correspond pas complètement au profil" in text
    assert PRINCIPAL in text and "risquent de nécessiter le mode automatique" in text
    assert "Continuer avec ce profil" in text and "Choisir un autre profil" in text
    assert "Créer un profil depuis ce plan" in text
    assert env["analyses"].get(aid).dossier.reconciliation is None
    confirmed = choose(env, aid, poor.id, confirm="1")
    assert confirmed.headers["location"] == f"/analyses/{aid}"
    assert env["analyses"].get(aid).dossier.profile_usage.profile_id == poor.id
    assert (env["profiles"].root / f"{poor.id}.json").read_bytes() == stored


# 15. Choosing, analysing and creating a dossier never modify the profile.


def test_daily_use_never_modifies_the_profile(env) -> None:
    profile = save(env, inferred(env["tmp"]))
    stored = (env["profiles"].root / f"{profile.id}.json").read_bytes()
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    env["client"].get(f"/analyses/{aid}/profil")
    choose(env, aid, profile.id)
    env["client"].get(f"/analyses/{aid}")
    env["client"].post(f"/analyses/{aid}/dossier", data={"reference": "P9001"})
    assert (env["profiles"].root / f"{profile.id}.json").read_bytes() == stored
    assert profile.id in env["profiles"].last_used()


# 16, 17, 23, 36, 37. Creating a profile from the flow reuses the inspection.


def test_profile_created_from_the_flow_analyses_the_same_plan(env) -> None:
    save(env, without_role(inferred(env["tmp"], "Ancienne"), "surface_principale"))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    client = env["client"]
    choice = client.get(f"/analyses/{aid}/profil").text
    assert f"/profils/nouveau?analyse={aid}&amp;parcours=1" in choice
    wizard = client.get(f"/profils/nouveau?analyse={aid}&parcours=1")
    assert wizard.status_code == 200 and "&amp;parcours=1" in wizard.text
    from .test_profiles import as_form, form_values

    values = form_values(wizard.text)
    values["name"] = ["Charte du plan"]
    saved = client.post(f"/profils/enregistrer?analyse={aid}&parcours=1", data=as_form(values), follow_redirects=False)
    assert "parcours=1" in saved.headers["location"]
    page = client.get(saved.headers["location"]).text
    assert "Utiliser ce profil pour analyser le plan" in page and f'action="/analyses/{aid}/profil"' in page
    new_id = re.search(r"/profils/([a-f0-9]{32})", saved.headers["location"]).group(1)
    assert choose(env, aid, new_id).headers["location"] == f"/analyses/{aid}"
    assert env["analyses"].get(aid).dossier.profile_usage.profile_id == new_id
    assert len(env["inspections"]) == 1


# 18, 22. The profile is really applied, with its provenance.


def test_profile_rules_drive_the_reconciliation(tmp_path: Path) -> None:
    plan = build_charter_dxf(tmp_path / "plan.dxf")
    automatic = analysed(plan, None)
    assert automatic.reconciliation.lot_proposals == []  # the charter means nothing to legacy
    dossier = analysed(plan, inferred(tmp_path))
    proposals = dossier.reconciliation.lot_proposals
    assert [(item.numero_propose, item.statut, item.origine) for item in proposals] == [
        (number, VerificationStatus.AUTO_VERIFIE, "profil") for number in "1234"
    ]
    assert all(item.surface_annotee_m2 == pytest.approx(54.0) for item in proposals)
    evidence = {item.id: item for item in dossier.reconciliation.preuves}
    lot = evidence["preuve-profil-lot-1"]
    assert lot.origine == "profil" and "règle du profil" in lot.description
    roles = [item for item in evidence.values() if item.source == "role_calque_profil"]
    assert roles and all(item.origine == "profil" for item in roles)
    assert evidence["preuve-profil-niveau-1"].valeur == "RDC"
    annexes = [item for item in dossier.reconciliation.business_zone_candidates if item.categorie_proposee.value == "secondaire_annexe"]
    assert all(item.statut == VerificationStatus.A_CONFIRMER for item in annexes)  # proposed, never attached


# 19, 23, 24. Hybrid: what the profile cannot read is completed by legacy.


def test_missing_rules_fall_back_to_the_automatic_engine(env, tmp_path: Path) -> None:
    plan = charter_with_legacy_lot(tmp_path / "hybrid.dxf")
    dossier = analysed(plan, inferred(tmp_path))
    origins = {item.numero_propose: item.origine for item in dossier.reconciliation.lot_proposals}
    assert origins == {"1": "profil", "2": "profil", "3": "profil", "4": "profil", "7": "legacy"}
    evidence = {item.id: item for item in dossier.reconciliation.preuves}
    assert evidence["preuve-legacy-lot-7"].origine == "legacy"
    legacy_lot = next(item for item in dossier.reconciliation.lot_proposals if item.numero_propose == "7")
    assert all(evidence[item].origine == "legacy" for item in legacy_lot.evidence_ids)
    profile = save(env, inferred(tmp_path))
    _, aid = deposit(env, plan)
    choose(env, aid, profile.id)
    text = plan_text(env["client"].get(f"/analyses/{aid}").text)
    assert "4 lot(s) identifié(s) par le profil · 1 complété(s) par l’analyse automatique" in text
    assert "analyse automatique" in text and "profil" in text


def test_profile_rule_wins_over_a_contradicting_heuristic(tmp_path: Path) -> None:
    plan = charter_with_legacy_lot(tmp_path / "hybrid.dxf")
    profile = inferred(tmp_path)
    profile = profile.model_copy(
        update={"layer_roles": profile.layer_roles.model_copy(update={"exclusion": ["80-*"]})}
    )
    dossier = analysed(plan, profile)
    assert "7" not in {item.numero_propose for item in dossier.reconciliation.lot_proposals}
    excluded = [item for item in dossier.reconciliation.preuves if item.source == "calque_exclusion_profil"]
    assert len(excluded) == 2 and all(item.origine == "profil" for item in excluded)


# 20-21. Source priority of the profile, and contradicting sources.


def test_profile_source_priority_is_respected(tmp_path: Path) -> None:
    plan = priority_plan(tmp_path / "priority.dxf")
    inside_first = analysed(plan, priority_profile(["texte_dans_zone", "texte_proche"]))
    proposal = inside_first.reconciliation.lot_proposals[0]
    assert proposal.numero_propose == "12" and proposal.statut == VerificationStatus.A_CONFIRMER
    conflict = next(item for item in inside_first.reconciliation.preuves if item.source == "source_secondaire_contradictoire")
    assert conflict.valeur == "13" and "lot 13" in conflict.description
    near_first = analysed(plan, priority_profile(["texte_proche", "texte_dans_zone"]))
    assert near_first.reconciliation.lot_proposals[0].numero_propose == "13"


# 25-26, 42. Revision and content hash.


def test_revision_follows_rule_changes_only(env) -> None:
    profiles = env["profiles"]
    profile = profiles.save(inferred(env["tmp"]))
    assert profile.revision == 1
    renamed = profiles.save(profile.model_copy(update={"name": "Cabinet Dupont — Copropriété"}))
    assert renamed.revision == 1 and renamed.content_hash == profile.content_hash
    changed = profiles.save(without_role(renamed, "archive"))
    assert changed.revision == 2 and changed.content_hash != profile.content_hash
    again = profiles.save(changed.model_copy(update={"last_check": None}))
    assert again.revision == 2
    copy = profiles.duplicate(profile.id)
    assert copy.revision == 1 and copy.content_hash == changed.content_hash
    assert re.fullmatch(r"[a-f0-9]{64}", profile.content_hash)


# 27. The snapshot is an exact copy of the rules, and only of the rules.


def test_snapshot_is_exact_and_holds_rules_only(env) -> None:
    profile = save(env, inferred(env["tmp"]))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    choose(env, aid, profile.id)
    usage = env["analyses"].get(aid).dossier.profile_usage
    assert usage.rules == profile.rules()
    assert (usage.profile_id, usage.profile_name, usage.revision, usage.content_hash) == (
        profile.id, profile.name, profile.revision, profile.content_hash
    )
    payload = usage.model_dump(mode="json")
    assert set(payload["rules"]) == set(RULE_FIELDS)
    serialized = json.dumps(payload, ensure_ascii=False)
    for private in ("plan.dxf", "54,00", "sommets", "handle", '"x"'):
        assert private not in serialized


# 28-33, 36. Reproducibility: modified, renamed and deleted profiles.


def test_analysed_dossier_keeps_its_snapshot_across_profile_versions(env) -> None:
    client, profiles = env["client"], env["profiles"]
    v1 = save(env, inferred(env["tmp"]))
    _, plan_a = deposit(env, build_charter_dxf(env["tmp"] / "plan-a.dxf"))
    choose(env, plan_a, v1.id)
    created = client.post(f"/analyses/{plan_a}/dossier", data={"reference": "P9001"}, follow_redirects=False)
    dossier_id = created.headers["location"].split("/")[2]
    before = env["dossiers"].get(dossier_id)
    assert before.profile_usage.revision == 1 and len(before.reconciliation.lot_proposals) == 4

    # Wednesday: the charter changes so that it would no longer read any lot.
    v2 = profiles.save(without_role(profiles.get(v1.id), "surface_principale"))
    assert v2.revision == 2
    page = plan_text(client.get(f"/dossiers/{dossier_id}/dossier").text)
    assert "Cabinet Dupont · v1" in page and "Une version plus récente du profil existe" in page
    assert "Dossier : v1 · disponible : v2" in page

    # Friday: any action re-reconciles the dossier, still with its own v1 rules.
    client.post(f"/dossiers/{dossier_id}/calques", data={"layer_name": COMMON, "status": "retenue"})
    after = env["dossiers"].get(dossier_id)
    assert after.profile_usage == before.profile_usage
    assert [item.numero_propose for item in after.reconciliation.lot_proposals] == ["1", "2", "3", "4"]
    assert after.reconciliation.profil_regles == "Cabinet Dupont · v1"

    # Plan B is analysed with the current revision.
    _, plan_b = deposit(env, build_charter_dxf(env["tmp"] / "plan-b.dxf"))
    choose(env, plan_b, v1.id, confirm="1")
    assert env["analyses"].get(plan_b).dossier.profile_usage.revision == 2
    created_b = client.post(f"/analyses/{plan_b}/dossier", data={"reference": "P9002"}, follow_redirects=False)
    dossier_b = created_b.headers["location"].split("/")[2]

    # Renaming keeps the history of what was used.
    profiles.save(profiles.get(v1.id).model_copy(update={"name": "Cabinet Dupont — Copropriété"}))
    page = plan_text(client.get(f"/dossiers/{dossier_id}/dossier").text)
    assert "Nom lors de l’analyse : Cabinet Dupont · nom actuel : Cabinet Dupont — Copropriété" in page
    assert env["dossiers"].get(dossier_id).profile_usage.profile_name == "Cabinet Dupont"

    # Deleting the profile breaks nothing.
    client.post(f"/profils/{v1.id}/supprimer")
    with pytest.raises(ProfileNotFoundError):
        profiles.get(v1.id)
    for identifier in (dossier_id, dossier_b):
        response = client.get(f"/dossiers/{identifier}/dossier")
        assert response.status_code == 200 and "Profil d’origine supprimé" in response.text
        assert client.get(f"/dossiers/{identifier}/lots").status_code == 200
    client.post(f"/dossiers/{dossier_id}/calques", data={"layer_name": COMMON, "status": "candidate"})
    assert [item.numero_propose for item in env["dossiers"].get(dossier_id).reconciliation.lot_proposals] == ["1", "2", "3", "4"]


# 34-35, 37. The quick snapshot carries the choice to the dossier.


def test_dossier_creation_reuses_the_choice_without_asking_again(env) -> None:
    profile = save(env, inferred(env["tmp"]))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    pending = env["client"].post(f"/analyses/{aid}/dossier", data={"reference": "X"}, follow_redirects=False)
    assert pending.headers["location"] == f"/analyses/{aid}/profil"  # never created before the choice
    assert env["client"].get(f"/analyses/{aid}", follow_redirects=False).headers["location"] == f"/analyses/{aid}/profil"
    choose(env, aid, profile.id)
    raw = json.loads((env["analyses"].root / f"{aid}.json").read_text(encoding="utf-8"))
    usage = raw["dossier"]["profile_usage"]
    assert usage["mode"] == "profil" and usage["profile_id"] == profile.id
    assert usage["revision"] == 1 and usage["content_hash"] == profile.content_hash and usage["rules"]
    created = env["client"].post(f"/analyses/{aid}/dossier", data={"reference": "P9001"}, follow_redirects=False)
    assert created.headers["location"].endswith("/dossier")
    dossier = env["dossiers"].get(created.headers["location"].split("/")[2])
    assert dossier.profile_usage.model_dump(mode="json") == usage
    assert len(env["inspections"]) == 1


# 36. One inspection, whatever the number of choices.


def test_choices_never_parse_the_plan_again(env) -> None:
    first = save(env, inferred(env["tmp"], "A"))
    second = save(env, without_role(inferred(env["tmp"], "B"), "partie_commune"))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    for choice in (first.id, "legacy", second.id, first.id):
        env["client"].get(f"/analyses/{aid}/profil")
        choose(env, aid, choice)
        env["client"].get(f"/analyses/{aid}")
    env["client"].get(f"/profils/nouveau?analyse={aid}&parcours=1")
    env["client"].post(f"/analyses/{aid}/dossier", data={"reference": "P1"})
    assert env["inspections"] == ["plan.dxf"]


# 38. Human decisions are never overwritten by a reconciliation.


def test_human_confirmations_survive_profiled_reconciliation(env) -> None:
    profile = save(env, inferred(env["tmp"]))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    choose(env, aid, profile.id)
    created = env["client"].post(f"/analyses/{aid}/dossier", data={"reference": "P9001"}, follow_redirects=False)
    dossier_id = created.headers["location"].split("/")[2]
    dossier = env["dossiers"].get(dossier_id)
    proposal = dossier.reconciliation.lot_proposals[0]
    lots_page = env["client"].get(f"/dossiers/{dossier_id}/lots?proposal_status=all").text
    assert 'name="level_code" required maxlength="60" value="RDC"' in lots_page  # level from the profile
    env["client"].post(
        f"/dossiers/{dossier_id}/lots/{proposal.id}/confirm",
        data={
            "candidate_ids": proposal.candidate_zone_ids[:1],
            "building_code": "A",
            "level_code": "RDC",
            "lot_number": "1",
            "category": "principale",
            "reason": "Contrôle visuel",
        },
    )
    confirmed = env["dossiers"].get(dossier_id)
    annex = next(
        item for item in confirmed.reconciliation.business_zone_candidates if item.numero_lot_propose == "1"
    )
    lot_id = next(lot.id for lot in confirmed.lots if lot.numero == "1")
    env["client"].post(f"/dossiers/{dossier_id}/annexes/{annex.candidate_zone_id}", data={"lot_id": lot_id, "level_code": "RDC"})
    env["client"].post(f"/dossiers/{dossier_id}/calques", data={"layer_name": COMMON, "status": "exclue"})
    final = env["dossiers"].get(dossier_id)
    kept = next(item for item in final.reconciliation.lot_proposals if item.id == proposal.id)
    assert kept.statut == VerificationStatus.CONFIRME_MANUEL and kept.decision_manuelle.motif == "Contrôle visuel"
    attached = next(item for item in final.reconciliation.business_zone_candidates if item.candidate_zone_id == annex.candidate_zone_id)
    assert attached.statut == VerificationStatus.CONFIRME_MANUEL


# 39-41. Responsive, themed and keyboard-accessible choice screen.


def test_choice_styles_use_tokens_and_visible_focus() -> None:
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    section = css[css.index("/* 8j. Choix du profil") : css.index("/* 9. Plan preview")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", section)
    assert ".choice-card:has(input:focus-visible)" in section and ".choice-card:has(input:checked)" in section
    assert "flex-wrap: wrap" in section and "min-width: 0" in section
    assert ".choice-label::after" in section  # the whole card is clickable


def test_choice_screen_is_accessible(env) -> None:
    save(env, inferred(env["tmp"]))
    save(env, without_role(inferred(env["tmp"], "Charte X"), "surface_principale"))
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    page = env["client"].get(f"/analyses/{aid}/profil").text
    assert '<fieldset id="choice-list"' in page and '<legend class="visually-hidden">Profil de dessin</legend>' in page
    radios = re.findall(r'<input type="radio" id="(choice-[^"]+)" name="profile"[^>]*aria-describedby="([^"]+)"', page)
    assert len(radios) == 3
    for radio_id, described in radios:
        assert f'<label for="{radio_id}"' in page and f'id="{described}"' in page
    # Tones are spelled out, not conveyed by colour alone.
    assert "Conforme : " in page and "Avertissement : " in page
    warned = choose(env, aid, re.search(r'id="choice-([a-f0-9]{32})"[^>]*>\s*<label[^>]*>\s*<span class="choice-name"><strong class="wrap-anywhere">Charte X', page).group(1))
    assert 'role="alert"' in warned.text


# 42. Identifiers, revisions and preferences are validated.


def test_identifiers_and_revisions_are_validated(env) -> None:
    client = env["client"]
    assert client.get("/analyses/" + "0" * 32 + "/profil").status_code == 404
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    save(env, inferred(env["tmp"]))
    assert choose(env, aid, "../../etc/passwd").status_code == 422
    assert client.post("/profils/" + "f" * 32 + "/defaut").status_code == 404
    with pytest.raises(ValueError):
        ProfileUsage(mode="profil", profile_id="f" * 32)
    with pytest.raises(ValueError):
        ProfileUsage(mode="legacy", profile_id="f" * 32)
    with pytest.raises(ValueError):
        DrawingProfile(name="x", revision=0)
    profile = inferred(env["tmp"])
    with pytest.raises(ValueError):
        ProfileUsage.model_validate({**usage_from_profile(profile).model_dump(), "content_hash": "../x"})
    preferences = env["profiles"].root / "meta" / "preferences.json"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text('{"default": "../../x", "last_used": {"zz": "nope"}}', encoding="utf-8")
    assert env["profiles"].default_id() is None and env["profiles"].last_used() == {}


# 43-44. Compatibility with stored data.


def test_schema_version_1_profiles_are_read_as_revision_1(env) -> None:
    profile = inferred(env["tmp"])
    legacy = profile.model_dump(mode="json")
    legacy["schema_version"] = 1
    del legacy["revision"]
    path = env["profiles"].root / f"{profile.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = env["profiles"].get(profile.id)
    assert (loaded.schema_version, loaded.revision) == (2, 1)
    assert env["profiles"].list()[0][0].id == profile.id


def test_dossiers_without_profile_usage_are_unchanged(env) -> None:
    fixture = ROOT / "tests/fixtures/copropriete_complete.json"
    raw = fixture.read_text(encoding="utf-8")
    dossier = Dossier.model_validate_json(raw)
    assert dossier.profile_usage is None
    reconcile_dossier(dossier)  # still the automatic engine
    assert dossier.reconciliation.profil_regles == "geometre-npg-v1"
    assert all(item.origine is None for item in dossier.reconciliation.preuves)

    def keys(value) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        if isinstance(value, list):
            return {key for item in value for key in keys(item)}
        return set()

    # Unset new fields are never written: the JSON, hence the validation
    # fingerprint, is the one produced before drawing profiles existed.
    assert not keys(json.loads(dossier.model_dump_json())) & {"profile_usage", "origine"}
    record_data_validation(dossier)
    env["dossiers"].save(dossier)
    reloaded = env["dossiers"].get(dossier.id)
    assert has_current_data_validation(reloaded)
    assert validation_snapshot_sha256(reloaded) == dossier.sha256_validation_donnees
    page = env["client"].get(f"/dossiers/{dossier.id}/dossier")
    assert page.status_code == 200
    assert "Analyse automatique / Legacy" in page.text and "antérieur aux profils" in page.text


# 29. Comparing many profiles stays light and is computed once per plan.


def test_many_profiles_are_compared_once_per_plan(env, monkeypatch: pytest.MonkeyPatch) -> None:
    base = inferred(env["tmp"])
    for index in range(40):
        save(env, base.model_copy(update={"id": f"{index:032x}", "name": f"Charte {index}", "lot_identification": base.lot_identification.model_copy(update={"radius": 5 + index})}))
    calls: list[int] = []
    real = main.CompatibilityCache.get

    def counting(self, *args, **kwargs):
        calls.append(1)
        return real(self, *args, **kwargs)

    from app.profiles import compatibility

    assessed: list[int] = []
    real_assess = compatibility.assess
    monkeypatch.setattr(compatibility, "assess", lambda *args: assessed.append(1) or real_assess(*args))
    monkeypatch.setattr(main.CompatibilityCache, "get", counting)
    _, aid = deposit(env, build_charter_dxf(env["tmp"] / "plan.dxf"))
    started = time.perf_counter()
    assert env["client"].get(f"/analyses/{aid}/profil").status_code == 200
    first = time.perf_counter() - started
    assert len(assessed) == 40 and first < 5
    env["client"].get(f"/analyses/{aid}/profil")
    assert len(assessed) == 40 and len(calls) == 80  # second display: cached
    assert len(env["inspections"]) == 1


def test_recommendation_never_breaks_ties() -> None:
    def item(level: str, identified: int, uncovered: int = 0) -> Compatibility:
        return Compatibility(level=level, identified=identified, uncovered_layers={"X": uncovered} if uncovered else {})

    assert recommend({"a": item("compatible", 8), "b": item("compatible", 8)}) is None
    assert recommend({"a": item("compatible", 8), "b": item("compatible", 6)}) == "a"
    assert recommend({"a": item("compatible", 8, 2), "b": item("compatible", 6)}) is None
    assert recommend({"a": item("avertissements", 8), "b": item("peu", 9)}) == "a"
    assert recommend({"a": item("peu", 8)}) is None


def test_unit_imposed_by_a_profile_is_traced_and_reversible(env) -> None:
    profile = save(env, inferred(env["tmp"]))
    plan = build_charter_dxf(env["tmp"] / "mm.dxf", units=4)
    _, aid = deposit(env, plan)
    warned = choose(env, aid, profile.id)
    assert "Le plan déclare l’unité « millimetre », le profil impose « metre »" in plan_text(warned.text)
    choose(env, aid, profile.id, confirm="1")
    dossier = env["analyses"].get(aid).dossier
    assert dossier.plan_importe.unite_retenue == "metre"
    decision = next(item for item in dossier.validations if item.champ == "unite_du_plan")
    assert "Unité imposée par le profil de dessin" in decision.justification
    assert "Unité imposée par le profil" in env["client"].get(f"/analyses/{aid}").text
    choose(env, aid, "legacy")
    restored = env["analyses"].get(aid).dossier
    assert restored.plan_importe.unite_retenue == "millimetre"
    assert [item.retenu for item in restored.validations if item.champ == "unite_du_plan"] == ["millimetre"]
