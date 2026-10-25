"""Annex-aware understanding, Model/Layout representations and annex attachment."""

from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.annex_view import build_annex_rows
from app.documents import assess_generation_readiness
from app.domain import (
    BBox,
    CandidateZone,
    CategorieZone,
    Planche,
    Sommet,
    StatutValidationDonnees,
    VerificationStatus,
)
from app.domain.models import StatutRevue
from app.dxf import inspect_dxf
from app.quick_analysis import build_quick_diagnostic
from app.reconciliation import reconcile_dossier
from app.storage import JsonDossierRepository
from app.workflow import (
    attach_annex,
    attach_proposed_annexes,
    confirm_lot_proposal,
    record_data_validation,
)

from .test_quick_analysis import snapshot_for
from .test_v042_reconciliation import (
    ANNEX,
    LOT_LAYER,
    PRINCIPAL,
    SURFACE_LAYER,
    provenance,
    synthetic_dossier,
    text,
)

TITLE = "PLAN D'IDENTIFICATION DES LOTS POUR MISE EN COPROPRIETE"


def rect(identifier: str, cx: float, cy: float, w: float, h: float, layer: str = PRINCIPAL, planche: str = "model"):
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    return CandidateZone(
        id=identifier,
        type_entite="LWPOLYLINE",
        calque=layer,
        handle_dxf=identifier,
        planche_region=planche,
        surface_geometrique_unites=w * h,
        surface_geometrique_m2=w * h,
        bbox=BBox(min_x=x0, min_y=y0, max_x=x1, max_y=y1),
        sommets=[Sommet(x=x0, y=y0), Sommet(x=x1, y=y0), Sommet(x=x1, y=y1), Sommet(x=x0, y=y1)],
        provenance=provenance(identifier, layer, planche),
    )


def lot_texts(prefix: str, number: str, x: float, y: float, surface: str | None, planche: str = "model"):
    texts = [
        text(f"{prefix}-lot", number, x, y, LOT_LAYER, planche),
        text(f"{prefix}-title", TITLE, x, y + 6, "01-CARTOUCHE", planche),
    ]
    if surface is not None:
        texts.append(text(f"{prefix}-surface", surface, x, y + 1, SURFACE_LAYER, planche))
    return texts


def lot_one(prefix: str = "m", dx: float = 0.0, planche: str = "model"):
    """Lot 1 = two principal contours of 4 m² and 6 m², annotated 10 m²."""
    return [
        rect(f"{prefix}-a", dx - 1, 0, 2, 2, planche=planche),
        rect(f"{prefix}-b", dx + 1.5, 0, 2, 3, planche=planche),
    ]


def with_layout(dossier, layout_id: str = "layout-p1", status: StatutRevue = StatutRevue.RETENUE):
    dossier.planches.append(
        Planche(
            id=layout_id,
            titre="Presentation : P1",
            statut=status,
            methode_detection="layout_dxf",
            bbox_region=BBox(min_x=-50, min_y=-50, max_x=500, max_y=500),
        )
    )
    return dossier


def proposal(dossier, number: str = "1"):
    return next(item for item in dossier.reconciliation.lot_proposals if item.numero_propose == number)


# ---------------------------------------------------------------- Phase 1


def annex_plan(extra_principal: int = 0):
    candidates = lot_one() + [rect(f"annex-{i}", 200 + i * 10, 200, 2, 2, ANNEX) for i in range(4)]
    candidates += [rect(f"orphan-{i}", 300 + i * 10, 300, 3, 3) for i in range(extra_principal)]
    return synthetic_dossier(candidates=candidates, texts=lot_texts("m", "1", 0, 0, "S=10.0m²"))


def test_unattached_annexes_do_not_degrade_understanding() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(annex_plan()))
    assert diagnostic["counts"]["unmatched_annex"] == 4
    assert diagnostic["understanding"]["key"] == "bonne"


def test_unattached_annexes_still_count_as_human_work() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(annex_plan()))
    assert diagnostic["workload"]["key"] in {"moyenne", "importante"}
    assert "4 annexe(s) à rattacher" in diagnostic["workload_reason"]
    assert "4 annexes à rattacher" in [item["title"] for item in diagnostic["checks"]]


def test_unmatched_principal_surfaces_remain_penalising() -> None:
    diagnostic = build_quick_diagnostic(snapshot_for(annex_plan(extra_principal=2)))
    assert diagnostic["counts"]["unmatched_principal"] == 2
    assert diagnostic["understanding"]["key"] == "partielle"


