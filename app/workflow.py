"""Application services for the first copropriete workflow slice."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain import (
    AdressePostale,
    Batiment,
    CategorieZone,
    ControleTechnique,
    DecisionValidation,
    Dossier,
    GeometrieSource,
    Lot,
    ManualReconciliationDecision,
    Millieme,
    Niveau,
    Parcelle,
    Planche,
    PlanImporte,
    Provenance,
    StatutValidationDonnees,
    StatutValidationMillieme,
    VerificationStatus,
    Zone,
)
from app.domain.models import StatutRevue
from app.dxf import apply_confirmed_unit
from app.dxf.technical import meters_per_unit
from app.reconciliation import reconcile_dossier


def safe_filename(filename: str) -> str:
    # pathlib on Linux does not treat a Windows backslash as a separator.
    leaf = filename.replace(chr(92), "/").split("/")[-1]
    cleaned = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else "_"
        for character in leaf
    ).strip()
    return (cleaned or "plan")[:255]


def _safe_label(value: str, limit: int = 120) -> str:
    return " ".join(value.split())[:limit]


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub("[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower() or "valeur"


def create_dossier(reference: str, dossier_type: str) -> Dossier:
    if dossier_type != "copropriete":
        raise ValueError("Seuls les dossiers de copropriete sont disponibles.")
    clean_reference = _safe_label(reference)
    if not clean_reference:
        raise ValueError("La reference du dossier est obligatoire.")
    return Dossier(id=uuid4().hex, reference=clean_reference)


def _optional_label(value: str, limit: int) -> str | None:
    cleaned = _safe_label(value, limit)
    return cleaned or None


def update_dossier_details(
    dossier: Dossier,
    *,
    numero: str,
    voie: str,
    complement: str,
    code_postal: str,
    commune: str,
    departement: str,
    date_plan: str,
) -> None:
    address = AdressePostale(
        numero=_optional_label(numero, 20),
        voie=_optional_label(voie, 180),
        complement=_optional_label(complement, 180),
        code_postal=_optional_label(code_postal, 12),
    )
    dossier.adresse = None if address.est_vide else address
    dossier.commune = _optional_label(commune, 120)
    dossier.departement = _optional_label(departement, 120)
    dossier.date_plan = _optional_label(date_plan, 40)
    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)


def _validated_parcel(
    commune: str,
    section: str,
    numero: str,
) -> Parcelle:
    clean_commune = _optional_label(commune, 120)
    clean_section = _optional_label(section, 30)
    clean_number = _optional_label(numero, 60)
    if not clean_commune or not clean_section or not clean_number:
        raise ValueError(
            "La commune, la section et le numéro de parcelle sont obligatoires."
        )
    return Parcelle(
        commune=clean_commune,
        section=clean_section,
        numero=clean_number,
    )


def add_parcel(
    dossier: Dossier,
    *,
    commune: str,
    section: str,
    numero: str,
) -> None:
    dossier.references_cadastrales.append(
        _validated_parcel(commune, section, numero)
    )
    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)


def update_parcel(
    dossier: Dossier,
    index: int,
    *,
    commune: str,
    section: str,
    numero: str,
) -> None:
    if index < 0 or index >= len(dossier.references_cadastrales):
        raise ValueError("Référence cadastrale inconnue.")
    dossier.references_cadastrales[index] = _validated_parcel(
        commune, section, numero
    )
    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)


def remove_parcel(dossier: Dossier, index: int) -> None:
    if index < 0 or index >= len(dossier.references_cadastrales):
        raise ValueError("Référence cadastrale inconnue.")
    dossier.references_cadastrales.pop(index)
    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)


def update_lot_metadata(
    dossier: Dossier,
    lot_id: str,
    *,
    usage: str,
    designation: str,
    millieme_value: str,
    millieme_base: str,
    millieme_status: str,
) -> None:
    lot = next((item for item in dossier.lots if item.id == lot_id), None)
    if lot is None:
        raise ValueError("Lot inconnu.")
    lot.usage = _optional_label(usage, 120)
    lot.designation = _optional_label(designation, 1000)

    existing = next(
        (item for item in dossier.milliemes if item.lot_id == lot_id),
        None,
    )
    if not millieme_value.strip():
        dossier.milliemes = [
            item for item in dossier.milliemes if item.lot_id != lot_id
        ]
    else:
        try:
            value = int(millieme_value)
            base = int(millieme_base or "1000")
        except ValueError as exc:
            raise ValueError("Les millièmes doivent être des nombres entiers.") from exc
        if value < 0 or base <= 0:
            raise ValueError("Les millièmes et leur base doivent être positifs.")
        status = StatutValidationMillieme(millieme_status)
        if existing is None:
            dossier.milliemes.append(
                Millieme(
                    lot_id=lot_id,
                    valeur=value,
                    base=base,
                    statut_validation=status,
                )
            )
        else:
            existing.valeur = value
            existing.base = base
            existing.statut_validation = status

    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)


def set_millieme_grid_complete(dossier: Dossier, complete: bool) -> None:
    dossier.grille_milliemes_complete = complete
    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)


def validation_snapshot(dossier: Dossier) -> bytes:
    """Return the canonical business payload covered by data validation."""

    payload = dossier.model_dump(
        mode="json",
        exclude={
            "statut",
            "statut_validation_donnees",
            "date_validation_donnees",
            "sha256_validation_donnees",
            "generations",
        },
    )
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (serialized + "\n").encode("utf-8")


def validation_snapshot_sha256(dossier: Dossier) -> str:
    return hashlib.sha256(validation_snapshot(dossier)).hexdigest()


def invalidate_data_validation(dossier: Dossier) -> None:
    """Mark changed canonical data for a new user validation."""

    dossier.statut_validation_donnees = StatutValidationDonnees.A_VALIDER
    dossier.date_validation_donnees = None
    dossier.sha256_validation_donnees = None


def record_data_validation(dossier: Dossier) -> None:
    dossier.statut_validation_donnees = StatutValidationDonnees.VALIDE
    dossier.date_validation_donnees = datetime.now(timezone.utc)
    dossier.sha256_validation_donnees = validation_snapshot_sha256(dossier)


def has_current_data_validation(dossier: Dossier) -> bool:
    return (
        dossier.statut_validation_donnees == StatutValidationDonnees.VALIDE
        and dossier.sha256_validation_donnees
        == validation_snapshot_sha256(dossier)
    )


def attach_import(
    dossier: Dossier,
    filename: str,
    file_type: str,
    control: ControleTechnique,
    planches: list[Planche],
) -> None:
    clean_filename = safe_filename(filename)
    unit_supported = meters_per_unit(control.unite_detectee) is not None
    dossier.plan_importe = PlanImporte(
        nom_fichier_original=clean_filename,
        type_fichier=file_type,
        version_dxf=control.version_dxf,
        unite_detectee=control.unite_detectee,
        unite_retenue=control.unite_detectee if unit_supported else None,
        unite_confirmee=unit_supported,
        bbox=control.bbox,
    )
    # Importing another plan starts a new geometric review in this MVP.
    dossier.controle_technique = control
    dossier.planches = planches
    dossier.batiments = []
    dossier.niveaux = []
    dossier.lots = []
    dossier.zones = []
    dossier.reconciliation = None
    if unit_supported:
        apply_confirmed_unit(control, control.unite_detectee)
        dossier.validations = [
            DecisionValidation(
                champ="unite_du_plan",
                propose=control.unite_detectee,
                retenu=control.unite_detectee,
            )
        ]
    else:
        dossier.validations = []
    dossier.statut = "controle_technique"
    reconcile_dossier(dossier)
    invalidate_data_validation(dossier)


def confirm_unit(dossier: Dossier, retained_unit: str, justification: str = "") -> None:
    if dossier.plan_importe is None or dossier.controle_technique is None:
        raise ValueError("Aucun plan importe.")
    detected = dossier.plan_importe.unite_detectee
    decision = DecisionValidation(
        champ="unite_du_plan",
        propose=detected,
        retenu=retained_unit,
        justification=justification or None,
    )
    apply_confirmed_unit(dossier.controle_technique, retained_unit)
    dossier.plan_importe.unite_retenue = retained_unit
    dossier.plan_importe.unite_confirmee = True
    dossier.validations = [
        validation
        for validation in dossier.validations
        if validation.champ != "unite_du_plan"
    ]
    dossier.validations.append(decision)
    dossier.statut = "selection_planches"
    reconcile_dossier(dossier)
    invalidate_data_validation(dossier)


def set_planche_status(
    dossier: Dossier, planche_id: str, status: StatutRevue
) -> None:
    planche = next((item for item in dossier.planches if item.id == planche_id), None)
    if planche is None:
        raise ValueError("Planche inconnue.")
    planche.statut = status
    reconcile_dossier(dossier)
    invalidate_data_validation(dossier)


def set_layer_status(
    dossier: Dossier, layer_name: str, status: StatutRevue
) -> None:
    if dossier.controle_technique is None:
        raise ValueError("Aucun controle technique.")
    layer = next(
        (item for item in dossier.controle_technique.calques if item.nom == layer_name),
        None,
    )
    if layer is None:
        raise ValueError("Calque inconnu.")
    layer.statut = status
    reconcile_dossier(dossier)
    invalidate_data_validation(dossier)


def associate_candidate(
    dossier: Dossier,
    candidate_id: str,
    building_code: str,
    level_code: str,
    lot_number: str | None,
    category: str,
    retained_surface: float | None = None,
    justification: str = "",
) -> Zone:
    plan = dossier.plan_importe
    control = dossier.controle_technique
    if plan is None or control is None or not plan.unite_confirmee:
        raise ValueError("Confirmez l'unite avant de valider les zones.")
    candidate = next(
        (item for item in control.zones_candidates if item.id == candidate_id), None
    )
    if candidate is None:
        raise ValueError("Zone candidate inconnue.")
    if candidate.surface_geometrique_m2 is None:
        raise ValueError("La surface ne peut pas etre convertie en metres carres.")
    if any(zone.id == f"validee-{candidate.id}" for zone in dossier.zones):
        raise ValueError("Cette zone candidate est deja associee.")

    building_code = _safe_label(building_code, 60)
    level_code = _safe_label(level_code, 60)
    lot_number = _safe_label(lot_number or "", 60) or None
    if not building_code or not level_code:
        raise ValueError("Le batiment et le niveau sont obligatoires.")
    category_value = CategorieZone(category)
    geometric_surface = candidate.surface_geometrique_m2
    selected_surface = (
        geometric_surface if retained_surface is None else float(retained_surface)
    )
    # Validate a correction before mutating the dossier collections.
    decision = DecisionValidation(
        champ="surface_retenue_m2",
        propose=geometric_surface,
        retenu=selected_surface,
        justification=justification or None,
    )

    building = next(
        (item for item in dossier.batiments if item.code == building_code), None
    )
    if building is None:
        building = Batiment(
            id=f"batiment-{_slug(building_code)}", code=building_code
        )
        dossier.batiments.append(building)

    level = next(
        (
            item
            for item in dossier.niveaux
            if item.batiment_id == building.id and item.code == level_code
        ),
        None,
    )
    if level is None:
        level = Niveau(
            id=f"niveau-{_slug(building.code)}-{_slug(level_code)}",
            batiment_id=building.id,
            code=level_code,
        )
        dossier.niveaux.append(level)

    lot: Lot | None = None
    if lot_number is not None:
        lot = next((item for item in dossier.lots if item.numero == lot_number), None)
        if lot is None:
            lot = Lot(id=f"lot-{_slug(lot_number)}", numero=lot_number)
            dossier.lots.append(lot)
        if building.id not in lot.batiment_ids:
            lot.batiment_ids.append(building.id)
        if level.id not in lot.niveau_ids:
            lot.niveau_ids.append(level.id)

    geometry = GeometrieSource(
        fichier_source=candidate.provenance.fichier_source,
        handle_dxf=candidate.handle_dxf,
        calque=candidate.calque,
        type_entite=candidate.type_entite,
        planche_region=candidate.planche_region,
        sommets=candidate.sommets,
        provenance=candidate.provenance,
    )
    zone = Zone(
        id=f"validee-{candidate.id}",
        batiment_id=building.id,
        niveau_id=level.id,
        lot_id=lot.id if lot else None,
        categorie=category_value,
        surface_geometrique_m2=geometric_surface,
        surface_retenue_m2=selected_surface,
        statut=(
            StatutRevue.EXCLUE
            if category_value == CategorieZone.EXCLUE
            else StatutRevue.RETENUE
        ),
        geometrie_source=geometry,
        decision_surface=decision,
        provenance_association=Provenance(
            fichier_source=plan.nom_fichier_original,
            handle_dxf=candidate.handle_dxf,
            calque=candidate.calque,
            type_entite=candidate.type_entite,
            planche_region=candidate.planche_region,
            methode_detection="saisie_utilisateur",
        ),
    )
    dossier.zones.append(zone)
    candidate.statut = zone.statut
    if lot is not None:
        lot.zone_ids.append(zone.id)
    dossier.validations.append(decision)
    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)
    return zone


def mark_lot_proposal_for_review(
    dossier: Dossier, proposal_id: str, reason: str
) -> None:
    reconciliation = dossier.reconciliation
    if reconciliation is None:
        raise ValueError("Aucune réconciliation n'est disponible.")
    proposal = next(
        (item for item in reconciliation.lot_proposals if item.id == proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("Proposition de lot inconnue.")
    clean_reason = _safe_label(reason, 300)
    if not clean_reason:
        raise ValueError("Le motif de réexamen est obligatoire.")
    proposal.decision_manuelle = ManualReconciliationDecision(
        statut=VerificationStatus.A_REVOIR,
        numero_retenu=proposal.numero_propose,
        candidate_zone_ids_retenus=proposal.candidate_zone_ids,
        surface_retenue_m2=proposal.surface_geometrique_m2,
        motif=clean_reason,
    )
    proposal.statut = VerificationStatus.A_REVOIR
    reconcile_dossier(dossier)
    invalidate_data_validation(dossier)


def confirm_lot_proposal(
    dossier: Dossier,
    proposal_id: str,
    candidate_ids: list[str],
    building_code: str,
    level_code: str,
    lot_number: str,
    category: str,
    reason: str,
) -> None:
    reconciliation = dossier.reconciliation
    control = dossier.controle_technique
    if reconciliation is None or control is None:
        raise ValueError("Aucune réconciliation n'est disponible.")
    proposal = next(
        (item for item in reconciliation.lot_proposals if item.id == proposal_id),
        None,
    )
    if proposal is None:
        raise ValueError("Proposition de lot inconnue.")

    selected_ids = list(dict.fromkeys(candidate_ids))
    if not selected_ids:
        raise ValueError("Sélectionnez au moins une zone pour confirmer le lot.")
    if any(item not in proposal.candidate_zone_ids for item in selected_ids):
        raise ValueError("Une zone sélectionnée ne fait pas partie de la proposition.")
    clean_building = _safe_label(building_code, 60)
    clean_level = _safe_label(level_code, 60)
    clean_lot = _safe_label(lot_number, 60)
    clean_reason = _safe_label(reason, 300)
    CategorieZone(category)
    if not clean_building or not clean_level or not clean_lot:
        raise ValueError("Le bâtiment, le niveau et le numéro de lot sont obligatoires.")
    if not clean_reason:
        raise ValueError("Le motif de la décision manuelle est obligatoire.")

    candidates = {
        item.id: item for item in control.zones_candidates if item.id in selected_ids
    }
    if len(candidates) != len(selected_ids):
        raise ValueError("Une zone sélectionnée est inconnue.")
    existing_by_candidate = {
        zone.id.removeprefix("validee-"): zone for zone in dossier.zones
    }
    for candidate_id in selected_ids:
        existing = existing_by_candidate.get(candidate_id)
        if existing is not None:
            existing_lot = next(
                (item for item in dossier.lots if item.id == existing.lot_id), None
            )
            if existing_lot is None or existing_lot.numero != clean_lot:
                raise ValueError("Une zone sélectionnée est déjà affectée à un autre lot.")

    for candidate_id in selected_ids:
        if candidate_id in existing_by_candidate:
            continue
        associate_candidate(
            dossier=dossier,
            candidate_id=candidate_id,
            building_code=clean_building,
            level_code=clean_level,
            lot_number=clean_lot,
            category=category,
        )

    retained_surface = sum(
        candidates[candidate_id].surface_geometrique_m2 or 0.0
        for candidate_id in selected_ids
    )
    proposal.decision_manuelle = ManualReconciliationDecision(
        statut=VerificationStatus.CONFIRME_MANUEL,
        numero_retenu=clean_lot,
        candidate_zone_ids_retenus=selected_ids,
        surface_retenue_m2=retained_surface,
        motif=clean_reason,
    )
    proposal.statut = VerificationStatus.CONFIRME_MANUEL
    reconcile_dossier(dossier)
    dossier.statut = "en_validation"
    invalidate_data_validation(dossier)
