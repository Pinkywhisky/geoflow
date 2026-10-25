"""Lightweight SVG previews of the closed contours detected in a plan.

The preview only reads canonical data (candidate zones and the reconciliation
result). It never touches the source DXF, so it stays available for dossiers
created from a quick analysis snapshot.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping

from app.domain import (
    CandidateZone,
    LotProposal,
    ReconciliationResult,
    VerificationStatus,
)

MAX_OVERVIEW_SHAPES = 2500
FOCUS_MARGIN_RATIO = 0.6

STATUS_CLASSES = {
    VerificationStatus.AUTO_VERIFIE: "is-ok",
    VerificationStatus.CONFIRME_MANUEL: "is-ok",
    VerificationStatus.A_CONFIRMER: "is-review",
    VerificationStatus.NON_RESOLU: "is-review",
    VerificationStatus.A_REVOIR: "is-review",
    VerificationStatus.CONTRADICTOIRE: "is-conflict",
}

Bounds = tuple[float, float, float, float]


def _fmt(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _bounds(candidate: CandidateZone) -> Bounds | None:
    if candidate.sommets:
        xs = [vertex.x for vertex in candidate.sommets]
        ys = [vertex.y for vertex in candidate.sommets]
        return min(xs), min(ys), max(xs), max(ys)
    if candidate.bbox is not None:
        box = candidate.bbox
        return box.min_x, box.min_y, box.max_x, box.max_y
    return None


def _union(boxes: Iterable[Bounds]) -> Bounds | None:
    boxes = list(boxes)
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _intersects(box: Bounds, other: Bounds) -> bool:
    return not (
        box[2] < other[0] or box[0] > other[2] or box[3] < other[1] or box[1] > other[3]
    )


def contour_path(candidate: CandidateZone) -> str | None:
    """SVG path of a closed contour, Y axis flipped, DXF bulges as arcs."""
    vertices = candidate.sommets
    if len(vertices) < 2:
        bounds = _bounds(candidate)
        if bounds is None:
            return None
        min_x, min_y, max_x, max_y = bounds
        return (
            f"M{_fmt(min_x)} {_fmt(-min_y)}H{_fmt(max_x)}"
            f"V{_fmt(-max_y)}H{_fmt(min_x)}Z"
        )
    parts = [f"M{_fmt(vertices[0].x)} {_fmt(-vertices[0].y)}"]
    for index, start in enumerate(vertices):
        end = vertices[(index + 1) % len(vertices)]
        if abs(start.bulge) < 1e-12:
            parts.append(f"L{_fmt(end.x)} {_fmt(-end.y)}")
            continue
        chord = math.hypot(end.x - start.x, end.y - start.y)
        if chord < 1e-12:
            continue
        radius = chord * (1 + start.bulge**2) / (4 * abs(start.bulge))
        large_arc = 1 if abs(start.bulge) > 1 else 0
        # A positive bulge turns counter-clockwise in DXF space, which becomes
        # clockwise once the Y axis is flipped for the screen: SVG sweep 1.
        sweep = 1 if start.bulge > 0 else 0
        parts.append(
            f"A{_fmt(radius)} {_fmt(radius)} 0 {large_arc} {sweep} "
            f"{_fmt(end.x)} {_fmt(-end.y)}"
        )
    parts.append("Z")
    return "".join(parts)


def _view_box(bounds: Bounds, margin_ratio: float) -> dict[str, float]:
    min_x, min_y, max_x, max_y = bounds
    width = max(max_x - min_x, 1e-6)
    height = max(max_y - min_y, 1e-6)
    margin = max(width, height) * margin_ratio
    return {
        "x": min_x - margin,
        "y": -(max_y + margin),
        "width": width + 2 * margin,
        "height": height + 2 * margin,
    }


def _shape_class(
    candidate_id: str,
    status_by_candidate: Mapping[str, VerificationStatus],
    excluded: set[str],
    focus: set[str],
) -> str:
    if candidate_id in excluded:
        state = "is-excluded"
    else:
        status = status_by_candidate.get(candidate_id)
        state = STATUS_CLASSES.get(status, "is-context") if status else "is-context"
    # The focused lot keeps its status colour so a contradiction stays red.
    return f"{state} is-focus" if candidate_id in focus else state


def _render(
    candidates: list[CandidateZone],
    bounds: Bounds,
    margin_ratio: float,
    reconciliation: ReconciliationResult | None,
    focus: set[str],
    label_proposals: list[LotProposal],
    link_for: Callable[[LotProposal], str] | None,
    font_ratio: float = 0.035,
    class_override: Mapping[str, str] | None = None,
) -> dict[str, object]:
    status_by_candidate: dict[str, VerificationStatus] = {}
    excluded: set[str] = set()
    if reconciliation is not None:
        excluded = set(reconciliation.candidate_ids_exclus_techniquement)
        for proposal in reconciliation.lot_proposals:
            for candidate_id in proposal.candidate_zone_ids:
                status_by_candidate[candidate_id] = proposal.statut

    view = _view_box(bounds, margin_ratio)
    shapes = []
    for candidate in candidates:
        path = contour_path(candidate)
        if path is None:
            continue
        area = candidate.surface_geometrique_m2
        shapes.append(
            {
                "id": candidate.id,
                "d": path,
                "class": (
                    class_override.get(candidate.id, "is-context")
                    if class_override is not None
                    else _shape_class(candidate.id, status_by_candidate, excluded, focus)
                ),
                "title": f"{candidate.id} · {candidate.calque}"
                + (f" · {area:.2f} m²" if area is not None else ""),
            }
        )
    # Focused shapes are drawn last so their outline stays on top.
    shapes.sort(key=lambda shape: shape["class"].endswith("is-focus"))

    by_id = {candidate.id: candidate for candidate in candidates}
    labels = []
    for proposal in label_proposals:
        boxes = [
            box
            for candidate_id in proposal.candidate_zone_ids
            if candidate_id in by_id and (box := _bounds(by_id[candidate_id]))
        ]
        union = _union(boxes)
        if union is None:
            continue
        labels.append(
            {
                "x": (union[0] + union[2]) / 2,
                "y": -(union[1] + union[3]) / 2,
                "text": proposal.numero_propose,
                "href": link_for(proposal) if link_for else None,
                "class": STATUS_CLASSES.get(proposal.statut, "is-context"),
            }
        )
    return {
        "view_box": " ".join(
            _fmt(view[key]) for key in ("x", "y", "width", "height")
        ),
        "font_size": max(view["width"], view["height"]) * font_ratio,
        "shapes": shapes,
        "labels": labels,
    }


def build_plan_overview(
    candidates: list[CandidateZone],
    reconciliation: ReconciliationResult | None,
    link_for: Callable[[LotProposal], str] | None = None,
) -> list[dict[str, object]]:
    """One preview per drawing region that holds lot proposals.

    Model space and paper-space layouts use unrelated coordinate systems, so
    regions are never drawn on top of each other.
    """
    proposals = reconciliation.lot_proposals if reconciliation else []
    proposal_candidates = {
        candidate_id
        for proposal in proposals
        for candidate_id in proposal.candidate_zone_ids
    }
    regions: dict[str, list[CandidateZone]] = {}
    for candidate in candidates:
        regions.setdefault(candidate.planche_region, []).append(candidate)
    relevant = [
        region
        for region, items in regions.items()
        if any(item.id in proposal_candidates for item in items)
    ] or list(regions)

    previews = []
    for region in relevant:
        items = regions[region]
        truncated = len(items) > MAX_OVERVIEW_SHAPES
        if truncated:
            items = [item for item in items if item.id in proposal_candidates]
        bounds = _union(box for item in items if (box := _bounds(item)))
        if bounds is None:
            continue
        region_ids = {item.id for item in items}
        region_proposals = [
            proposal
            for proposal in proposals
            if any(candidate_id in region_ids for candidate_id in proposal.candidate_zone_ids)
        ]
        preview = _render(
            items, bounds, 0.04, reconciliation, set(), region_proposals, link_for
        )
        preview.update(
            region=region,
            contour_count=len(items),
            truncated=truncated,
        )
        previews.append(preview)
    return previews


def _focus_preview(
    candidates: list[CandidateZone],
    reconciliation: ReconciliationResult | None,
    focus_ids: list[str],
    labels: Callable[[set[str]], list[LotProposal]],
) -> dict[str, object] | None:
    """Zoom on ``focus_ids`` with the neighbouring contours as context."""

    by_id = {candidate.id: candidate for candidate in candidates}
    focus = [by_id[item] for item in focus_ids if item in by_id]
    focus_bounds = _union(box for item in focus if (box := _bounds(item)))
    if focus_bounds is None:
        return None
    region = focus[0].planche_region
    view = _view_box(focus_bounds, FOCUS_MARGIN_RATIO)
    window = (
        view["x"],
        -(view["y"] + view["height"]),
        view["x"] + view["width"],
        -view["y"],
    )
    context = [
        candidate
        for candidate in candidates
        if candidate.planche_region == region
        and (box := _bounds(candidate)) is not None
        and _intersects(box, window)
    ]
    return _render(
        context,
        focus_bounds,
        FOCUS_MARGIN_RATIO,
        reconciliation,
        {item.id for item in focus},
        labels({item.id for item in context}),
        None,
        font_ratio=0.1,
    )


def build_proposal_preview(
    candidates: list[CandidateZone],
    reconciliation: ReconciliationResult | None,
    proposal: LotProposal,
) -> dict[str, object] | None:
    """Zoomed preview of one proposal, with neighbouring contours as context."""
    return _focus_preview(
        candidates, reconciliation, proposal.candidate_zone_ids, lambda _: [proposal]
    )


def build_zone_preview(
    candidates: list[CandidateZone],
    reconciliation: ReconciliationResult | None,
    candidate_id: str,
) -> dict[str, object] | None:
    """Zoom on one zone, labelled with the lots drawn around it."""

    def neighbours(visible: set[str]) -> list[LotProposal]:
        proposals = reconciliation.lot_proposals if reconciliation else []
        return [
            proposal
            for proposal in proposals
            if visible.intersection(proposal.candidate_zone_ids)
        ]

    return _focus_preview(candidates, reconciliation, [candidate_id], neighbours)


def build_class_preview(
    candidates: list[CandidateZone], classes: Mapping[str, str]
) -> dict[str, object] | None:
    """Overview of the region holding most classified contours, coloured by
    the given classes (e.g. layer roles of a drawing profile)."""

    regions = Counter(item.planche_region for item in candidates if item.id in classes)
    if not regions:
        return None
    region = max(regions, key=lambda item: (regions[item], item == "model"))
    items = [item for item in candidates if item.planche_region == region]
    if len(items) > MAX_OVERVIEW_SHAPES:
        items = [item for item in items if item.id in classes]
    bounds = _union(box for item in items if (box := _bounds(item)))
    if bounds is None:
        return None
    return _render(items, bounds, 0.04, None, set(), [], None, class_override=classes)
