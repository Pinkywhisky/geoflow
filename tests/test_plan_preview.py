from __future__ import annotations

import app.main as main
from app.domain import CandidateZone, Sommet, VerificationStatus
from app.plan_preview import (
    build_plan_overview,
    build_proposal_preview,
    contour_path,
)
from app.reconciliation import reconcile_dossier
from app.workflow import confirm_lot_proposal

from .test_v042_reconciliation import candidate, synthetic_dossier


def polygon(identifier: str, points, planche: str = "model") -> CandidateZone:
    base = candidate(identifier, 0, 1, planche=planche)
    return base.model_copy(
        update={"sommets": [Sommet(x=x, y=y, bulge=b) for x, y, b in points]}
    )


def test_contour_path_flips_y_axis_for_screen() -> None:
    square = polygon("sq", [(0, 0, 0), (2, 0, 0), (2, 3, 0), (0, 3, 0)])
    assert contour_path(square) == "M0 0L2 0L2 -3L0 -3L0 0Z"


def test_contour_path_draws_bulge_as_svg_arc() -> None:
    # A bulge of 1 is a half circle; chord 2 gives a radius of 1.
    half_disc = polygon("arc", [(0, 0, 1), (2, 0, 0)])
    path = contour_path(half_disc)
    assert "A1 1 0 0 1 2 0" in path
    reverse = polygon("arc-cw", [(0, 0, -2), (2, 0, 0)])
    radius = 2 * (1 + 4) / 8
    assert f"A{radius:g} {radius:g} 0 1 0 2 0" in contour_path(reverse)


def test_contour_path_falls_back_to_bounding_box() -> None:
    boxed = candidate("box", 5, 1)
    assert contour_path(boxed).startswith("M4.8 0.2H5.2V-0.2")


def test_proposal_preview_highlights_lot_and_crops_far_contours() -> None:
    dossier = synthetic_dossier(
        candidates=[candidate("zone-a", -1, 4), candidate("zone-b", 1, 6), candidate("far", 500, 6)]
    )
    reconcile_dossier(dossier)
    proposal = dossier.reconciliation.lot_proposals[0]
    preview = build_proposal_preview(
        dossier.controle_technique.zones_candidates, dossier.reconciliation, proposal
    )
    classes = {shape["id"]: shape["class"] for shape in preview["shapes"]}
    assert classes == {"zone-a": "is-ok is-focus", "zone-b": "is-ok is-focus"}
    assert [label["text"] for label in preview["labels"]] == ["1"]
    assert preview["labels"][0]["href"] is None


def test_overview_keeps_regions_apart_and_links_lots() -> None:
    dossier = synthetic_dossier(
        candidates=[
            candidate("zone-a", -1, 4),
            candidate("zone-b", 1, 6),
            candidate("layout-only", 1, 6, planche="Layout1"),
        ]
    )
    reconcile_dossier(dossier)
    previews = build_plan_overview(
        dossier.controle_technique.zones_candidates,
        dossier.reconciliation,
        lambda proposal: f"#proposal-{proposal.id}",
    )
    assert [preview["region"] for preview in previews] == ["model"]
    assert {shape["class"] for shape in previews[0]["shapes"]} == {"is-ok"}
    assert previews[0]["labels"][0]["href"].startswith("#proposal-")
    width, height = (float(value) for value in previews[0]["view_box"].split()[2:])
    assert width > 0 and height > 0


def _lots_step(dossier):
    return next(
        step for step in main._workflow_steps(dossier, "dossier") if step["id"] == "lots"
    )


def test_lots_step_is_done_only_when_reconciliation_is_resolved() -> None:
    dossier = synthetic_dossier()
    reconcile_dossier(dossier)
    proposal = dossier.reconciliation.lot_proposals[0]
    confirm_lot_proposal(
        dossier=dossier,
        proposal_id=proposal.id,
        candidate_ids=proposal.candidate_zone_ids,
        building_code="A",
        level_code="RdC",
        lot_number="1",
        category="principale",
        reason="Contrôle visuel",
    )
    assert dossier.zones
    assert _lots_step(dossier)["completed"] is True
    assert _lots_step(dossier)["pending"] == 0

    dossier.reconciliation.lot_proposals[0].statut = VerificationStatus.CONTRADICTOIRE
    step = _lots_step(dossier)
    assert step["completed"] is False
    assert step["pending"] == 1


def test_lots_page_renders_plan_preview(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.storage import JsonDossierRepository

    repository = JsonDossierRepository(tmp_path)
    monkeypatch.setattr(main, "repository", repository)
    dossier = synthetic_dossier()
    reconcile_dossier(dossier)
    repository.save(dossier)

    response = TestClient(main.app).get(
        f"/dossiers/{dossier.id}/lots?proposal_status=all"
    )
    assert response.status_code == 200
    assert "Aperçu du plan" in response.text
    assert 'class="plan-svg is-zoomed"' in response.text
    assert f'id="proposal-{dossier.reconciliation.lot_proposals[0].id}"' in response.text
