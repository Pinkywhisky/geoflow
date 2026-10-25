"""View model of the annex attachment list in the Lots & surfaces step."""

from __future__ import annotations

from app.domain import CategorieZone, Dossier, VerificationStatus
from app.plan_preview import build_zone_preview
from app.quick_analysis import evidence_reason

ANNEX_STATES = {
    "rattachee": "Rattachée",
    "proposee": "Lot proposé",
    "ambigue": "Plusieurs lots possibles",
    "non_rattachee": "Non rattachée",
}


def build_annex_rows(dossier: Dossier) -> dict[str, object]:
    """Annexes with their attachment state, suggestion and explanation."""

    reconciliation = dossier.reconciliation
    control = dossier.controle_technique
    if reconciliation is None or control is None:
        return {"rows": [], "counts": {key: 0 for key in ANNEX_STATES}, "pending_proposals": 0}
    candidate_by_id = {item.id: item for item in control.zones_candidates}
    evidence_by_id = {item.id: item for item in reconciliation.preuves}
    planche_by_id = {item.id: item for item in dossier.planches}
    zone_by_candidate = {zone.id.removeprefix("validee-"): zone for zone in dossier.zones}
    level_by_id = {item.id: item for item in dossier.niveaux}
    lot_by_id = {lot.id: lot for lot in dossier.lots}
    lot_by_number = {lot.numero: lot for lot in dossier.lots}

    rows = []
    for item in reconciliation.business_zone_candidates:
        if item.categorie_proposee != CategorieZone.SECONDAIRE_ANNEXE:
            continue
        candidate = candidate_by_id.get(item.candidate_zone_id)
        if candidate is None:
            continue
        zone = zone_by_candidate.get(item.candidate_zone_id)
        evidence = [evidence_by_id[key] for key in item.evidence_ids if key in evidence_by_id]
        if zone is not None and zone.lot_id in lot_by_id:
            state = "rattachee"
        elif item.statut == VerificationStatus.A_CONFIRMER and item.numero_lot_propose:
            state = "proposee"
        elif any(proof.source == "numeros_lot_concurrents" for proof in evidence):
            state = "ambigue"
        else:
            state = "non_rattachee"
        proposed_lot = lot_by_number.get(item.numero_lot_propose or "") if state == "proposee" else None
        reasons = [evidence_reason(proof) for proof in evidence]
        if state == "non_rattachee":
            reasons.append({"positive": False, "text": "Aucun lot proposé automatiquement."})
        planche = planche_by_id.get(candidate.planche_region)
        rows.append(
            {
                "candidate_id": candidate.id,
                "surface": candidate.surface_geometrique_m2,
                "planche": planche.titre if planche else "Planche à confirmer",
                "texts": [text for text in candidate.textes_proches if not text.strip().isdigit()][:2],
                "state": state,
                "state_label": ANNEX_STATES[state],
                "lot_id": zone.lot_id if state == "rattachee" else "",
                "level": (
                    level_by_id[zone.niveau_id].code
                    if zone is not None and zone.niveau_id in level_by_id
                    else ""
                ),
                "proposed_number": item.numero_lot_propose if state == "proposee" else None,
                "proposed_lot_id": proposed_lot.id if proposed_lot else None,
                "reasons": reasons,
                "preview": build_zone_preview(control.zones_candidates, reconciliation, candidate.id),
            }
        )
    order = {"proposee": 0, "ambigue": 1, "non_rattachee": 2, "rattachee": 3}
    # Suggestions that can be accepted right away (lot constituted) come first.
    rows.sort(
        key=lambda row: (
            order[row["state"]],
            row["state"] == "proposee" and row["proposed_lot_id"] is None,
            row["planche"],
            -(row["surface"] or 0),
            row["candidate_id"],
        )
    )
    counts = {key: sum(row["state"] == key for row in rows) for key in ANNEX_STATES}
    return {
        "rows": rows,
        "counts": counts,
        "pending_proposals": sum(row["proposed_lot_id"] is not None for row in rows),
    }