def test_plan_without_annexes_keeps_previous_levels() -> None:
    assert build_quick_diagnostic(snapshot_for(synthetic_dossier()))["understanding"]["key"] == "bonne"
    lonely = synthetic_dossier(candidates=[rect("only", 0, 0, 2, 5)])
    assert build_quick_diagnostic(snapshot_for(lonely))["understanding"]["key"] == "partielle"


def test_contradiction_with_annexes_is_never_good() -> None:
    dossier = annex_plan()
    dossier.controle_technique.textes[-1].contenu = "S=14m²"
    diagnostic = build_quick_diagnostic(snapshot_for(dossier))
    assert diagnostic["counts"]["contradictions"] == 1
    assert diagnostic["understanding"]["key"] == "partielle"


# ---------------------------------------------------------------- Phase 2


def test_paper_extent_never_classifies_model_entities(tmp_path: Path) -> None:
    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = 6
    document.layers.add(PRINCIPAL)
    document.modelspace().add_lwpolyline([(10, 10), (14, 10), (14, 13), (10, 13)], close=True, dxfattribs={"layer": PRINCIPAL})
    layout = document.layouts.new("Feuille 1")
    layout.add_lwpolyline([(0, 0), (400, 0), (400, 300), (0, 300)], close=True, dxfattribs={"layer": PRINCIPAL})
    path = tmp_path / "spaces.dxf"
    document.saveas(path)

    control, planches = inspect_dxf(path, "spaces.dxf")

    regions = sorted((item.planche_region, round(item.surface_geometrique_m2, 1)) for item in control.zones_candidates)
    assert regions == [("layout-feuille-1", 120000.0), ("model", 12.0)]


def test_model_text_inside_paper_extent_matches_model_contours() -> None:
    dossier = with_layout(synthetic_dossier(candidates=lot_one(), texts=lot_texts("m", "1", 0, 0, "S=10.0m²")))
    reconcile_dossier(dossier)
    item = proposal(dossier)
    assert item.candidate_zone_ids == ["m-a", "m-b"]
    assert item.statut == VerificationStatus.AUTO_VERIFIE


def test_same_object_in_model_and_layout_is_counted_once() -> None:
    candidates = lot_one() + lot_one("p", dx=100, planche="layout-p1")
    texts = lot_texts("m", "1", 0, 0, "S=10.0m²") + lot_texts("p", "1", 100, 0, "S=10.0m²", "layout-p1")
    dossier = with_layout(synthetic_dossier(candidates=candidates, texts=texts))
    reconcile_dossier(dossier)
    item = proposal(dossier)
    assert item.surface_geometrique_m2 == pytest.approx(10.0)
    assert item.surface_annotee_m2 == pytest.approx(10.0)
    assert item.candidate_zone_ids == ["m-a", "m-b"]
    assert item.candidate_zone_ids_ecartes == ["p-a", "p-b"]
    assert item.statut == VerificationStatus.A_CONFIRMER
    sources = {proof.source for proof in dossier.reconciliation.preuves if proof.id in item.evidence_ids}
    assert "representation_dupliquee" in sources


def test_different_zones_in_model_and_layout_are_both_kept() -> None:
    layout_zone = [rect("p-a", 100, 0, 3, 4, planche="layout-p1")]
    texts = lot_texts("m", "1", 0, 0, "S=10.0m²") + lot_texts("p", "1", 100, 0, "S=12.0m²", "layout-p1")
    dossier = with_layout(synthetic_dossier(candidates=lot_one() + layout_zone, texts=texts))
    reconcile_dossier(dossier)
    item = proposal(dossier)
    assert item.surface_geometrique_m2 == pytest.approx(22.0)
    assert item.candidate_zone_ids_ecartes == []


def test_equal_surfaces_with_different_shapes_are_not_merged() -> None:
    other = [rect("p-a", 99, 0, 1, 4, planche="layout-p1"), rect("p-b", 101.5, 0, 1, 6, planche="layout-p1")]
    texts = lot_texts("m", "1", 0, 0, "S=10.0m²") + lot_texts("p", "1", 100, 0, "S=10.0m²", "layout-p1")
    dossier = with_layout(synthetic_dossier(candidates=lot_one() + other, texts=texts))
    reconcile_dossier(dossier)
    item = proposal(dossier)
    assert item.surface_geometrique_m2 == pytest.approx(20.0)
    assert item.candidate_zone_ids_ecartes == []


