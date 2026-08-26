"""Read-only transformation from the canonical dossier to a document context."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from app.domain import (
    CategorieZone,
    Dossier,
    GlobalCheckStatus,
    StatutValidationMillieme,
    StatutValidationJuridique,
)
from app.domain.models import StatutRevue


@dataclass(frozen=True)
class GenerationNotice:
    code: str
    message: str
    kind: Literal["action", "product", "information"]
    href: str | None = None
    action_label: str | None = None


@dataclass(frozen=True)
class GenerationReadiness:
    blockers: tuple[str, ...]
    user_actions: tuple[GenerationNotice, ...]
    product_limitations: tuple[GenerationNotice, ...]
    information: tuple[GenerationNotice, ...]
    retained_surface_m2: float
    corrected_surface_count: int
    retained_zone_count: int

    @property
    def can_generate(self) -> bool:
        return not self.blockers

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            item.message
            for item in (
                *self.user_actions,
                *self.product_limitations,
                *self.information,
            )
        )


def assess_generation_readiness(dossier: Dossier) -> GenerationReadiness:
    """Check only canonical, user-validated data; never inspect source drawings."""

    blockers: list[str] = []
    user_actions: list[GenerationNotice] = []
    product_limitations: list[GenerationNotice] = []
    information: list[GenerationNotice] = []
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
    if dossier.reconciliation is not None:
        for check in dossier.reconciliation.controles_globaux:
            if check.statut == GlobalCheckStatus.BLOQUANT:
                blockers.append(f"Réconciliation : {check.message}")

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

    dossier_url = f"/dossiers/{dossier.id}"
    if (
        dossier.adresse is None
        or dossier.adresse.est_vide
        or not (dossier.commune or "").strip()
    ):
        user_actions.append(
            GenerationNotice(
                code="adresse_manquante",
                message="L'adresse du dossier reste à compléter.",
                kind="action",
                href=f"{dossier_url}/dossier#adresse",
                action_label="Compléter dans Dossier",
            )
        )
    if not any(item.libelle for item in dossier.references_cadastrales):
        user_actions.append(
            GenerationNotice(
                code="cadastre_manquant",
                message="Les références cadastrales restent à renseigner.",
                kind="action",
                href=f"{dossier_url}/dossier#cadastre",
                action_label="Compléter dans Dossier",
            )
        )

    missing_usage = sum(not (lot.usage or "").strip() for lot in dossier.lots)
    if missing_usage:
        user_actions.append(
            GenerationNotice(
                code="usage_lot_manquant",
                message=f"L'usage de {missing_usage} lot(s) reste à renseigner.",
                kind="action",
                href=f"{dossier_url}/lots#donnees-lots",
                action_label="Compléter les lots",
            )
        )
    missing_designation = sum(
        not (lot.designation or "").strip() for lot in dossier.lots
    )
    if missing_designation:
        user_actions.append(
            GenerationNotice(
                code="designation_lot_manquante",
                message=(
                    f"La désignation de {missing_designation} lot(s) "
                    "reste à renseigner."
                ),
                kind="action",
                href=f"{dossier_url}/lots#donnees-lots",
                action_label="Compléter les lots",
            )
        )

    millieme_lot_ids = [item.lot_id for item in dossier.milliemes]
    if len(millieme_lot_ids) != len(set(millieme_lot_ids)):
        blockers.append("Plusieurs valeurs de millièmes ciblent le même lot.")
    if any(item not in lots for item in millieme_lot_ids):
        blockers.append("Des millièmes référencent un lot inconnu.")
    if dossier.grille_milliemes_complete:
        missing_lots = set(lots) - set(millieme_lot_ids)
        if missing_lots:
            blockers.append(
                "La grille de millièmes est déclarée complète mais certains "
                "lots n'ont aucune valeur."
            )
        total_milliemes = sum(item.valeur for item in dossier.milliemes)
        if total_milliemes != 1000:
            blockers.append(
                "La grille de millièmes est déclarée complète mais son total "
                f"vaut {total_milliemes} au lieu de 1000."
            )
    if not dossier.milliemes:
        user_actions.append(
            GenerationNotice(
                code="milliemes_manquants",
                message=(
                    "Les millièmes ne sont pas renseignés et ne sont pas calculés."
                ),
                kind="action",
                href=f"{dossier_url}/lots#milliemes",
                action_label="Compléter les millièmes",
            )
        )
    elif (
        not dossier.grille_milliemes_complete
        or any(
            item.statut_validation != StatutValidationMillieme.VALIDE
            for item in dossier.milliemes
        )
    ):
        user_actions.append(
            GenerationNotice(
                code="milliemes_a_confirmer",
                message="Des millièmes présents restent à confirmer.",
                kind="action",
                href=f"{dossier_url}/lots#milliemes",
                action_label="Compléter les millièmes",
            )
        )

    if any(
        item.statut_validation != StatutValidationJuridique.CONFIRME
        for item in dossier.servitudes
    ):
        product_limitations.append(
            GenerationNotice(
                code="servitudes_a_confirmer",
                message="Une ou plusieurs servitudes restent à confirmer.",
                kind="product",
            )
        )
    product_limitations.append(
        GenerationNotice(
            code="clauses_juridiques",
            message=(
                "Les clauses juridiques ne sont pas encore gérées par GeoFlow."
            ),
            kind="product",
        )
    )
    if corrected_count:
        information.append(
            GenerationNotice(
                code="surfaces_corrigees",
                message=(
                    f"{corrected_count} correction(s) manuelle(s) de surface "
                    "sont documentées."
                ),
                kind="information",
            )
        )

    return GenerationReadiness(
        blockers=tuple(dict.fromkeys(blockers)),
        user_actions=tuple(user_actions),
        product_limitations=tuple(product_limitations),
        information=tuple(information),
        retained_surface_m2=total,
        corrected_surface_count=corrected_count,
        retained_zone_count=len(retained_zones),
    )
