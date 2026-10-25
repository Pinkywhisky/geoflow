"""Reconciliation of a dossier analysed with a drawing profile.

The profile snapshot of the dossier decides wherever it has a rule; the
automatic (legacy) engine only fills what the profile leaves undecided, and
every decision records which of the two made it:

* contours: a geometric rule (source, excluded layout, self-intersection) or
  a layer role of the profile decides; a layer without role keeps the
  automatic classification;
* lots: principal zones are numbered by the profile's ordered sources; a lot
  the profile cannot number may be completed by the automatic engine, if it
  uses no contour the profile decided otherwise;
* annexes: a lot number written inside the annex (if the profile says so),
  else the automatic proximity/contact suggestion, never applied alone.

Human decisions (excluded planche or layer, confirmed lots and annexes) keep
their authority over both.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from app.domain import (
    BusinessZoneCandidate,
    CategorieZone,
    Dossier,
    Evidence,
    EvidencePolarity,
    EvidenceReliability,
    LotProposal,
    ProfileUsage,
    ReconciliationResult,
    VerificationStatus,
)
from app.domain.models import StatutRevue
from app.profiles.models import LAYER_ROLES, TEXT_SOURCES
from app.profiles.reading import ZoneReading, annex_lot_inside, read_plan
from app.reconciliation.engine import (
    DEFAULT_RULES,
    RECONCILIATION_RULES_VERSION,
    SurfaceTolerance,
    _add_evidence,
    _annex_attachment,
    _build_global_checks,
    _LotAnchor,
    _Point,
    _resolved_text_planche,
    _SpatialIndex,
    _text_provenance,
    human_exclusion_reason,
    reconcile_legacy,
)

logger = logging.getLogger(__name__)

CATEGORY_BY_ROLE = {
    "surface_principale": CategorieZone.PRINCIPALE,
    "annexe": CategorieZone.SECONDAIRE_ANNEXE,
    "partie_commune": CategorieZone.COMMUNE,
}
EXCLUSION_SOURCES = {
    "hors_source": ("hors_source_profil", "Contour hors de la source géométrique du profil."),
    "presentation_exclue": ("presentation_exclue_profil", "Contour sur une présentation exclue par le profil."),
    "auto_intersection": ("contour_auto_intersecte_profil", "Contour auto-intersecté, refusé par le profil."),
    "exclusion": ("calque_exclusion_profil", "Calque classé « Exclusion » par le profil."),
    "archive": ("calque_archive_profil", "Calque classé « Archive » par le profil."),
    "hors_copropriete": ("calque_hors_copropriete_profil", "Calque classé « Hors copropriété » par le profil."),
}
SET_ASIDE_ROLES = ("exclusion", "archive", "hors_copropriete")
STATUS_PRIORITY = {
    VerificationStatus.AUTO_VERIFIE: 0,
    VerificationStatus.A_CONFIRMER: 1,
    VerificationStatus.NON_RESOLU: 2,
    VerificationStatus.CONTRADICTOIRE: 3,
}


def _number_key(value: str) -> tuple[int, str]:
    return len(value), value


def _lot_proposal(
    number: str,
    members: list[ZoneReading],
    usage: ProfileUsage,
    tolerance: SurfaceTolerance,
    dossier: Dossier,
    evidence: dict[str, Evidence],
) -> LotProposal:
    rules = usage.rules
    members = sorted(members, key=lambda zone: zone.candidate.id)
    ids = [zone.candidate.id for zone in members]
    evidence_ids: list[str] = []
    best = min(
        (zone.lot for zone in members),
        key=lambda read: (read.source != "texte_dans_zone", read.distance or 0.0, read.text.handle_dxf),
    )
    evidence_ids.append(
        _add_evidence(
            evidence,
            Evidence(
                id=f"preuve-profil-lot-{number}",
                polarite=EvidencePolarity.POSITIVE,
                source="numero_lot_profil",
                valeur=number,
                fiabilite=EvidenceReliability.FORTE,
                description=(
                    f"Numéro lu selon la règle du profil ({TEXT_SOURCES[best.source].lower()}, "
                    f"motif « {rules.lot_identification.pattern} »)."
                ),
                distance=best.distance,
                candidate_zone_ids=ids,
                provenance=_text_provenance(best.text, dossier),
            ),
        )
    )
    doubtful = False
    for zone in members:
        read = zone.lot
        if read.conflict_value is not None:
            doubtful = True
            evidence_ids.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-profil-conflit-{zone.candidate.id}",
                        polarite=EvidencePolarity.NEGATIVE,
                        source="source_secondaire_contradictoire",
                        valeur=str(read.conflict_value),
                        fiabilite=EvidenceReliability.FORTE,
                        description=(
                            f"Source secondaire contradictoire ({TEXT_SOURCES[read.conflict_source].lower()}) : "
                            f"lot {read.conflict_value}. La source prioritaire du profil "
                            f"({TEXT_SOURCES[read.source].lower()}) donne le lot {number}."
                        ),
                        candidate_zone_ids=[zone.candidate.id],
                        provenance=_text_provenance(read.conflict_text, dossier),
                    ),
                )
            )
        if read.ambiguous:
            doubtful = True
            evidence_ids.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-profil-ambigu-{zone.candidate.id}",
                        polarite=EvidencePolarity.NEGATIVE,
                        source="numeros_multiples_dans_zone",
                        valeur=", ".join(str(item) for item in read.ambiguous),
                        fiabilite=EvidenceReliability.FORTE,
                        description="Plusieurs numéros sont inscrits dans la zone : la source suivante du profil a été utilisée.",
                        candidate_zone_ids=[zone.candidate.id],
                    ),
                )
            )

    annotations = {
        zone.surface.text.handle_dxf: zone.surface
        for zone in members
        if zone.surface.text is not None and zone.surface.value is not None
    }
    for handle, read in sorted(annotations.items()):
        evidence_ids.append(
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-profil-surface-{handle}",
                    polarite=EvidencePolarity.POSITIVE,
                    source="surface_annotee_profil",
                    valeur=float(read.value),
                    fiabilite=EvidenceReliability.MOYENNE,
                    description=(
                        f"Surface annotée lue selon la règle du profil ({TEXT_SOURCES[read.source].lower()}, "
                        f"motif « {rules.surface_identification.pattern} »)."
                    ),
                    distance=read.distance,
                    provenance=_text_provenance(read.text, dossier),
                ),
            )
        )
    annotated = sum(float(read.value) for read in annotations.values()) if annotations else None
    geometric = sum(zone.area_m2 or 0.0 for zone in members)
    evidence_ids.append(
        _add_evidence(
            evidence,
            Evidence(
                id=f"preuve-profil-geometrie-{number}",
                polarite=EvidencePolarity.POSITIVE,
                source="somme_contours_principaux",
                valeur=round(geometric, 6),
                fiabilite=EvidenceReliability.FORTE,
                description=f"Somme de {len(members)} contour(s) de surface principale portant ce numéro selon le profil.",
                candidate_zone_ids=ids,
                provenance=members[0].candidate.provenance,
            ),
        )
    )
    levels = sorted({zone.level for zone in members if zone.level})
    if levels:
        evidence_ids.append(
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-profil-niveau-{number}",
                    polarite=EvidencePolarity.POSITIVE,
                    source="niveau_profil",
                    valeur=", ".join(levels),
                    fiabilite=EvidenceReliability.MOYENNE,
                    description="Niveau lu selon les correspondances du profil.",
                    candidate_zone_ids=ids,
                ),
            )
        )
    elif rules.levels.aliases:
        evidence_ids.append(
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-profil-niveau-{number}",
                    polarite=EvidencePolarity.NEGATIVE,
                    source="niveau_non_determine",
                    fiabilite=EvidenceReliability.FAIBLE,
                    description="Aucun texte de niveau du profil à proximité : niveau à préciser.",
                    candidate_zone_ids=ids,
                ),
            )
        )
    spaces = sorted({zone.space for zone in members})
    if len(spaces) > 1:
        doubtful = True
        evidence_ids.append(
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-profil-espaces-{number}",
                    polarite=EvidencePolarity.NEGATIVE,
                    source="plusieurs_espaces",
                    valeur=", ".join(spaces),
                    fiabilite=EvidenceReliability.MOYENNE,
                    description="Le lot est dessiné dans plusieurs espaces : vérifiez qu’il ne s’agit pas de deux représentations.",
                    candidate_zone_ids=ids,
                ),
            )
        )

    proposed = annotated if annotated is not None else geometric
    if annotated is None:
        status = VerificationStatus.NON_RESOLU
    elif not tolerance.accepts(geometric, annotated):
        evidence_ids.append(
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-profil-ecart-{number}",
                    polarite=EvidencePolarity.NEGATIVE,
                    source="ecart_surface_hors_tolerance",
                    valeur=round(geometric - annotated, 6),
                    fiabilite=EvidenceReliability.FORTE,
                    description="La somme géométrique et la surface annotée dépassent la tolérance du profil.",
                    candidate_zone_ids=ids,
                ),
            )
        )
        policy = rules.priorities.surface_conflict
        if policy == "signaler":
            status = VerificationStatus.CONTRADICTOIRE
        else:
            # The profile names the source of truth, but a gap is never auto-verified.
            status = VerificationStatus.A_CONFIRMER
            proposed = annotated if policy == "annotation" else geometric
            evidence_ids.append(
                _add_evidence(
                    evidence,
                    Evidence(
                        id=f"preuve-profil-priorite-{number}",
                        polarite=EvidencePolarity.POSITIVE,
                        source="priorite_surface_profil",
                        valeur=policy,
                        fiabilite=EvidenceReliability.MOYENNE,
                        description=(
                            "En cas d’écart, le profil fait foi à "
                            + ("l’annotation" if policy == "annotation" else "la géométrie")
                            + " : la surface proposée la suit, à confirmer."
                        ),
                        candidate_zone_ids=ids,
                    ),
                )
            )
    elif doubtful:
        status = VerificationStatus.A_CONFIRMER
    else:
        status = VerificationStatus.AUTO_VERIFIE

    texts = {zone.lot.text.handle_dxf for zone in members if zone.lot.text is not None}
    return LotProposal(
        id=f"proposition-lot-{number}",
        numero_propose=number,
        occurrence_ids=[f"occurrence-{handle}" for handle in sorted(texts)],
        candidate_zone_ids=ids,
        surface_geometrique_m2=geometric,
        surface_annotee_m2=annotated,
        surface_proposee_m2=proposed,
        statut_automatique=status,
        statut=status,
        evidence_ids=evidence_ids,
        origine="profil",
    )


def reconcile_with_profile(dossier: Dossier, usage: ProfileUsage) -> ReconciliationResult:
    rules = usage.rules
    assert rules is not None
    surface_rule = rules.surface_identification
    tolerance = SurfaceTolerance(surface_rule.tolerance_abs_m2, surface_rule.tolerance_rel_pct / 100)
    label = f"{usage.profile_name} · v{usage.revision}"
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
            profil_regles=label,
            tolerance_absolue_m2=tolerance.absolute_m2,
            tolerance_relative=tolerance.relative,
            contours_analyses=0,
        )
        dossier.reconciliation = result
        return result

    # The automatic engine on the same snapshot: its outcome is only used
    # where the profile has no rule, and labelled as such.
    legacy = reconcile_legacy(dossier, DEFAULT_RULES)
    legacy_evidence = {item.id: item for item in legacy.preuves}
    legacy_business = {item.candidate_zone_id: item for item in legacy.business_zone_candidates}
    legacy_excluded = set(legacy.candidate_ids_exclus_techniquement)

    human = {
        candidate.id: reason
        for candidate in control.zones_candidates
        if (reason := human_exclusion_reason(dossier, candidate)) is not None
    }
    reading = read_plan(rules, control, skip_ids=frozenset(human), use_retained_areas=True)
    zone_by_id = {zone.candidate.id: zone for zone in reading.zones}
    retained_layers = {layer.nom for layer in control.calques if layer.statut == StatutRevue.RETENUE}

    evidence: dict[str, Evidence] = {}
    from_legacy: set[str] = set()

    def take_legacy(evidence_ids: list[str]) -> None:
        for evidence_id in evidence_ids:
            if evidence_id in legacy_evidence:
                evidence.setdefault(evidence_id, legacy_evidence[evidence_id])
                from_legacy.add(evidence_id)

    excluded_ids: list[str] = []
    unresolved_ids: list[str] = []
    business: list[BusinessZoneCandidate] = []
    principal: list[ZoneReading] = []
    # Contours the automatic engine may still use for a lot.
    legacy_usable: set[str] = set()
    for candidate in control.zones_candidates:
        if candidate.id in human:
            # Same evidence as the automatic engine: a human decision.
            excluded_ids.append(candidate.id)
            evidence.setdefault(f"preuve-exclusion-{candidate.id}", legacy_evidence[f"preuve-exclusion-{candidate.id}"])
            continue
        zone = zone_by_id.get(candidate.id)
        reason = reading.rejected.get(candidate.id)
        role = zone.role if zone is not None else None
        if reason is None and role in SET_ASIDE_ROLES and candidate.calque not in retained_layers:
            reason = role
        if reason is not None:
            source, description = EXCLUSION_SOURCES[reason]
            excluded_ids.append(candidate.id)
            _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-exclusion-{candidate.id}",
                    polarite=EvidencePolarity.NEGATIVE,
                    source=source,
                    valeur=candidate.calque if reason in SET_ASIDE_ROLES else candidate.planche_region,
                    fiabilite=EvidenceReliability.FORTE,
                    description=description,
                    candidate_zone_ids=[candidate.id],
                    provenance=candidate.provenance,
                ),
            )
            continue
        if role in CATEGORY_BY_ROLE:
            layer_evidence = _add_evidence(
                evidence,
                Evidence(
                    id=f"preuve-calque-{candidate.id}",
                    polarite=EvidencePolarity.POSITIVE,
                    source="role_calque_profil",
                    valeur=candidate.calque,
                    fiabilite=EvidenceReliability.FORTE,
                    description=f"Calque classé « {LAYER_ROLES[role]} » par le profil.",
                    candidate_zone_ids=[candidate.id],
                    provenance=candidate.provenance,
                ),
            )
            business.append(
                BusinessZoneCandidate(
                    candidate_zone_id=candidate.id,
                    categorie_proposee=CATEGORY_BY_ROLE[role],
                    statut=VerificationStatus.NON_RESOLU,
                    evidence_ids=[layer_evidence],
                    motif=f"{LAYER_ROLES[role]} selon le profil.",
                    origine="profil",
                )
            )
            if role == "surface_principale":
                principal.append(zone)
                if zone.lot.value is None:
                    legacy_usable.add(candidate.id)
            continue
        # No rule of the profile for this contour: automatic classification.
        if candidate.id in legacy_excluded:
            excluded_ids.append(candidate.id)
            take_legacy([f"preuve-exclusion-{candidate.id}"])
        elif candidate.id in legacy_business:
            item = legacy_business[candidate.id]
            take_legacy(item.evidence_ids)
            business.append(item.model_copy(update={"origine": "legacy"}))
            legacy_usable.add(candidate.id)
        else:
            unresolved_ids.append(candidate.id)

    groups: dict[str, list[ZoneReading]] = defaultdict(list)
    for zone in principal:
        if zone.lot.value is not None:
            groups[str(zone.lot.value)].append(zone)
    proposals = [
        _lot_proposal(number, groups[number], usage, tolerance, dossier, evidence)
        for number in sorted(groups, key=_number_key)
    ]
    used = {candidate_id for proposal in proposals for candidate_id in proposal.candidate_zone_ids}
    for proposal in legacy.lot_proposals:
        if proposal.numero_propose in groups or any(
            candidate_id not in legacy_usable or candidate_id in used for candidate_id in proposal.candidate_zone_ids
        ):
            continue
        take_legacy(proposal.evidence_ids)
        note = _add_evidence(
            evidence,
            Evidence(
                id=f"preuve-legacy-lot-{proposal.numero_propose}",
                polarite=EvidencePolarity.NEGATIVE,
                source="lot_complete_par_legacy",
                valeur=proposal.numero_propose,
                fiabilite=EvidenceReliability.FAIBLE,
                description="Aucune règle du profil n’identifie ce lot : il est complété par l’analyse automatique.",
                candidate_zone_ids=list(proposal.candidate_zone_ids),
            ),
        )
        from_legacy.add(note)
        used.update(proposal.candidate_zone_ids)
        proposals.append(
            proposal.model_copy(
                update={
                    "origine": "legacy",
                    "candidate_zone_ids_ecartes": [
                        item for item in proposal.candidate_zone_ids_ecartes if item in legacy_usable
                    ],
                    "evidence_ids": [*proposal.evidence_ids, note],
                }
            )
        )
    proposals.sort(key=lambda item: _number_key(item.numero_propose))
    for index, proposal in enumerate(proposals):
        decision = previous_decisions.get(proposal.id)
        if decision is not None:
            proposals[index] = proposal.model_copy(update={"decision_manuelle": decision, "statut": decision.statut})

    proposal_by_candidate = {
        candidate_id: proposal for proposal in proposals for candidate_id in proposal.candidate_zone_ids
    }

    def with_proposal_status(item: BusinessZoneCandidate) -> BusinessZoneCandidate:
        proposal = proposal_by_candidate.get(item.candidate_zone_id)
        if proposal is not None:
            return item.model_copy(
                update={"statut": proposal.statut, "motif": "Contour regroupé dans la proposition " + proposal.id}
            )
        if item.categorie_proposee != CategorieZone.PRINCIPALE:
            return item
        motif = (
            "Surface principale sans numéro de lot lisible selon le profil."
            if item.origine == "profil"
            else "Contour calculable sur un calque métier conventionné."
        )
        return item.model_copy(update={"statut": VerificationStatus.NON_RESOLU, "motif": motif})

    business = [with_proposal_status(item) for item in business]

    # Annexes classified by the profile.
    anchor_index: _SpatialIndex[_LotAnchor] = _SpatialIndex(DEFAULT_RULES.spatial_cell_size)
    anchor_ids: set[str] = set()
    for proposal in proposals:
        if proposal.origine != "profil":
            continue
        for zone in groups[proposal.numero_propose]:
            text = zone.lot.text
            occurrence_id = f"occurrence-{text.handle_dxf}"
            if occurrence_id in anchor_ids:
                continue
            anchor_ids.add(occurrence_id)
            planche_id = _resolved_text_planche(text, dossier.planches)
            anchor = _LotAnchor(occurrence_id, proposal.numero_propose, text, planche_id)
            anchor_index.add(_Point(planche_id, text.x, text.y, text.handle_dxf, anchor))
    candidate_by_id = {item.id: item for item in control.zones_candidates}
    zone_by_candidate = {zone.id.removeprefix("validee-"): zone for zone in dossier.zones}
    contours_by_number = {
        proposal.numero_propose: [candidate_by_id[item] for item in proposal.candidate_zone_ids if item in candidate_by_id]
        for proposal in proposals
    }
    annex_rules = replace(DEFAULT_RULES, lot_assignment_radius=rules.lot_identification.radius)

    def annex_state(item: BusinessZoneCandidate) -> BusinessZoneCandidate:
        if (
            item.origine != "profil"
            or item.categorie_proposee != CategorieZone.SECONDAIRE_ANNEXE
            or item.candidate_zone_id in proposal_by_candidate
        ):
            return item
        candidate = candidate_by_id.get(item.candidate_zone_id)
        attached = zone_by_candidate.get(item.candidate_zone_id)
        if attached is None or attached.lot_id is None:
            if rules.annexes.lot_source == "numero_dans_annexe":
                number = annex_lot_inside(reading, zone_by_id[item.candidate_zone_id])
                if number is not None:
                    evidence_id = _add_evidence(
                        evidence,
                        Evidence(
                            id=f"preuve-profil-annexe-{item.candidate_zone_id}",
                            polarite=EvidencePolarity.POSITIVE,
                            source="numero_lot_dans_annexe",
                            valeur=str(number),
                            fiabilite=EvidenceReliability.FORTE,
                            description="Numéro de lot inscrit dans l’annexe, lu selon la règle du profil.",
                            candidate_zone_ids=[item.candidate_zone_id],
                        ),
                    )
                    return item.model_copy(
                        update={
                            "statut": VerificationStatus.A_CONFIRMER,
                            "numero_lot_propose": str(number),
                            "evidence_ids": [*item.evidence_ids, evidence_id],
                            "motif": f"Lot {number} inscrit dans l’annexe, à confirmer.",
                        }
                    )
            if rules.annexes.fallback == "rien":
                return item.model_copy(update={"motif": "Annexe selon le profil ; le profil ne propose pas de lot."})
        return _annex_attachment(
            item,
            candidate,
            attached,
            dossier,
            anchor_index,
            anchor_ids,
            contours_by_number,
            evidence,
            annex_rules,
        )

    business = [annex_state(item) for item in business]

    preuves = []
    for item in evidence.values():
        if item.id in from_legacy:
            origine = "legacy"
        elif item.id.startswith("preuve-exclusion-") and item.candidate_zone_ids and item.candidate_zone_ids[0] in human:
            origine = None
        else:
            origine = "profil"
        preuves.append(item.model_copy(update={"origine": origine}) if item.origine != origine else item)
    result = ReconciliationResult(
        version_regles=RECONCILIATION_RULES_VERSION,
        profil_regles=label,
        tolerance_absolue_m2=tolerance.absolute_m2,
        tolerance_relative=tolerance.relative,
        contours_analyses=len(control.zones_candidates),
        business_zone_candidates=sorted(business, key=lambda item: item.candidate_zone_id),
        lot_proposals=proposals,
        preuves=sorted(preuves, key=lambda item: item.id),
        candidate_ids_exclus_techniquement=sorted(excluded_ids),
        candidate_ids_non_resolus=sorted(unresolved_ids),
    )
    result.controles_globaux = _build_global_checks(dossier, proposals)
    dossier.reconciliation = result
    logger.info(
        "Reconciliation with profile %s r%s: contours=%d proposals=%d (profile=%d legacy=%d)",
        usage.profile_id,
        usage.revision,
        result.contours_analyses,
        len(proposals),
        sum(item.origine == "profil" for item in proposals),
        sum(item.origine == "legacy" for item in proposals),
    )
    return result