def test_identical_levels_on_the_same_planche_are_not_merged() -> None:
    candidates = lot_one() + lot_one("r1", dx=100)
    texts = lot_texts("m", "1", 0, 0, "S=10.0m²") + lot_texts("r1", "1", 100, 0, "S=10.0m²")
    dossier = synthetic_dossier(candidates=candidates, texts=texts)
    reconcile_dossier(dossier)
    item = proposal(dossier)
    assert item.surface_geometrique_m2 == pytest.approx(20.0)
    assert item.surface_annotee_m2 == pytest.approx(20.0)
    assert item.candidate_zone_ids_ecartes == []


def test_unannotated_representation_is_set_aside_but_selectable() -> None:
    candidates = lot_one() + lot_one("v1", dx=100)
    texts = lot_texts("m", "1", 0, 0, "S=10.0m²") + lot_texts("v1", "1", 100, 0, None)
    dossier = synthetic_dossier(candidates=candidates, texts=texts)
    reconcile_dossier(dossier)
    item = proposal(dossier)
    assert item.surface_geometrique_m2 == pytest.approx(10.0)
    assert item.candidate_zone_ids_ecartes == ["v1-a", "v1-b"]
    assert item.statut == VerificationStatus.A_CONFIRMER
    confirm_lot_proposal(dossier, item.id, ["m-a", "m-b", "v1-a"], "A", "RdC", "1", "principale", "Duplex")
    assert {zone.id for zone in dossier.zones} == {"validee-m-a", "validee-m-b", "validee-v1-a"}


def test_without_any_annotation_occurrences_still_add_up() -> None:
    candidates = lot_one() + lot_one("r1", dx=100)
    texts = lot_texts("m", "1", 0, 0, None) + lot_texts("r1", "1", 100, 0, None)
    dossier = synthetic_dossier(candidates=candidates, texts=texts)
    reconcile_dossier(dossier)
    item = proposal(dossier)
    assert item.surface_geometrique_m2 == pytest.approx(20.0)
    assert item.statut == VerificationStatus.NON_RESOLU


def test_several_legitimate_layouts_keep_their_own_lots() -> None:
    candidates = lot_one("p", dx=0, planche="layout-p1") + lot_one("q", dx=0, planche="layout-p2")
    texts = lot_texts("p", "1", 0, 0, "S=10.0m²", "layout-p1") + lot_texts("q", "2", 0, 0, "S=10.0m²", "layout-p2")
    dossier = with_layout(with_layout(synthetic_dossier(candidates=candidates, texts=texts)), "layout-p2")
    reconcile_dossier(dossier)
    assert proposal(dossier, "1").candidate_zone_ids == ["p-a", "p-b"]
    assert proposal(dossier, "2").candidate_zone_ids == ["q-a", "q-b"]


def test_abandoned_layout_contours_are_excluded() -> None:
    candidates = lot_one() + lot_one("p", dx=100, planche="layout-p1")
    texts = lot_texts("m", "1", 0, 0, "S=10.0m²") + lot_texts("p", "1", 100, 0, "S=10.0m²", "layout-p1")
    dossier = with_layout(synthetic_dossier(candidates=candidates, texts=texts), status=StatutRevue.ABANDONNEE)
    reconcile_dossier(dossier)
    assert proposal(dossier).candidate_zone_ids == ["m-a", "m-b"]
    assert {"p-a", "p-b"} <= set(dossier.reconciliation.candidate_ids_exclus_techniquement)


def test_paperspace_only_plan_is_understood() -> None:
    candidates = lot_one("p", planche="layout-p1")
    dossier = synthetic_dossier(candidates=candidates, texts=lot_texts("p", "1", 0, 0, "S=10.0m²", "layout-p1"))
    dossier.planches = []
    with_layout(dossier)
    reconcile_dossier(dossier)
    assert proposal(dossier).statut == VerificationStatus.AUTO_VERIFIE


# ---------------------------------------------------------------- Phase 3


def annex_dossier():
    """Lots 1 and 2 side by side with annexes in every attachment situation."""
    candidates = lot_one() + lot_one("l2", dx=12) + [
        rect("annex-near-1", -1, -3, 2, 2, ANNEX),  # only lot 1 within radius
        rect("annex-far", 300, 300, 2, 2, ANNEX),  # no lot nearby
        rect("annex-between", 6, 0, 2, 2, ANNEX),  # both lots within radius, touches none
        rect("annex-touch-2", 9, 0, 2, 2, ANNEX),  # both within radius, touches lot 2 only
    ]
    texts = lot_texts("m", "1", 0, 0, "S=10.0m²") + lot_texts("l2", "2", 12, 0, "S=10.0m²")
    dossier = synthetic_dossier(candidates=candidates, texts=texts)
    reconcile_dossier(dossier)
    return dossier


