"""Persistent, explainable pre-diagnostic built from the canonical workflow."""

from __future__ import annotations

import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain import (
    CategorieZone,
    ControleTechnique,
    Dossier,
    Planche,
    StatutValidationDonnees,
    VerificationStatus,
)
from app.domain.models import StatutRevue
from app.workflow import attach_import, create_dossier, safe_filename


ANALYSIS_TTL = timedelta(hours=24)
_SAFE_ID = re.compile(r"^[a-f0-9]{32}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QuickAnalysisNotFoundError(KeyError):
    pass


class QuickAnalysisSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    duration_seconds: float = Field(ge=0)
    dossier: Dossier


class QuickAnalysisRepository:
    """Atomic JSON store for refresh-safe, expiring analyses."""

    def __init__(
        self,
        root: str | Path,
        ttl: timedelta = ANALYSIS_TTL,
    ) -> None:
        self.root = Path(root)
        self.ttl = ttl

    def _path(self, analysis_id: str) -> Path:
        if not _SAFE_ID.fullmatch(analysis_id):
            raise QuickAnalysisNotFoundError(analysis_id)
        return self.root / f"{analysis_id}.json"

    def create(self, dossier: Dossier, duration_seconds: float) -> QuickAnalysisSnapshot:
        if (
            dossier.plan_importe is None
            or dossier.controle_technique is None
            or dossier.reconciliation is None
        ):
            raise ValueError("Une analyse complète est requise avant sa persistance.")
        self.cleanup_expired()
        created_at = utc_now()
        snapshot = QuickAnalysisSnapshot(
            id=uuid4().hex,
            created_at=created_at,
            expires_at=created_at + self.ttl,
            duration_seconds=max(0.0, duration_seconds),
            dossier=dossier,
        )
        self._save(snapshot)
        return snapshot

    def get(self, analysis_id: str) -> QuickAnalysisSnapshot:
        path = self._path(analysis_id)
        try:
            snapshot = QuickAnalysisSnapshot.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise QuickAnalysisNotFoundError(analysis_id) from exc
        except ValueError as exc:
            raise QuickAnalysisNotFoundError(analysis_id) from exc
        if snapshot.expires_at <= utc_now():
            path.unlink(missing_ok=True)
            raise QuickAnalysisNotFoundError(analysis_id)
        return snapshot

    def delete(self, analysis_id: str) -> None:
        path = self._path(analysis_id)
        if not path.is_file():
            raise QuickAnalysisNotFoundError(analysis_id)
        path.unlink()

    def cleanup_expired(self) -> int:
        if not self.root.exists():
            return 0
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                snapshot = QuickAnalysisSnapshot.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if snapshot.expires_at <= utc_now():
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _save(self, snapshot: QuickAnalysisSnapshot) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(snapshot.id)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.root,
                prefix=f".{snapshot.id}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(snapshot.model_dump_json(indent=2))
                temporary.write("\n")
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def prepare_quick_analysis_dossier(
    filename: str,
    file_type: str,
    control: ControleTechnique,
    planches: list[Planche],
) -> Dossier:
    """Run the canonical import/reconciliation with provisional planche decisions."""

    clean_filename = safe_filename(filename)
    reference = Path(clean_filename).stem.strip()[:120] or "Analyse rapide"
    dossier = create_dossier(reference, "copropriete")
    for planche in planches:
        if planche.methode_detection == "texte_version_abandonnee_a_valider":
            planche.statut = StatutRevue.ABANDONNEE
        elif planche.methode_detection == "layout_dxf":
            planche.statut = StatutRevue.RETENUE
    attach_import(dossier, clean_filename, file_type, control, planches)
    dossier.statut = "analyse_rapide"
    return dossier


def promote_quick_analysis(snapshot: QuickAnalysisSnapshot) -> Dossier:
    """Create a canonical dossier from the already computed snapshot."""

    dossier = snapshot.dossier.model_copy(deep=True)
    dossier.id = uuid4().hex
    dossier.statut = "controle_technique"
    dossier.statut_validation_donnees = StatutValidationDonnees.A_VALIDER
    dossier.date_validation_donnees = None
    dossier.sha256_validation_donnees = None
    dossier.generations = []
    return dossier


def _status_count(dossier: Dossier) -> Counter[VerificationStatus]:
    proposals = dossier.reconciliation.lot_proposals if dossier.reconciliation else []
    return Counter(item.statut for item in proposals)


def build_quick_diagnostic(snapshot: QuickAnalysisSnapshot) -> dict[str, object]:
    """Derive a concise diagnostic; every value comes from canonical data."""

    dossier = snapshot.dossier
    plan = dossier.plan_importe
    control = dossier.controle_technique
    reconciliation = dossier.reconciliation
    assert plan is not None and control is not None and reconciliation is not None

    status_counts = _status_count(dossier)
    proposals = reconciliation.lot_proposals
    business = reconciliation.business_zone_candidates
    business_ids_in_proposals = {
        candidate_id
        for proposal in proposals
        for candidate_id in proposal.candidate_zone_ids
    }
    unmatched_business = sum(
        item.candidate_zone_id not in business_ids_in_proposals for item in business
    )
    auto_count = status_counts[VerificationStatus.AUTO_VERIFIE]
    review_count = sum(
        status_counts[status]
        for status in (
            VerificationStatus.A_CONFIRMER,
            VerificationStatus.NON_RESOLU,
            VerificationStatus.A_REVOIR,
        )
    )
    contradiction_count = status_counts[VerificationStatus.CONTRADICTOIRE]
    manual_burden = review_count + contradiction_count + unmatched_business
    unresolved_ratio = unmatched_business / max(1, len(business))
    usable_planches = [
        item
        for item in dossier.planches
        if item.statut not in {StatutRevue.EXCLUE, StatutRevue.ABANDONNEE}
    ]
    abandoned_planches = [
        item for item in dossier.planches if item.statut == StatutRevue.ABANDONNEE
    ]
    unit_known = plan.unite_confirmee

    good = (
        unit_known
        and bool(usable_planches)
        and bool(proposals)
        and auto_count >= (len(proposals) // 2) + 1
        and contradiction_count == 0
        and unresolved_ratio <= 0.20
    )
    partial = (
        unit_known
        and bool(usable_planches)
        and bool(business)
        and (bool(proposals) or unmatched_business < len(business))
    )
    if good:
        understanding = {
            "key": "bonne",
            "label": "Bonne",
            "reason": (
                "L’unité et des planches exploitables sont détectées, la majorité "
                "des lots est auto-vérifiée et aucune contradiction n’est relevée."
            ),
        }
    elif partial:
        understanding = {
            "key": "partielle",
            "label": "Partielle",
            "reason": (
                "La structure métier est détectée, mais des confirmations, des "
                "zones non rapprochées ou des contradictions subsistent."
            ),
        }
    else:
        understanding = {
            "key": "faible",
            "label": "Faible",
            "reason": (
                "L’unité, les planches ou les rapprochements métier sont trop "
                "incomplets pour obtenir une lecture fiable des lots."
            ),
        }

    if understanding["key"] == "bonne" and manual_burden == 0:
        workload = {"key": "faible", "label": "Faible"}
    elif (
        understanding["key"] == "faible"
        or contradiction_count >= 2
        or (
            len(business) >= 3
            and manual_burden / len(business) >= 0.70
        )
    ):
        workload = {"key": "importante", "label": "Importante"}
    else:
        workload = {"key": "moyenne", "label": "Moyenne"}

    if not control.zones_candidates:
        conclusion = "Aucun contour 2D exploitable n’a été détecté dans ce plan."
    elif not proposals:
        conclusion = (
            "Le plan est lisible, mais aucun lot suffisamment fiable n’a été identifié."
        )
    elif understanding["key"] == "bonne":
        conclusion = "Ce plan semble exploitable par GeoFlow."
    elif understanding["key"] == "partielle":
        conclusion = (
            "Ce plan peut être utilisé, mais plusieurs éléments devront être confirmés."
        )
    else:
        conclusion = (
            "GeoFlow comprend mal ce plan. Une préparation manuelle importante sera nécessaire."
        )

    category_counts = Counter(
        item.categorie_proposee for item in business
    )
    category_summary = {
        "principale": category_counts[CategorieZone.PRINCIPALE],
        "annexe": category_counts[CategorieZone.SECONDAIRE_ANNEXE],
        "commune": category_counts[CategorieZone.COMMUNE],
        "autre": category_counts[CategorieZone.AUTRE],
        "exclue": len(reconciliation.candidate_ids_exclus_techniquement),
    }

    suspicious_layers = [item for item in control.calques if item.exclusion_suggeree]
    anomalies: list[dict[str, str]] = []
    if contradiction_count:
        anomalies.append(
            {
                "level": "critical",
                "message": f"{contradiction_count} proposition(s) présentent une contradiction de surface ou d’association.",
            }
        )
    if status_counts[VerificationStatus.NON_RESOLU]:
        anomalies.append(
            {
                "level": "warning",
                "message": f"{status_counts[VerificationStatus.NON_RESOLU]} lot(s) proposé(s) restent non résolus.",
            }
        )
    if abandoned_planches:
        anomalies.append(
            {
                "level": "warning",
                "message": f"{len(abandoned_planches)} zone(s) ou planche(s) semblent appartenir à une version abandonnée.",
            }
        )
    if suspicious_layers:
        anomalies.append(
            {
                "level": "warning",
                "message": f"{len(suspicious_layers)} calque(s) masqués, gelés, non tracés ou suspects sont signalés.",
            }
        )
    if unmatched_business:
        anomalies.append(
            {
                "level": "warning",
                "message": f"{unmatched_business} zone(s) métier ne sont associées à aucun lot proposé.",
            }
        )
    if not unit_known:
        anomalies.append(
            {
                "level": "critical",
                "message": "L’unité du plan est absente, inconnue ou non prise en charge.",
            }
        )
    if not anomalies and proposals:
        anomalies.append(
            {
                "level": "ok",
                "message": "Aucune contradiction détectée dans les rapprochements proposés.",
            }
        )

    candidate_by_id = {item.id: item for item in control.zones_candidates}
    planche_by_id = {item.id: item for item in dossier.planches}
    evidence_by_id = {item.id: item for item in reconciliation.preuves}
    lot_rows = []
    for proposal in sorted(
        proposals,
        key=lambda item: (
            int(item.numero_propose) if item.numero_propose.isdigit() else 10**9,
            item.numero_propose,
        ),
    ):
        zones = [
            candidate_by_id[item]
            for item in proposal.candidate_zone_ids
            if item in candidate_by_id
        ]
        lot_rows.append(
            {
                "proposal": proposal,
                "zones": [
                    {
                        "surface": candidate.surface_geometrique_m2,
                        "category": next(
                            (
                                item.categorie_proposee.value
                                for item in business
                                if item.candidate_zone_id == candidate.id
                            ),
                            "autre",
                        ),
                        "planche": planche_by_id.get(candidate.planche_region),
                    }
                    for candidate in zones
                ],
                "evidence": [
                    evidence_by_id[item]
                    for item in proposal.evidence_ids
                    if item in evidence_by_id
                ],
            }
        )

    bulge_count = sum(
        abs(vertex.bulge) > 1e-12
        for candidate in control.zones_candidates
        for vertex in candidate.sommets
    )
    profile_recognized = bool(business)
    bbox = control.bbox
    compatibility = [
        ("DWG/DXF", "Compatible"),
        ("Unité", plan.unite_detectee if unit_known else "À confirmer"),
        (
            "Surfaces 2D",
            f"{control.polylignes_fermees} contour(s) calculable(s)"
            if control.polylignes_fermees
            else "Aucune",
        ),
        ("Arcs bulge", f"{bulge_count} détecté(s)" if bulge_count else "Pris en charge"),
        (
            "Lots",
            "Compris"
            if proposals and review_count == 0 and contradiction_count == 0
            else "Partiel"
            if proposals
            else "Non détectés",
        ),
        (
            "Annexes",
            f"{category_summary['annexe']} détectée(s)"
            if category_summary["annexe"]
            else "Non détectées",
        ),
        ("Millièmes", "Non analysés dans l’analyse rapide"),
    ]

    return {
        "understanding": understanding,
        "workload": workload,
        "conclusion": conclusion,
        "counts": {
            "entities": control.nombre_entites,
            "contours": reconciliation.contours_analyses,
            "business": len(business),
            "proposals": len(proposals),
            "auto": auto_count,
            "review": review_count,
            "contradictions": contradiction_count,
            "unresolved": status_counts[VerificationStatus.NON_RESOLU],
            "excluded": len(reconciliation.candidate_ids_exclus_techniquement),
        },
        "structure": {
            "buildings": len(dossier.batiments),
            "levels": len(dossier.niveaux),
            "planches": len(usable_planches),
            "abandoned_planches": len(abandoned_planches),
        },
        "categories": category_summary,
        "anomalies": anomalies,
        "surface_anomalies": contradiction_count,
        "lots": lot_rows,
        "profile": reconciliation.profil_regles if profile_recognized else None,
        "compatibility": compatibility,
        "technical": {
            "version": control.version_dxf,
            "unit": control.unite_detectee,
            "bbox": bbox,
            "layers": len(control.calques),
            "texts": len(control.textes),
            "types": control.types_entites,
            "closed": control.polylignes_fermees,
            "bulges": bulge_count,
            "excluded": len(reconciliation.candidate_ids_exclus_techniquement),
            "warnings": control.avertissements,
        },
    }
