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
    Evidence,
    EvidencePolarity,
    GlobalCheckStatus,
    LotProposal,
    Planche,
    ReconciliationResult,
    StatutValidationDonnees,
    VerificationStatus,
)
from app.domain.models import StatutRevue
from app.dxf.technical import meters_per_unit
from app.labels import verification_label
from app.reconciliation.engine import DEFAULT_RULES
from app.workflow import (
    apply_profile_choice,
    attach_import,
    clean_reference,
    create_dossier,
    safe_filename,
)


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
        """Persist an inspected plan; its business analysis may come later,
        once the drawing profile is chosen (see :func:`is_pending`)."""

        if dossier.plan_importe is None or dossier.controle_technique is None:
            raise ValueError("Un plan inspecté est requis avant sa persistance.")
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

    def update(self, snapshot: QuickAnalysisSnapshot) -> None:
        self.get(snapshot.id)
        self._save(snapshot)

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
    """Attach the inspected plan with provisional planche decisions.

    The business analysis waits for the drawing profile choice
    (:func:`apply_profile_choice`): the plan is read only once.
    """

    clean_filename = safe_filename(filename)
    reference = Path(clean_filename).stem.strip()[:120] or "Analyse rapide"
    dossier = create_dossier(reference, "copropriete")
    for planche in planches:
        if planche.methode_detection == "texte_version_abandonnee_a_valider":
            planche.statut = StatutRevue.ABANDONNEE
        elif planche.methode_detection == "layout_dxf":
            planche.statut = StatutRevue.RETENUE
    attach_import(dossier, clean_filename, file_type, control, planches, reconcile=False)
    dossier.statut = "analyse_rapide"
    return dossier


def is_pending(snapshot: QuickAnalysisSnapshot) -> bool:
    """True while the plan is inspected but the profile not yet chosen."""

    return snapshot.dossier.reconciliation is None


def promote_quick_analysis(
    snapshot: QuickAnalysisSnapshot, reference: str | None = None
) -> Dossier:
    """Create a canonical dossier from the already computed snapshot.

    The reference derived from the file name is only a default: the user may
    replace it here, and later in the Dossier step.
    """

    if is_pending(snapshot):
        raise ValueError("Choisissez le profil de dessin avant de créer le dossier.")
    dossier = snapshot.dossier.model_copy(deep=True)
    dossier.id = uuid4().hex
    if reference is not None:
        dossier.reference = clean_reference(reference)
    dossier.statut = "controle_technique"
    dossier.statut_validation_donnees = StatutValidationDonnees.A_VALIDER
    dossier.date_validation_donnees = None
    dossier.sha256_validation_donnees = None
    dossier.generations = []
    return dossier


def _status_count(dossier: Dossier) -> Counter[VerificationStatus]:
    proposals = dossier.reconciliation.lot_proposals if dossier.reconciliation else []
    return Counter(item.statut for item in proposals)


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _lot_list(numbers: list[str], limit: int = 8) -> str:
    shown = ", ".join(numbers[:limit])
    return shown + (f" et {len(numbers) - limit} autre(s)" if len(numbers) > limit else "")