def annex_state(dossier, candidate_id: str):
    return next(item for item in dossier.reconciliation.business_zone_candidates if item.candidate_zone_id == candidate_id)


def constituted(dossier):
    for number in ("1", "2"):
        item = proposal(dossier, number)
        confirm_lot_proposal(dossier, item.id, item.candidate_zone_ids, "A", "RdC", number, "principale", "Contrôle")
    return dossier


def test_annex_suggestions_are_conservative() -> None:
    dossier = annex_dossier()
    near = annex_state(dossier, "annex-near-1")
    assert (near.statut, near.numero_lot_propose) == (VerificationStatus.A_CONFIRMER, "1")
    far = annex_state(dossier, "annex-far")
    assert (far.statut, far.numero_lot_propose) == (VerificationStatus.NON_RESOLU, None)
    between = annex_state(dossier, "annex-between")
    assert between.numero_lot_propose is None
    assert any(proof.source == "numeros_lot_concurrents" for proof in dossier.reconciliation.preuves if proof.id in between.evidence_ids)
    touch = annex_state(dossier, "annex-touch-2")
    assert touch.numero_lot_propose == "2"
    assert any(proof.source == "contact_contour_lot" for proof in dossier.reconciliation.preuves if proof.id in touch.evidence_ids)
    assert not dossier.zones


def test_annex_rows_explain_each_state() -> None:
    rows = {row["candidate_id"]: row for row in build_annex_rows(constituted(annex_dossier()))["rows"]}
    assert rows["annex-near-1"]["state"] == "proposee"
    assert rows["annex-between"]["state"] == "ambigue"
    assert rows["annex-far"]["reasons"][-1]["text"] == "Aucun lot proposé automatiquement."
    assert rows["annex-touch-2"]["proposed_lot_id"] == "lot-2"


@pytest.fixture
def lots_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repository = JsonDossierRepository(tmp_path)
    monkeypatch.setattr(main, "repository", repository)
    dossier = constituted(annex_dossier())
    record_data_validation(dossier)
    repository.save(dossier)
    return TestClient(main.app), repository, dossier.id


def post_annex(client, dossier_id, candidate_id, **data):
    return client.post(
        f"/dossiers/{dossier_id}/annexes/{candidate_id}", data=data, headers={"Accept": "application/json"}
    )


def test_manual_attachment_is_persisted_and_invalidates_validation(lots_client) -> None:
    client, repository, dossier_id = lots_client
    response = post_annex(client, dossier_id, "annex-far", lot_id="lot-2", level_code="Sous-sol")
    assert response.status_code == 200

    reloaded = repository.get(dossier_id)
    zone = next(item for item in reloaded.zones if item.id == "validee-annex-far")
    assert zone.categorie == CategorieZone.SECONDAIRE_ANNEXE and zone.lot_id == "lot-2"
    assert zone.surface_retenue_m2 == pytest.approx(4.0)
    assert zone.geometrie_source.handle_dxf == "annex-far"
    lot = next(item for item in reloaded.lots if item.id == "lot-2")
    assert "validee-annex-far" in lot.zone_ids
    assert {level.code for level in reloaded.niveaux if level.id in lot.niveau_ids} == {"RdC", "Sous-sol"}
    assert reloaded.statut_validation_donnees == StatutValidationDonnees.A_VALIDER
    assert annex_state(reloaded, "annex-far").statut == VerificationStatus.CONFIRME_MANUEL


def test_attachment_can_move_and_be_removed_without_duplicates(lots_client) -> None:
    client, repository, dossier_id = lots_client
    post_annex(client, dossier_id, "annex-near-1", lot_id="lot-1")
    post_annex(client, dossier_id, "annex-near-1", lot_id="lot-1")
    post_annex(client, dossier_id, "annex-near-1", lot_id="lot-2", level_code="Cave")
    moved = repository.get(dossier_id)
    assert [zone.lot_id for zone in moved.zones if zone.id == "validee-annex-near-1"] == ["lot-2"]
    assert all("validee-annex-near-1" not in lot.zone_ids for lot in moved.lots if lot.id == "lot-1")

    post_annex(client, dossier_id, "annex-near-1", lot_id="")
    removed = repository.get(dossier_id)
    assert all(zone.id != "validee-annex-near-1" for zone in removed.zones)
    assert all("validee-annex-near-1" not in lot.zone_ids for lot in removed.lots)
    assert "Cave" not in {level.code for level in removed.niveaux}
    assert annex_state(removed, "annex-near-1").statut == VerificationStatus.A_CONFIRMER


