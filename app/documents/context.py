"""Read-only transformation from the canonical dossier to a document context."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.domain import CategorieZone, Dossier, StatutValidationJuridique
from app.domain.models import StatutRevue


@dataclass(frozen=True)
class GenerationReadiness:
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    retained_surface_m2: float
    corrected_surface_count: int
    retained_zone_count: int

    @property
    def can_generate(self) -> bool:
        return not self.blockers


def assess_generation_readiness(dossier: Dossier) -> GenerationReadiness:
    """Check only canonical, user-validated data; never inspect source drawings."""

    blockers: list[str] = []
    warnings: list[str] = []
    plan = dossier.plan_importe
    retained_zones = [
        zone
        for zone in dossier.zones
        if zone.statut == StatutRevue.RETENUE
        and zone.categorie != CategorieZone.EXCLUE
    ]

    if dossier.type != "copropriete":
        blockers.append("Le dossier n'est pas de type copropriété.")
    if plan is None or not plan.unite_confirmee:
        blockers.append("L'unité du plan n'est pas confirmée.")
    if not any(planche.statut == StatutRevue.RETENUE for planche in dossier.planches):
        blockers.append("Aucune planche n'est marquée comme retenue.")
    if not retained_zones:
        blockers.append("Aucune surface retenue n'est disponible.")

    buildings = {item.id for item in dossier.batiments}
    levels = {item.id: item for item in dossier.niveaux}
    lots = {item.id: item for item in dossier.lots}
    zones = {item.id: item for item in dossier.zones}
    if len(buildings) != len(dossier.batiments) or len(levels) != len(
        dossier.niveaux
    ) or len(lots) != len(dossier.lots) or len(zones) != len(dossier.zones):
        blockers.append("Le dossier contient des identifiants structurels en double.")

    for level in dossier.niveaux:
        if level.batiment_id not in buildings:
            blockers.append(f"Le niveau {level.code} référence un bâtiment inconnu.")
    for lot in dossier.lots:
        if not lot.numero.strip():
            blockers.append("Un lot ne possède pas de numéro.")
        if any(building_id not in buildings for building_id in lot.batiment_ids):
            blockers.append(f"Le lot {lot.numero or lot.id} référence un bâtiment inconnu.")
        if any(level_id not in levels for level_id in lot.niveau_ids):
            blockers.append(f"Le lot {lot.numero or lot.id} référence un niveau inconnu.")
        for zone_id in lot.zone_ids:
            zone = zones.get(zone_id)
            if zone is None or zone.lot_id != lot.id:
                blockers.append(
                    f"Le lot {lot.numero or lot.id} contient une association de zone incohérente."
                )

    corrected_count = 0
    total = 0.0
    for zone in retained_zones:
        surface = zone.surface_retenue_m2
        if not math.isfinite(surface) or surface <= 0:
            blockers.append(f"La zone {zone.id} possède une surface retenue invalide.")
        else:
            total += surface
        if zone.batiment_id not in buildings or zone.niveau_id not in levels:
            blockers.append(f"La zone {zone.id} possède une structure incohérente.")
        elif levels[zone.niveau_id].batiment_id != zone.batiment_id:
            blockers.append(f"La zone {zone.id} associe un niveau au mauvais bâtiment.")
        if zone.categorie != CategorieZone.COMMUNE and not zone.lot_id:
            blockers.append(f"La zone retenue {zone.id} n'est affectée à aucun lot.")
        if zone.lot_id and zone.lot_id not in lots:
            blockers.append(f"La zone {zone.id} référence un lot inconnu.")
        if not math.isclose(
            zone.surface_geometrique_m2, zone.surface_retenue_m2, abs_tol=1e-9
        ):
            corrected_count += 1
            if not (zone.decision_surface.justification or "").strip():
                blockers.append(
                    f"La correction de surface de la zone {zone.id} n'est pas justifiée."
                )

    if not dossier.references_cadastrales:
        warnings.append("Les références cadastrales restent à renseigner.")
    if any(not (lot.usage or "").strip() for lot in dossier.lots):
        warnings.append("L'usage de certains lots reste à renseigner.")
    if any(
        item.statut_validation != StatutValidationJuridique.CONFIRME
        for item in dossier.servitudes
    ):
        warnings.append("Une ou plusieurs servitudes restent à confirmer.")
    if not dossier.milliemes:
        warnings.append("Les millièmes ne sont pas renseignés et ne sont pas calculés.")
    elif any(not item.valide for item in dossier.milliemes):
        warnings.append("Des millièmes présents restent à valider.")
    warnings.append("Les clauses juridiques restent à compléter et à valider.")
    if corrected_count:
        warnings.append(
            f"{corrected_count} correction(s) manuelle(s) de surface sont documentées."
        )

    return GenerationReadiness(
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
        retained_surface_m2=total,
        corrected_surface_count=corrected_count,
        retained_zone_count=len(retained_zones),
    )
