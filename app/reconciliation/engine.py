"""Conservative, deterministic DXF-to-lot reconciliation.

Authority is ordered: an explicit user decision wins over the automatic
result; retained/excluded planches and layers constrain the analysis;
positioned DXF text is supporting evidence; geometry remains the source of
calculated surfaces. No text, layer name, or surface can validate a lot alone.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.domain import (
    BusinessZoneCandidate,
    CategorieZone,
    Dossier,
    Evidence,
    EvidencePolarity,
    EvidenceReliability,
    GlobalCheck,
    GlobalCheckStatus,
    LotProposal,
    Provenance,
    ReconciliationResult,
    VerificationStatus,
)
from app.domain.models import BBox, CandidateZone, Planche, StatutRevue, TexteDxf


logger = logging.getLogger(__name__)
RECONCILIATION_RULES_VERSION = "1.0.0"
T = TypeVar("T")


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_value.upper().split())


@dataclass(frozen=True)
class SurfaceTolerance:
    absolute_m2: float = 0.05
    relative: float = 0.001

    def accepts(self, geometric_m2: float, annotated_m2: float) -> bool:
        allowed = max(
            self.absolute_m2,
            self.relative * max(abs(geometric_m2), abs(annotated_m2)),
        )
        return abs(geometric_m2 - annotated_m2) <= allowed


@dataclass(frozen=True)
class ReconciliationRules:
    profile_name: str
    principal_layer_patterns: tuple[str, ...]
    annex_layer_patterns: tuple[str, ...]
    excluded_layer_patterns: tuple[str, ...]
    lot_label_layer_patterns: tuple[str, ...]
    surface_label_layer_patterns: tuple[str, ...]
    plan_title_markers: tuple[str, ...]
    automatic_title_markers: tuple[str, ...]
    tolerance: SurfaceTolerance = SurfaceTolerance()
    spatial_cell_size: float = 20.0
    lot_assignment_radius: float = 12.0
    surface_assignment_radius: float = 8.0
    title_assignment_radius: float = 35.0
    automatic_minimum_contours: int = 2


DEFAULT_RULES = ReconciliationRules(
    profile_name="geometre-npg-v1",
    principal_layer_patterns=(r"^80(?:-|$)",),
    annex_layer_patterns=(r"^81(?:-|$)",),
    excluded_layer_patterns=(r"^82(?:-|$)", r"^POUBELLE$", r"^00$"),
    lot_label_layer_patterns=(r"^05(?:-|$)",),
    surface_label_layer_patterns=(r"^83(?:-|$)",),
    plan_title_markers=("IDENTIFICATION DES LOTS", "PLAN DE COPROPRIETE"),
    automatic_title_markers=("IDENTIFICATION DES LOTS",),
)


def _matches(value: str, patterns: tuple[str, ...]) -> bool:
    normalized = _normalize(value)
    return any(re.search(pattern, normalized) for pattern in patterns)


def _surface_number(value: str) -> float | None:
    try:
        number = float(value.replace(",", "."))
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


_TOTAL_SURFACE = re.compile(
    r"TOT\s*>\s*1[.,]8\s*=\s*([0-9]+(?:[.,][0-9]+)?)\s*M2",
    re.IGNORECASE,
)
_ABOVE_HEIGHT_SURFACE = re.compile(
    r"S\s*>\s*1[.,]80?\s*=\s*([0-9]+(?:[.,][0-9]+)?)\s*M2",
    re.IGNORECASE,
)
_DIRECT_SURFACE = re.compile(
    r"(?:^|\s)S\s*=\s*([0-9]+(?:[.,][0-9]+)?)\s*M2",
    re.IGNORECASE,
)
_FULL_LOT_LABEL = re.compile(r"^LOT\s+(?:N(?:O)?\s*)?([0-9]{1,4})\b")
_LOT_NUMBER_ONLY = re.compile(r"^[0-9]{1,4}$")


def parse_principal_surface(value: str) -> float | None:
    """Extract one principal surface, ignoring below-height and annex values."""

    normalized = _normalize(value)
    for pattern in (_TOTAL_SURFACE, _ABOVE_HEIGHT_SURFACE, _DIRECT_SURFACE):
        match = pattern.search(normalized)
        if match:
            return _surface_number(match.group(1))
    return None


@dataclass(frozen=True)
class _Point(Generic[T]):
    planche_id: str
    x: float
    y: float
    stable_id: str
    value: T


class _SpatialIndex(Generic[T]):
    """Small deterministic grid index; queries never scan every text."""

    def __init__(self, cell_size: float) -> None:
        self.cell_size = cell_size
        self._cells: dict[tuple[str, int, int], list[_Point[T]]] = defaultdict(list)

    def _cell(self, coordinate: float) -> int:
        return math.floor(coordinate / self.cell_size)

    def add(self, point: _Point[T]) -> None:
        key = (point.planche_id, self._cell(point.x), self._cell(point.y))
        self._cells[key].append(point)

    def query(
        self, planche_id: str, x: float, y: float, radius: float
    ) -> list[tuple[float, _Point[T]]]:
        min_x, max_x = self._cell(x - radius), self._cell(x + radius)
        min_y, max_y = self._cell(y - radius), self._cell(y + radius)
        found: list[tuple[float, _Point[T]]] = []
        for cell_x in range(min_x, max_x + 1):
            for cell_y in range(min_y, max_y + 1):
                for point in self._cells.get((planche_id, cell_x, cell_y), ()):
                    distance = math.hypot(point.x - x, point.y - y)
                    if distance <= radius:
                        found.append((distance, point))
        return sorted(found, key=lambda item: (item[0], item[1].stable_id))


@dataclass(frozen=True)
class _LotAnchor:
    occurrence_id: str
    numero: str
    text: TexteDxf
    planche_id: str


@dataclass
class _Occurrence:
    anchor: _LotAnchor
    candidate_ids: list[str]
    geometric_surface_m2: float
    annotated_surface_m2: float | None
    automatic_status: VerificationStatus
    evidence_ids: list[str]


def _bbox_area(bbox: BBox) -> float:
    return max(0.0, bbox.max_x - bbox.min_x) * max(0.0, bbox.max_y - bbox.min_y)


def _point_in_bbox(x: float, y: float, bbox: BBox) -> bool:
    return bbox.min_x <= x <= bbox.max_x and bbox.min_y <= y <= bbox.max_y


def _resolved_text_planche(text: TexteDxf, planches: list[Planche]) -> str:
    specific = [
        planche
        for planche in planches
        if planche.id != "model"
        and planche.bbox_region is not None
        and _point_in_bbox(text.x, text.y, planche.bbox_region)
    ]
    if specific:
        return min(
            specific,
            key=lambda item: (
                _bbox_area(item.bbox_region) if item.bbox_region else math.inf,
                item.id,
            ),
        ).id
    return text.planche_region


def _candidate_center(candidate: CandidateZone) -> tuple[float, float] | None:
    bbox = candidate.bbox
    if bbox is None:
        return None
    return (bbox.min_x + bbox.max_x) / 2, (bbox.min_y + bbox.max_y) / 2


def _text_provenance(text: TexteDxf, dossier: Dossier) -> Provenance:
    if text.provenance is not None:
        return text.provenance
    source = dossier.plan_importe.nom_fichier_original if dossier.plan_importe else ""
    return Provenance(
        fichier_source=source,
        handle_dxf=text.handle_dxf,
        calque=text.calque,
        type_entite="TEXT",
        planche_region=text.planche_region,
        methode_detection="texte_dxf_positionne",
    )


def _planche_status(dossier: Dossier, planche_id: str) -> StatutRevue:
    planche = next((item for item in dossier.planches if item.id == planche_id), None)
    return planche.statut if planche is not None else StatutRevue.CANDIDATE


def _add_evidence(store: dict[str, Evidence], evidence: Evidence) -> str:
    store.setdefault(evidence.id, evidence)
    return evidence.id


def _technical_exclusion_reason(
    dossier: Dossier, candidate: CandidateZone, rules: ReconciliationRules
) -> tuple[str, str] | None:
    planche_status = _planche_status(dossier, candidate.planche_region)
    if planche_status in {StatutRevue.EXCLUE, StatutRevue.ABANDONNEE}:
        return "planche_explicitement_exclue", candidate.planche_region

    control = dossier.controle_technique
    layer = next(
        (
            item
            for item in (control.calques if control is not None else [])
            if item.nom == candidate.calque
        ),
        None,
    )
    if layer is not None and layer.statut in {
        StatutRevue.EXCLUE,
        StatutRevue.ABANDONNEE,
    }:
        return "calque_explicitement_exclu", candidate.calque
    if (
        layer is None or layer.statut != StatutRevue.RETENUE
    ) and _matches(candidate.calque, rules.excluded_layer_patterns):
        return "calque_exclu_par_profil", candidate.calque
    return None


def _build_global_checks(
    dossier: Dossier, proposals: list[LotProposal]
) -> list[GlobalCheck]:
    checks: list[GlobalCheck] = []
    canonical_numbers = [lot.numero.strip() for lot in dossier.lots]
    duplicate_numbers = sorted(
        number
        for number, count in Counter(canonical_numbers).items()
        if number and count > 1
    )
    checks.append(
        GlobalCheck(
            code="numeros_lots_uniques",
            statut=(
                GlobalCheckStatus.BLOQUANT
                if duplicate_numbers
                else GlobalCheckStatus.OK
            ),
            message=(
                "Numéros de lots en double : " + ", ".join(duplicate_numbers)
                if duplicate_numbers
                else "Les numéros des lots constitués sont uniques."
            ),
        )
    )

    candidate_usage = Counter(
        candidate_id
        for proposal in proposals
        for candidate_id in proposal.candidate_zone_ids
    )
    reused = sorted(
        candidate_id
        for candidate_id, count in candidate_usage.items()
        if count > 1
    )
    checks.append(
        GlobalCheck(
            code="zones_non_reutilisees",
            statut=GlobalCheckStatus.BLOQUANT if reused else GlobalCheckStatus.OK,
            message=(
                f"{len(reused)} contour(s) sont utilisés par plusieurs propositions."
                if reused
                else "Aucun contour n'est utilisé par plusieurs propositions."
            ),
            candidate_zone_ids=reused,
        )
    )

    incomplete = [
        proposal.id
        for proposal in proposals
        if not proposal.numero_propose.strip()
        or not proposal.candidate_zone_ids
        or proposal.surface_proposee_m2 is None
        or proposal.surface_proposee_m2 <= 0
    ]
    checks.append(
        GlobalCheck(
            code="propositions_completes",
            statut=(
                GlobalCheckStatus.BLOQUANT if incomplete else GlobalCheckStatus.OK
            ),
            message=(
                f"{len(incomplete)} proposition(s) n'ont pas de numéro, de zone ou de surface."
                if incomplete
                else "Chaque proposition possède un numéro, une zone et une surface."
            ),
            proposal_ids=incomplete,
        )
    )

    blocking_statuses = {
        VerificationStatus.A_CONFIRMER,
        VerificationStatus.NON_RESOLU,
        VerificationStatus.CONTRADICTOIRE,
        VerificationStatus.A_REVOIR,
    }
    blocking = [
        proposal.id for proposal in proposals if proposal.statut in blocking_statuses
    ]
    contradictory = [
        proposal.id
        for proposal in proposals
        if proposal.statut == VerificationStatus.CONTRADICTOIRE
    ]
    checks.append(
        GlobalCheck(
            code="reconciliation_resolue",
            statut=GlobalCheckStatus.BLOQUANT if blocking else GlobalCheckStatus.OK,
            message=(
                f"{len(blocking)} proposition(s) restent à confirmer ou à résoudre, "
                f"dont {len(contradictory)} contradictoire(s)."
                if blocking
                else "Toutes les propositions sont vérifiées ou confirmées."
            ),
            proposal_ids=blocking,
        )
    )

    zones = {zone.id: zone for zone in dossier.zones}
    malformed_lots = [
        lot.id
        for lot in dossier.lots
        if not lot.zone_ids
        or sum(
            zones[zone_id].surface_retenue_m2
            for zone_id in lot.zone_ids
            if zone_id in zones
        )
        <= 0
    ]
    checks.append(
        GlobalCheck(
            code="lots_constitues",
            statut=(
                GlobalCheckStatus.BLOQUANT
                if malformed_lots
                else GlobalCheckStatus.OK
            ),
            message=(
                f"{len(malformed_lots)} lot(s) constitués sont sans zone ou sans surface."
                if malformed_lots
                else "Les lots constitués possèdent des zones et des surfaces."
            ),
        )
    )

    associated_ids = {
        zone.id.removeprefix("validee-") for zone in dossier.zones
    }
    retained_unassigned = sorted(
        candidate.id
        for candidate in (
            dossier.controle_technique.zones_candidates
            if dossier.controle_technique
            else []
        )
        if candidate.statut == StatutRevue.RETENUE
        and candidate.id not in associated_ids
    )
    checks.append(
        GlobalCheck(
            code="zones_retenues_affectees",
            statut=(
                GlobalCheckStatus.BLOQUANT
                if retained_unassigned
                else GlobalCheckStatus.OK
            ),
            message=(
                f"{len(retained_unassigned)} zone(s) retenue(s) ne sont affectées à aucun lot."
                if retained_unassigned
                else "Toutes les zones déjà retenues sont affectées."
            ),
            candidate_zone_ids=retained_unassigned,
        )
    )
    return checks


def reconcile_dossier(
    dossier: Dossier, rules: ReconciliationRules = DEFAULT_RULES
) -> ReconciliationResult:
    """Compute and persist a deterministic reconciliation result."""

    control = dossier.controle_technique
    previous = dossier.reconciliation
    previous_decisions = {
        proposal.id: proposal.decision_manuelle
        for proposal in (previous.lot_proposals if previous else [])
        if proposal.decision_manuelle is not None
    }
    if control is None:
        result = ReconciliationResult(
            version_regles=RECONCILIATION_RULES_VERSION,
            profil_regles=rules.profile_name,
            tolerance_absolue_m2=rules.tolerance.absolute_m2,
            tolerance_relative=rules.tolerance.relative,
            contours_analyses=0,
        )
        dossier.reconciliation = result
        return result

    evidence: dict[str, Evidence] = {}
    excluded_ids: list[str] = []
    unresolved_ids: list[str] = []
    business_candidates: list[BusinessZoneCandidate] = []
    principal_candidates: dict[str, CandidateZone] = {}

    for candidate in control.zones_candidates:
        exclusion = _technical_exclusion_reason(dossier, candidate, rules)
        if exclusion is not None:
            reason, value = exclusion
            excluded_ids.append(candidate.id)
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-exclusion-{candidate.id}",
                    polarite=EvidencePolarity.NEGATIVE,
                    source=reason,
                    valeur=value,
                    fiabilite=EvidenceReliability.FORTE,
                    description=(
                        "Contour exclu par une règle technique traçable et réversible."
                    ),
                    candidate_zone_ids=[candidate.id],
                    provenance=candidate.provenance,
                ),
            )
            continue

        category: CategorieZone | None = None
        if _matches(candidate.calque, rules.principal_layer_patterns):
            category = CategorieZone.PRINCIPALE
            principal_candidates[candidate.id] = candidate
        elif _matches(candidate.calque, rules.annex_layer_patterns):
            category = CategorieZone.SECONDAIRE_ANNEXE

        if category is None:
            unresolved_ids.append(candidate.id)
            continue

        layer_evidence_id = _add_evidence(
            evidence,
            Evidence(
                id=f"preuve-calque-{candidate.id}",
                polarite=EvidencePolarity.POSITIVE,
                source="profil_de_calques",
                valeur=candidate.calque,
                fiabilite=EvidenceReliability.MOYENNE,
                description=(
                    "Le calque correspond à une convention métier du profil; "
                    "ce signal ne suffit jamais seul à valider une zone."
                ),
                candidate_zone_ids=[candidate.id],
                provenance=candidate.provenance,
            ),
        )
        business_candidates.append(
            BusinessZoneCandidate(
                candidate_zone_id=candidate.id,
                categorie_proposee=category,
                statut=VerificationStatus.NON_RESOLU,
                evidence_ids=[layer_evidence_id],
                motif="Contour calculable sur un calque métier conventionné.",
            )
        )

    text_points: list[_Point[TexteDxf]] = []
    text_index: _SpatialIndex[TexteDxf] = _SpatialIndex(rules.spatial_cell_size)
    for text in control.textes:
        planche_id = _resolved_text_planche(text, dossier.planches)
        point = _Point(
            planche_id=planche_id,
            x=text.x,
            y=text.y,
            stable_id=text.handle_dxf,
            value=text,
        )
        text_points.append(point)
        text_index.add(point)

    anchors: list[_LotAnchor] = []
    anchor_index: _SpatialIndex[_LotAnchor] = _SpatialIndex(
        rules.spatial_cell_size
    )
    for point in text_points:
        text = point.value
        normalized = text.contenu_normalise or _normalize(text.contenu)
        if not _matches(text.calque, rules.lot_label_layer_patterns):
            continue
        if not _LOT_NUMBER_ONLY.fullmatch(normalized):
            continue
        anchor = _LotAnchor(
            occurrence_id=f"occurrence-{text.handle_dxf}",
            numero=normalized,
            text=text,
            planche_id=point.planche_id,
        )
        anchors.append(anchor)
        anchor_index.add(
            _Point(
                planche_id=point.planche_id,
                x=text.x,
                y=text.y,
                stable_id=text.handle_dxf,
                value=anchor,
            )
        )
    anchors.sort(key=lambda item: (item.numero, item.occurrence_id))

    surfaces_by_occurrence: dict[
        str, list[tuple[float, TexteDxf, float]]
    ] = defaultdict(list)
    for point in text_points:
        text = point.value
        if not _matches(text.calque, rules.surface_label_layer_patterns):
            continue
        surface = parse_principal_surface(text.contenu)
        if surface is None:
            continue
        nearby = anchor_index.query(
            point.planche_id,
            point.x,
            point.y,
            rules.surface_assignment_radius,
        )
        if nearby:
            distance, anchor_point = nearby[0]
            surfaces_by_occurrence[anchor_point.value.occurrence_id].append(
                (distance, text, surface)
            )

    candidates_by_occurrence: dict[str, list[CandidateZone]] = defaultdict(list)
    for candidate in principal_candidates.values():
        center = _candidate_center(candidate)
        if center is None:
            continue
        nearby = anchor_index.query(
            candidate.planche_region,
            center[0],
            center[1],
            rules.lot_assignment_radius,
        )
        if nearby:
            candidates_by_occurrence[
                nearby[0][1].value.occurrence_id
            ].append(candidate)

    occurrences: list[_Occurrence] = []
    for anchor in anchors:
        nearby_titles = [
            (distance, point)
            for distance, point in text_index.query(
                anchor.planche_id,
                anchor.text.x,
                anchor.text.y,
                rules.title_assignment_radius,
            )
            if any(
                marker
                in (
                    point.value.contenu_normalise
                    or _normalize(point.value.contenu)
                )
                for marker in rules.plan_title_markers
            )
        ]
        nearby_full_labels = []
        for distance, point in text_index.query(
            anchor.planche_id,
            anchor.text.x,
            anchor.text.y,
            rules.lot_assignment_radius,
        ):
            normalized = point.value.contenu_normalise or _normalize(
                point.value.contenu
            )
            match = _FULL_LOT_LABEL.match(normalized)
            if match and match.group(1) == anchor.numero:
                nearby_full_labels.append((distance, point))
        nearby_automatic_titles = [
            (distance, point)
            for distance, point in nearby_titles
            if any(
                marker
                in (
                    point.value.contenu_normalise
                    or _normalize(point.value.contenu)
                )
                for marker in rules.automatic_title_markers
            )
        ]

        assigned = sorted(
            candidates_by_occurrence.get(anchor.occurrence_id, []),
            key=lambda item: item.id,
        )
        surface_matches = sorted(
            surfaces_by_occurrence.get(anchor.occurrence_id, []),
            key=lambda item: (item[0], item[1].handle_dxf),
        )
        if not nearby_titles and not nearby_full_labels:
            # A bare number near geometry is too ambiguous to be a lot proposal.
            continue
        if not assigned and not surface_matches:
            continue

        occurrence_evidence: list[str] = []
        occurrence_evidence.append(
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-lot-{anchor.occurrence_id}",
                    polarite=EvidencePolarity.POSITIVE,
                    source="texte_numero_lot",
                    valeur=anchor.numero,
                    fiabilite=EvidenceReliability.MOYENNE,
                    description=(
                        "Numéro positionné sur un calque de repères de lots; "
                        "ce texte seul ne vaut pas validation."
                    ),
                    provenance=_text_provenance(anchor.text, dossier),
                ),
            )
        )
        if nearby_titles:
            distance, title = nearby_titles[0]
            occurrence_evidence.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-titre-{anchor.occurrence_id}",
                        polarite=EvidencePolarity.POSITIVE,
                        source="titre_plan_copropriete",
                        valeur=title.value.contenu,
                        fiabilite=EvidenceReliability.MOYENNE,
                        description=(
                            "Le repère est proche d'un titre de plan de copropriété."
                        ),
                        distance=distance,
                        provenance=_text_provenance(title.value, dossier),
                    ),
                )
            )
        if nearby_full_labels:
            distance, label = nearby_full_labels[0]
            occurrence_evidence.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-libelle-{anchor.occurrence_id}",
                        polarite=EvidencePolarity.POSITIVE,
                        source="libelle_lot_explicite",
                        valeur=label.value.contenu,
                        fiabilite=EvidenceReliability.MOYENNE,
                        description=(
                            "Un libellé explicite du même lot est proche du repère."
                        ),
                        distance=distance,
                        provenance=_text_provenance(label.value, dossier),
                    ),
                )
            )

        annotated_surface: float | None = None
        if surface_matches:
            surface_distance, selected_surface_text, annotated_surface = (
                surface_matches[0]
            )
            occurrence_evidence.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-surface-{anchor.occurrence_id}",
                        polarite=EvidencePolarity.POSITIVE,
                        source="surface_annotee",
                        valeur=annotated_surface,
                        fiabilite=EvidenceReliability.MOYENNE,
                        description=(
                            "Surface principale lue dans un texte DXF positionné."
                        ),
                        distance=surface_distance,
                        provenance=_text_provenance(
                            selected_surface_text, dossier
                        ),
                    ),
                )
            )

        geometric_surface = sum(
            candidate.surface_geometrique_m2 or 0.0 for candidate in assigned
        )
        if assigned:
            occurrence_evidence.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-geometrie-{anchor.occurrence_id}",
                        polarite=EvidencePolarity.POSITIVE,
                        source="somme_contours_principaux",
                        valeur=round(geometric_surface, 6),
                        fiabilite=EvidenceReliability.FORTE,
                        description=(
                            f"Somme déterministe de {len(assigned)} contour(s) "
                            "principaux affectés par proximité."
                        ),
                        candidate_zone_ids=[item.id for item in assigned],
                        provenance=assigned[0].provenance,
                    ),
                )
            )

        if annotated_surface is None or not assigned:
            automatic_status = VerificationStatus.NON_RESOLU
        elif not rules.tolerance.accepts(geometric_surface, annotated_surface):
            automatic_status = VerificationStatus.CONTRADICTOIRE
            occurrence_evidence.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-ecart-{anchor.occurrence_id}",
                        polarite=EvidencePolarity.NEGATIVE,
                        source="ecart_surface_hors_tolerance",
                        valeur=round(
                            geometric_surface - annotated_surface, 6
                        ),
                        fiabilite=EvidenceReliability.FORTE,
                        description=(
                            "La somme géométrique et la surface annotée dépassent "
                            "la tolérance absolue + relative."
                        ),
                        candidate_zone_ids=[item.id for item in assigned],
                    ),
                )
            )
        elif (
            _planche_status(dossier, anchor.planche_id) == StatutRevue.RETENUE
            and nearby_automatic_titles
            and len(assigned) >= rules.automatic_minimum_contours
        ):
            automatic_status = VerificationStatus.AUTO_VERIFIE
        else:
            automatic_status = VerificationStatus.A_CONFIRMER

        occurrences.append(
            _Occurrence(
                anchor=anchor,
                candidate_ids=[item.id for item in assigned],
                geometric_surface_m2=geometric_surface,
                annotated_surface_m2=annotated_surface,
                automatic_status=automatic_status,
                evidence_ids=occurrence_evidence,
            )
        )

    occurrences_by_number: dict[str, list[_Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        occurrences_by_number[occurrence.anchor.numero].append(occurrence)

    proposals: list[LotProposal] = []
    status_priority = {
        VerificationStatus.CONTRADICTOIRE: 4,
        VerificationStatus.NON_RESOLU: 3,
        VerificationStatus.A_CONFIRMER: 2,
        VerificationStatus.AUTO_VERIFIE: 1,
    }
    for number in sorted(
        occurrences_by_number, key=lambda item: (len(item), item)
    ):
        grouped = sorted(
            occurrences_by_number[number],
            key=lambda item: item.anchor.occurrence_id,
        )
        automatic_status = max(
            (item.automatic_status for item in grouped),
            key=lambda item: status_priority[item],
        )
        candidate_ids = sorted(
            {
                candidate_id
                for item in grouped
                for candidate_id in item.candidate_ids
            }
        )
        annotated_values = [
            item.annotated_surface_m2
            for item in grouped
            if item.annotated_surface_m2 is not None
        ]
        geometric_surface = sum(
            item.geometric_surface_m2 for item in grouped
        )
        annotated_surface = (
            sum(annotated_values) if annotated_values else None
        )
        proposal_id = f"proposition-lot-{number}"
        decision = previous_decisions.get(proposal_id)
        proposal = LotProposal(
            id=proposal_id,
            numero_propose=number,
            occurrence_ids=[
                item.anchor.occurrence_id for item in grouped
            ],
            candidate_zone_ids=candidate_ids,
            surface_geometrique_m2=geometric_surface,
            surface_annotee_m2=annotated_surface,
            surface_proposee_m2=(
                annotated_surface
                if annotated_surface is not None
                else geometric_surface
            ),
            statut_automatique=automatic_status,
            statut=(
                decision.statut if decision is not None else automatic_status
            ),
            evidence_ids=list(
                dict.fromkeys(
                    evidence_id
                    for item in grouped
                    for evidence_id in item.evidence_ids
                )
            ),
            decision_manuelle=decision,
        )
        proposals.append(proposal)
        logger.debug(
            "Proposal %s: status=%s occurrences=%d zones=%d "
            "geometric=%.3f annotated=%s",
            proposal.id,
            proposal.statut.value,
            len(proposal.occurrence_ids),
            len(proposal.candidate_zone_ids),
            proposal.surface_geometrique_m2,
            proposal.surface_annotee_m2,
        )

    proposal_by_candidate: dict[str, LotProposal] = {}
    for proposal in proposals:
        for candidate_id in proposal.candidate_zone_ids:
            proposal_by_candidate[candidate_id] = proposal
    business_candidates = [
        item.model_copy(
            update={
                "statut": proposal_by_candidate[
                    item.candidate_zone_id
                ].statut,
                "motif": (
                    "Contour regroupé dans la proposition "
                    + proposal_by_candidate[item.candidate_zone_id].id
                ),
            }
        )
        if item.candidate_zone_id in proposal_by_candidate
        else item
        for item in business_candidates
    ]

    result = ReconciliationResult(
        version_regles=RECONCILIATION_RULES_VERSION,
        profil_regles=rules.profile_name,
        tolerance_absolue_m2=rules.tolerance.absolute_m2,
        tolerance_relative=rules.tolerance.relative,
        contours_analyses=len(control.zones_candidates),
        business_zone_candidates=sorted(
            business_candidates,
            key=lambda item: item.candidate_zone_id,
        ),
        lot_proposals=proposals,
        preuves=sorted(evidence.values(), key=lambda item: item.id),
        candidate_ids_exclus_techniquement=sorted(excluded_ids),
        candidate_ids_non_resolus=sorted(unresolved_ids),
    )
    result.controles_globaux = _build_global_checks(dossier, proposals)
    dossier.reconciliation = result
    logger.info(
        "Reconciliation %s: contours=%d business_candidates=%d "
        "proposals=%d auto=%d confirm=%d contradictions=%d "
        "technical_excluded=%d",
        rules.profile_name,
        result.contours_analyses,
        len(result.business_zone_candidates),
        len(result.lot_proposals),
        sum(
            item.statut == VerificationStatus.AUTO_VERIFIE
            for item in proposals
        ),
        sum(
            item.statut == VerificationStatus.A_CONFIRMER
            for item in proposals
        ),
        sum(
            item.statut == VerificationStatus.CONTRADICTOIRE
            for item in proposals
        ),
        len(result.candidate_ids_exclus_techniquement),
    )
    return result