def test_several_annexes_for_one_lot_keep_their_categories(lots_client) -> None:
    client, repository, dossier_id = lots_client
    for candidate_id in ("annex-near-1", "annex-far", "annex-between"):
        assert post_annex(client, dossier_id, candidate_id, lot_id="lot-1").status_code == 200
    dossier = repository.get(dossier_id)
    lot = next(item for item in dossier.lots if item.id == "lot-1")
    zones = {zone.id: zone for zone in dossier.zones}
    categories = sorted(zones[zone_id].categorie.value for zone_id in lot.zone_ids)
    assert categories == ["principale", "principale", "secondaire_annexe", "secondaire_annexe", "secondaire_annexe"]
    assert not [item for item in assess_generation_readiness(dossier).blockers if "annex" in item]


def test_invalid_attachment_requests_are_refused(lots_client) -> None:
    client, repository, dossier_id = lots_client
    unknown_lot = post_annex(client, dossier_id, "annex-far", lot_id="lot-404")
    assert unknown_lot.status_code == 400 and "Lot inconnu" in unknown_lot.json()["message"]
    principal = post_annex(client, dossier_id, "m-a", lot_id="lot-1")
    assert principal.status_code == 400 and "n'est pas une annexe" in principal.json()["message"]
    assert post_annex(client, dossier_id, "nope", lot_id="lot-1").status_code == 400
    assert post_annex(client, "inconnu", "annex-far", lot_id="lot-1").status_code == 404
    assert all(zone.categorie != CategorieZone.SECONDAIRE_ANNEXE for zone in repository.get(dossier_id).zones)


def test_bulk_acceptance_only_confirms_suggestions(lots_client) -> None:
    client, repository, dossier_id = lots_client
    response = client.post(f"/dossiers/{dossier_id}/annexes/confirm-proposals", follow_redirects=False)
    assert response.status_code == 303
    dossier = repository.get(dossier_id)
    attached = {zone.id: zone.lot_id for zone in dossier.zones if zone.categorie == CategorieZone.SECONDAIRE_ANNEXE}
    assert attached == {"validee-annex-near-1": "lot-1", "validee-annex-touch-2": "lot-2"}
    assert attach_proposed_annexes(dossier) == 0


def test_attached_annex_leaves_the_diagnostic_todo_list() -> None:
    dossier = constituted(annex_dossier())
    before = build_quick_diagnostic(snapshot_for(dossier))["counts"]["unmatched_annex"]
    attach_annex(dossier, "annex-far", "lot-1")
    after = build_quick_diagnostic(snapshot_for(dossier))["counts"]["unmatched_annex"]
    assert (before, after) == (4, 3)


def test_lots_page_lists_annexes_with_filters(lots_client) -> None:
    client, _, dossier_id = lots_client
    page = client.get(f"/dossiers/{dossier_id}/lots").text
    assert "Rattacher les annexes aux lots" in page
    assert "data-annex-filter" in page and "Accepter les 2 proposition(s)" in page
    assert "Aucun / non rattachée" in page


def test_draft_lists_attached_annexes_as_distinct_surfaces() -> None:
    from io import BytesIO

    from docx import Document

    from app.documents import generate_copropriete_draft

    dossier = constituted(annex_dossier())
    attach_annex(dossier, "annex-far", "lot-1", "Sous-sol")
    record_data_validation(dossier)
    generated = generate_copropriete_draft(dossier)
    text = "\n".join(
        cell.text
        for table in Document(BytesIO(generated.content)).tables
        for row in table.rows
        for cell in row.cells
    )
    assert "secondaire annexe" in text and "Sous-sol" in text


def test_concurrent_annex_autosaves_are_all_kept(lots_client) -> None:
    from concurrent.futures import ThreadPoolExecutor

    client, repository, dossier_id = lots_client
    targets = ["annex-near-1", "annex-far", "annex-between", "annex-touch-2"] * 2
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda cid: post_annex(client, dossier_id, cid, lot_id="lot-1").status_code, targets))
    assert set(codes) == {200}
    dossier = repository.get(dossier_id)
    attached = sorted(zone.id for zone in dossier.zones if zone.categorie == CategorieZone.SECONDAIRE_ANNEXE)
    assert attached == sorted(f"validee-{cid}" for cid in set(targets))