def _format_m2(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " m²"


def _format_tolerance(reconciliation: ReconciliationResult) -> str:
    relative = f"{reconciliation.tolerance_relative * 100:g}".replace(".", ",")
    return f"{_format_m2(reconciliation.tolerance_absolue_m2)} ou {relative} %, la plus grande des deux"


def _format_duration(seconds: float) -> str:
    if seconds < 0.1:
        return "moins de 0,1 s"
    return f"{seconds:.1f}".replace(".", ",") + " s"


EVIDENCE_TEXT = {
    "texte_numero_lot": "Repère de lot « {valeur} » trouvé sur le calque des numéros",
    "titre_plan_copropriete": "Titre de plan de copropriété à proximité",
    "libelle_lot_explicite": "Libellé explicite « {valeur} » à proximité",
    "surface_annotee": "Surface annotée sur le plan : {m2}",
    "somme_contours_principaux": "Contours de surface principale rattachés : {m2} au total",
    "ecart_surface_hors_tolerance": "Écart de {gap} entre les contours et l’annotation, hors tolérance",
    "profil_de_calques": "Calque d’annexe reconnu : {valeur}",
    "numero_lot_proche_annexe": "Repère du lot « {valeur} » à proximité, seul candidat sur la planche",
    "numeros_lot_concurrents": "Plusieurs lots à proximité ({valeur}) : aucun n’est proposé",
    "contact_contour_lot": "L’annexe touche un contour du lot « {valeur} », les autres lots proches en sont séparés",
    # Decisions of a drawing profile.
    "role_calque_profil": "Calque « {valeur} » classé par le profil",
    "numero_lot_profil": "Numéro de lot « {valeur} » lu selon la règle du profil",
    "source_secondaire_contradictoire": "Source secondaire contradictoire : lot {valeur} (la source prioritaire du profil est retenue)",
    "numeros_multiples_dans_zone": "Plusieurs numéros dans la zone ({valeur}) : source suivante du profil utilisée",
    "surface_annotee_profil": "Surface annotée lue selon la règle du profil : {m2}",
    "niveau_profil": "Niveau « {valeur} » lu selon le profil",
    "niveau_non_determine": "Aucun niveau lu selon le profil : à préciser",
    "plusieurs_espaces": "Lot dessiné dans plusieurs espaces ({valeur}) : à vérifier",
    "priorite_surface_profil": "En cas d’écart, le profil fait foi ({valeur})",
    "numero_lot_dans_annexe": "Numéro du lot « {valeur} » inscrit dans l’annexe (règle du profil)",
    "lot_complete_par_legacy": "Lot complété par l’analyse automatique : aucune règle du profil ne l’identifie",
}


def evidence_reason(evidence: Evidence) -> dict[str, object]:
    template = EVIDENCE_TEXT.get(evidence.source)
    value = evidence.valeur
    if template is None:
        text = evidence.description
    else:
        numeric = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        text = template.format(
            valeur=value,
            m2=_format_m2(float(numeric)) if numeric is not None else "",
            gap=(f"{numeric:+.2f}".replace(".", ",") + " m²") if numeric is not None else "",
        )
    return {
        "positive": evidence.polarite == EvidencePolarity.POSITIVE,
        "text": text,
        "origin": evidence.origine,
    }


def _missing_reasons(
    proposal: LotProposal, planche_status: dict[str, StatutRevue], zones: list
) -> list[dict[str, object]]:
    """Facts explaining why a proposal is not auto-verified.

    They mirror the conditions of ``reconcile_dossier``: an annotated surface
    and contours are required, then a retained planche, a specific plan title
    and at least ``automatic_minimum_contours`` contours.
    """

    reasons: list[dict[str, object]] = []
    if proposal.statut in {VerificationStatus.AUTO_VERIFIE, VerificationStatus.CONFIRME_MANUEL}:
        return reasons
    if proposal.surface_annotee_m2 is None:
        reasons.append({"positive": False, "text": "Aucune surface annotée trouvée près du repère"})
    if not proposal.candidate_zone_ids:
        reasons.append({"positive": False, "text": "Aucun contour de surface principale à proximité"})
    if proposal.statut_automatique != VerificationStatus.A_CONFIRMER or proposal.origine == "profil":
        # A profile proposal's own evidence already states what is doubtful.
        return reasons
    if proposal.candidate_zone_ids_ecartes:
        # The set-aside representation evidence already explains the status.
        return reasons
    if any(
        planche_status.get(zone.planche_region) != StatutRevue.RETENUE for zone in zones
    ):
        reasons.append({"positive": False, "text": "Contour situé sur une planche non retenue"})
    elif len(proposal.candidate_zone_ids) < DEFAULT_RULES.automatic_minimum_contours:
        reasons.append(
            {
                "positive": False,
                "text": "Un seul contour rattaché : la vérification automatique en exige au moins "
                f"{DEFAULT_RULES.automatic_minimum_contours}",
            }
        )
    else:
        reasons.append(
            {
                "positive": False,
                "text": "Titre de plan trop générique pour une vérification automatique",
            }
        )
    return reasons


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
        for candidate_id in [*proposal.candidate_zone_ids, *proposal.candidate_zone_ids_ecartes]
    }
    set_aside_lots = [proposal.numero_propose for proposal in proposals if proposal.candidate_zone_ids_ecartes]
    unmatched_items = [
        item
        for item in business
        if item.candidate_zone_id not in business_ids_in_proposals
        and item.statut != VerificationStatus.CONFIRME_MANUEL
        # Common parts named by the profile are qualified, not work left.
        and not (item.categorie_proposee == CategorieZone.COMMUNE and item.origine == "profil")
    ]
    unmatched_business = len(unmatched_items)
    # Only principal contours are grouped into lot proposals: annexes are
    # attached afterwards, so they are tracked apart from principal surfaces.
    unmatched_principal = sum(
        item.categorie_proposee == CategorieZone.PRINCIPALE for item in unmatched_items
    )
    unmatched_annex = sum(
        item.categorie_proposee == CategorieZone.SECONDAIRE_ANNEXE for item in unmatched_items
    )
    unmatched_other = unmatched_business - unmatched_principal - unmatched_annex
    annex_suggestions = sum(
        item.categorie_proposee == CategorieZone.SECONDAIRE_ANNEXE
        and item.statut == VerificationStatus.A_CONFIRMER
        and item.numero_lot_propose is not None
        for item in unmatched_items
    )
    auto_count = status_counts[VerificationStatus.AUTO_VERIFIE]
    to_confirm_count = status_counts[VerificationStatus.A_CONFIRMER]
    to_review_count = status_counts[VerificationStatus.A_REVOIR]
    unresolved_count = status_counts[VerificationStatus.NON_RESOLU]
    review_count = to_confirm_count + unresolved_count + to_review_count
    contradiction_count = status_counts[VerificationStatus.CONTRADICTOIRE]
    # Remaining work counts every unattached zone, annexes included.
    manual_burden = review_count + contradiction_count + unmatched_business
    # Understanding only measures the principal structure: an annex GeoFlow
    # identifies but does not attach yet is work left, not a misreading.
    structural_zones = sum(
        item.categorie_proposee != CategorieZone.SECONDAIRE_ANNEXE for item in business
    )
    unresolved_ratio = (unmatched_principal + unmatched_other) / max(1, structural_zones)
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

    # Narrative: first sentence answers "does GeoFlow understand this plan?",
    # the second states the facts behind it. Both are rule-based.
    if not control.zones_candidates:
        conclusion = "Aucun contour 2D exploitable n’a été détecté dans ce plan."
    elif not business:
        conclusion = (
            "Le plan est lisible, mais sa structure métier n’est pas reconnue : "
            "aucun calque ne correspond à la convention attendue."
        )
    elif not proposals:
        conclusion = (
            "Les surfaces sont détectées, mais GeoFlow ne peut associer de manière "
            "fiable aucune zone à un lot."
        )
    elif understanding["key"] == "bonne":
        conclusion = "GeoFlow reconnaît correctement la structure générale du plan."
    elif understanding["key"] == "partielle":
        conclusion = (
            "GeoFlow reconnaît la structure générale du plan, mais plusieurs "
            "éléments doivent être vérifiés."
        )
    else:
        conclusion = (
            "Le plan est lisible, mais sa structure métier est insuffisamment "
            "reconnue pour automatiser le dossier."
        )

    facts: list[str] = []
    if not unit_known:
        facts.append("L’unité du plan doit être confirmée avant tout calcul de surface.")
    if proposals:
        parts = [f"{auto_count} lot(s) sur {len(proposals)} sont auto-vérifiés"]
        pending = []
        if to_confirm_count + to_review_count:
            pending.append(f"{to_confirm_count + to_review_count} à confirmer")
        if unresolved_count:
            pending.append(f"{unresolved_count} non résolu(s)")
        if contradiction_count:
            pending.append(f"{contradiction_count} avec un écart de surface")
        if pending:
            parts.append(", ".join(pending))
        facts.append(" ; ".join(parts) + ".")
    elif business:
        facts.append(
            f"{_plural(len(business), 'zone métier', 'zones métier')} détectée(s), "
            "sans repère de lot exploitable."
        )

    workload_parts: list[str] = []
    if contradiction_count:
        workload_parts.append(_plural(contradiction_count, "écart de surface", "écarts de surface") + " à résoudre")
    if to_confirm_count + to_review_count:
        workload_parts.append(f"{to_confirm_count + to_review_count} lot(s) à confirmer")
    if unresolved_count:
        workload_parts.append(f"{unresolved_count} lot(s) non résolu(s) à compléter")
    if unmatched_principal:
        workload_parts.append(f"{unmatched_principal} surface(s) principale(s) sans lot")
    if unmatched_annex:
        workload_parts.append(f"{unmatched_annex} annexe(s) à rattacher")
    if unmatched_other:
        workload_parts.append(f"{unmatched_other} autre(s) zone(s) à qualifier")
    if understanding["key"] == "faible" and not proposals:
        workload_reason = (
            "GeoFlow n’a pas pu proposer de lots : la plupart des données devront être saisies."
        )
    elif workload_parts:
        workload_reason = (
            workload_parts[0][:1].upper() + workload_parts[0][1:]
            + "".join(
                (" et " if index == len(workload_parts) - 1 else ", ") + part
                for index, part in enumerate(workload_parts[1:], start=1)
            )
            + "."
        )
    else:
        workload_reason = "Aucune association à reprendre avant la création du dossier."

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

    exclusion_sources = Counter(
        item.source
        for item in reconciliation.preuves
        if item.id.startswith("preuve-exclusion-")
    )
    excluded_by_planche = exclusion_sources["planche_explicitement_exclue"]

    number_by_status: dict[VerificationStatus, list[str]] = {}
    for proposal in proposals:
        number_by_status.setdefault(proposal.statut, []).append(proposal.numero_propose)
    reused_check = next(
        (
            item
            for item in reconciliation.controles_globaux
            if item.code == "zones_non_reutilisees"
            and item.statut == GlobalCheckStatus.BLOQUANT
        ),
        None,
    )

    # Points to check, most severe first. Each one says what is wrong and
    # what the user will have to verify in the dossier.
    suspicious_layers = [item for item in control.calques if item.exclusion_suggeree]
    checks: list[dict[str, object]] = []
    if not unit_known:
        checks.append(
            {
                "tone": "critical",
                "title": "Unité du plan inconnue",
                "detail": "L’unité est absente, inconnue ou non prise en charge : les surfaces ne peuvent pas être converties en m².",
                "action": "Confirmez l’unité dans l’étape Contrôle du dossier.",
            }
        )
    if contradiction_count:
        checks.append(
            {
                "tone": "critical",
                "title": _plural(contradiction_count, "écart de surface", "écarts de surface"),
                "detail": (
                    "La surface annotée diffère de la somme des contours au-delà de la "
                    f"tolérance ({_format_tolerance(reconciliation)})."
                ),
                "action": "Comparez annotation et contours pour le(s) lot(s) "
                + _lot_list(number_by_status[VerificationStatus.CONTRADICTOIRE])
                + ".",
            }
        )
    if reused_check is not None:
        reused = len(reused_check.candidate_zone_ids)
        checks.append(
            {
                "tone": "critical",
                "title": _plural(reused, "contour partagé", "contours partagés") + " entre plusieurs lots",
                "detail": "Un même contour est rattaché à plusieurs numéros de lot.",
                "action": "Choisissez le lot auquel chaque contour appartient.",
            }
        )
    if unresolved_count:
        checks.append(
            {
                "tone": "warning",
                "title": _plural(unresolved_count, "lot non résolu", "lots non résolus"),
                "detail": "Un repère de lot est présent, mais sans surface annotée ou sans contour principal à proximité.",
                "action": "Complétez le(s) lot(s) "
                + _lot_list(number_by_status[VerificationStatus.NON_RESOLU])
                + ".",
            }
        )
    if to_confirm_count + to_review_count:
        numbers = number_by_status.get(VerificationStatus.A_CONFIRMER, []) + number_by_status.get(
            VerificationStatus.A_REVOIR, []
        )
        checks.append(
            {
                "tone": "warning",
                "title": _plural(len(numbers), "lot à confirmer", "lots à confirmer"),
                "detail": "Surface cohérente, mais les indices ne suffisent pas à une vérification automatique.",
                "action": f"Confirmez le(s) lot(s) {_lot_list(numbers)} ; le détail figure dans « Pourquoi ? ».",
            }
        )
    if unmatched_principal:
        checks.append(
            {
                "tone": "warning",
                "title": _plural(
                    unmatched_principal,
                    "surface principale sans lot",
                    "surfaces principales sans lot",
                ),
                "detail": "Contour sur un calque de surface principale, sans repère de lot fiable à proximité.",
                "action": "Rattachez-les à un lot ou excluez-les dans le dossier.",
            }
        )
    if business and not proposals and not unmatched_principal:
        checks.append(
            {
                "tone": "warning",
                "title": "Aucun lot proposé",
                "detail": "Des zones métier sont détectées, mais aucun repère de lot exploitable n’a été trouvé.",
                "action": "Les lots devront être constitués manuellement.",
            }
        )
    usage = dossier.profile_usage
    profiled = usage is not None and usage.mode == "profil"
    if not business and control.zones_candidates:
        checks.append(
            {
                "tone": "warning",
                "title": "Convention de calques non reconnue",
                "detail": (
                    f"Aucun calque ne correspond aux rôles du profil « {usage.profile_name} »."
                    if profiled
                    else f"Aucun calque ne correspond au profil {reconciliation.profil_regles} "
                    "(surfaces principales 80, annexes 81)."
                ),
                "action": "Les zones devront être qualifiées manuellement.",
            }
        )
    if profiled and plan.unite_retenue != plan.unite_detectee and meters_per_unit(plan.unite_detectee) is not None:
        checks.insert(
            0,
            {
                "tone": "warning",
                "title": "Unité imposée par le profil",
                "detail": (
                    f"Le plan déclare « {plan.unite_detectee} », le profil impose « {plan.unite_retenue} » : "
                    "les surfaces sont calculées avec l’unité du profil."
                ),
                "action": "Vérifiez l’unité dans l’étape Contrôle du dossier.",
            }
        )
    if set_aside_lots:
        checks.append(
            {
                "tone": "info",
                "title": _plural(
                    len(set_aside_lots),
                    "lot représenté plusieurs fois",
                    "lots représentés plusieurs fois",
                ),
                "detail": (
                    "Copies exactes ou représentations sans surface annotée d’un lot déjà "
                    "annoté : leurs contours ne sont pas additionnés."
                ),
                "action": f"Vérifiez le(s) lot(s) {_lot_list(set_aside_lots)} ; le détail figure dans « Pourquoi ? ».",
            }
        )
    if abandoned_planches:
        checks.append(
            {
                "tone": "info",
                "title": _plural(
                    len(abandoned_planches),
                    "planche de version abandonnée écartée",
                    "planches de version abandonnée écartées",
                ),
                "detail": (
                    "Mention explicite de version abandonnée sur : "
                    + ", ".join(item.titre for item in abandoned_planches)
                    + (
                        f". {_plural(excluded_by_planche, 'contour exclu', 'contours exclus')} provisoirement du diagnostic."
                        if excluded_by_planche
                        else "."
                    )
                ),
                "action": "Décision révisable dans l’étape Contrôle du dossier.",
            }
        )
    if unmatched_annex:
        checks.append(
            {
                "tone": "info",
                "title": _plural(unmatched_annex, "annexe à rattacher", "annexes à rattacher"),
                "detail": (
                    "Les surfaces annexes sont détectées ; "
                    + (
                        f"GeoFlow propose un lot pour {annex_suggestions} d’entre elles, "
                        + (
                            "d’après le numéro inscrit (règle du profil) ou le repère de lot proche."
                            if profiled
                            else "d’après le seul repère de lot proche."
                        )
                        if annex_suggestions
                        else "aucun lot n’est proposé automatiquement faute de repère unique à proximité."
                    )
                ),
                "action": "Rattachez-les dans l’étape Lots & surfaces du dossier ; rien n’est rattaché sans votre choix.",
            }
        )
    if suspicious_layers:
        checks.append(
            {
                "tone": "info",
                "title": _plural(len(suspicious_layers), "calque signalé", "calques signalés"),
                "detail": "Calques masqués, gelés, non tracés ou nommés Poubelle/00 : signalés pour vérification.",
                "action": "Vérifiez-les dans l’inventaire des calques.",
            }
        )
    anomalies = [
        {
            "level": item["tone"] if item["tone"] == "critical" else "warning",
            "message": f"{item['title']} — {item['detail']}",
        }
        for item in checks
    ]
    if not anomalies and proposals:
        anomalies.append(
            {
                "level": "ok",
                "message": "Aucune contradiction détectée dans les rapprochements proposés.",
            }
        )

    candidate_by_id = {item.id: item for item in control.zones_candidates}
    planche_by_id = {item.id: item for item in dossier.planches}
    planche_status = {item.id: item.statut for item in dossier.planches}
    evidence_by_id = {item.id: item for item in reconciliation.preuves}
    category_by_candidate = {
        item.candidate_zone_id: item.categorie_proposee.value for item in business
    }
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
        evidence = [
            evidence_by_id[item]
            for item in proposal.evidence_ids
            if item in evidence_by_id
        ]
        surfaces_by_planche: dict[str, float] = {}
        for candidate in zones:
            planche = planche_by_id.get(candidate.planche_region)
            title = planche.titre if planche else "Planche à confirmer"
            surfaces_by_planche[title] = surfaces_by_planche.get(title, 0.0) + (
                candidate.surface_geometrique_m2 or 0.0
            )
        lot_rows.append(
            {
                "proposal": proposal,
                "status_label": verification_label(proposal.statut),
                "zones": [
                    {
                        "surface": candidate.surface_geometrique_m2,
                        "category": category_by_candidate.get(candidate.id, "autre"),
                        "planche": planche_by_id.get(candidate.planche_region),
                    }
                    for candidate in zones
                ],
                "planche_surfaces": [
                    {"title": title, "surface": surface}
                    for title, surface in surfaces_by_planche.items()
                ],
                "evidence": evidence,
                "reasons": [evidence_reason(item) for item in evidence]
                + _missing_reasons(proposal, planche_status, zones),
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

    # Creating a dossier is never blocked: the workflow handles manual entry.
    if understanding["key"] == "bonne":
        # The creation panel directly follows the summary: no extra button.
        actions = {"create_first": True}
    elif understanding["key"] == "partielle":
        actions = {
            "label": "Examiner les points à vérifier",
            "secondary_label": "Créer le dossier",
            "create_first": False,
        }
    else:
        actions = {
            "label": "Voir pourquoi",
            "secondary_label": "Créer le dossier quand même",
            "create_first": False,
        }

    key_counts = [
        {"key": "proposals", "value": len(proposals), "label": "lots proposés", "tone": "neutral"},
        {"key": "auto", "value": auto_count, "label": "auto-vérifiés", "tone": "ok"},
        {
            "key": "confirm",
            "value": to_confirm_count + to_review_count,
            "label": "à confirmer",
            "tone": "warning",
        },
        {"key": "contradictions", "value": contradiction_count, "label": "écarts de surface", "tone": "critical"},
    ]
    if unresolved_count:
        key_counts.insert(3, {"key": "unresolved", "value": unresolved_count, "label": "non résolus", "tone": "warning"})

    return {
        "understanding": understanding,
        "workload": workload,
        "workload_reason": workload_reason,
        "conclusion": conclusion,
        "facts": facts,
        "duration": _format_duration(snapshot.duration_seconds),
        "key_counts": key_counts,
        "counts": {
            "entities": control.nombre_entites,
            "contours": reconciliation.contours_analyses,
            "business": len(business),
            "proposals": len(proposals),
            "auto": auto_count,
            "review": review_count,
            "to_confirm": to_confirm_count + to_review_count,
            "contradictions": contradiction_count,
            "unresolved": unresolved_count,
            "excluded": len(reconciliation.candidate_ids_exclus_techniquement),
            "unmatched_principal": unmatched_principal,
            "unmatched_annex": unmatched_annex,
            "annex_suggestions": annex_suggestions,
        },
        "structure": {
            "buildings": len(dossier.batiments),
            "levels": len(dossier.niveaux),
            "planches": len(usable_planches),
            "abandoned_planches": len(abandoned_planches),
        },
        "versions": {
            "abandoned": [item.titre for item in abandoned_planches],
            "excluded_contours": excluded_by_planche,
        },
        "categories": category_summary,
        "checks": checks,
        "anomalies": anomalies,
        "surface_anomalies": contradiction_count,
        "lots": lot_rows,
        "actions": actions,
        "profile": reconciliation.profil_regles if profile_recognized else None,
        "compatibility": compatibility,
        "technical": {
            "version": control.version_dxf,
            "unit": control.unite_detectee,
            "bbox": bbox,
            "layers": len(control.calques),
            "flagged_layers": [item.nom for item in suspicious_layers],
            "texts": len(control.textes),
            "types": control.types_entites,
            "closed": control.polylignes_fermees,
            "bulges": bulge_count,
            "excluded": len(reconciliation.candidate_ids_exclus_techniquement),
            "exclusions": [
                (EXCLUSION_LABELS.get(source, source), count)
                for source, count in sorted(exclusion_sources.items())
            ],
            "rules_version": reconciliation.version_regles,
            "rules_profile": reconciliation.profil_regles,
            "tolerance": _format_tolerance(reconciliation),
            "warnings": control.avertissements,
        },
    }


EXCLUSION_LABELS = {
    "planche_explicitement_exclue": "Planche écartée (version abandonnée ou exclue)",
    "calque_explicitement_exclu": "Calque exclu par l’utilisateur",
    "calque_exclu_par_profil": "Calque exclu par le profil (82, Poubelle, 00)",
    "hors_source_profil": "Hors de la source géométrique du profil de dessin",
    "presentation_exclue_profil": "Présentation exclue par le profil de dessin",
    "contour_auto_intersecte_profil": "Contour auto-intersecté (profil de dessin)",
    "calque_exclusion_profil": "Calque « Exclusion » du profil de dessin",
    "calque_archive_profil": "Calque « Archive » du profil de dessin",
    "calque_hors_copropriete_profil": "Calque « Hors copropriété » du profil de dessin",
}
