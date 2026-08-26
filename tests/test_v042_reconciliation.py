from __future__ import annotations

from copy import deepcopy

import pytest

from app.domain import (
    BBox,
    CandidateZone,
    ControleTechnique,
    Dossier,
    LayerInfo,
    Lot,
    ManualReconciliationDecision,
    Planche,
    PlanImporte,
    Provenance,
    StatutValidationDonnees,
    TexteDxf,
    VerificationStatus,
)
from app.domain.models import StatutRevue
from app.reconciliation import (
    ReconciliationRules,
    SurfaceTolerance,
    parse_principal_surface,
    reconcile_dossier,
)


PRINCIPAL = "80-SUPERFICIE PRINCIPALE"
ANNEX = "81-SUPERFICIE SECONDAIRE ou ANNEXE"
EXCLUDED = "82-SUPERFICIE EXCLUE"
LOT_LAYER = "05-ECRITURE LOT"
SURFACE_LAYER = "83-SUPERFICIE ECRITURE"


def provenance(handle: str, layer: str, planche: str = "model") -> Provenance:
    return Provenance(
        fichier_source="synthetic.dxf",
        handle_dxf=handle,
        calque=layer,
        type_entite="LWPOLYLINE",
        planche_region=planche,
        methode_detection="fixture_synthetique",
    )


def candidate(
    identifier: str,
    x: float,
    area: float,
    layer: str = PRINCIPAL,
    planche: str = "model",
) -> CandidateZone:
    return CandidateZone(
        id=identifier,
        type_entite="LWPOLYLINE",
        calque=layer,
        handle_dxf=identifier,
        planche_region=planche,
        surface_geometrique_unites=area,
        surface_geometrique_m2=area,
        bbox=BBox(min_x=x - 0.2, min_y=-0.2, max_x=x + 0.2, max_y=0.2),
        provenance=provenance(identifier, layer, planche),
    )


def text(
    identifier: str,
    content: str,
    x: float,
    y: float,
    layer: str,
    planche: str = "model",
) -> TexteDxf:
    return TexteDxf(
        contenu=content,
        x=x,
        y=y,
        calque=layer,
        handle_dxf=identifier,
        planche_region=planche,
        provenance=Provenance(
            fichier_source="synthetic.dxf",
            handle_dxf=identifier,
            calque=layer,
            type_entite="TEXT",
            planche_region=planche,
            methode_detection="fixture_synthetique",
        ),
    )


def synthetic_dossier(
    *,
    candidates: list[CandidateZone] | None = None,
    texts: list[TexteDxf] | None = None,
    planche_status: StatutRevue = StatutRevue.RETENUE,
    layers: list[LayerInfo] | None = None,
) -> Dossier:
    selected_candidates = (
        [candidate("zone-a", -1, 4), candidate("zone-b", 1, 6)]
        if candidates is None
        else candidates
    )
    selected_texts = (
        [
            text("lot-1", "1", 0, 0, LOT_LAYER),
            text(
                "title-1",
                "PLAN D'IDENTIFICATION DES LOTS POUR MISE EN COPROPRIETE",
                0,
                4,
                "01-CARTOUCHE",
            ),
            text("surface-1", "S=10.0m²", 0, 1, SURFACE_LAYER),
        ]
        if texts is None
        else texts
    )
    layer_names = {
        item.calque for item in selected_candidates
    } | {item.calque for item in selected_texts}
    selected_layers = layers or [
        LayerInfo(nom=name, visible=True, gele=False, trace=True)
        for name in sorted(layer_names)
    ]
    return Dossier(
        id="synthetic",
        reference="SYNTHETIC",
        statut_validation_donnees=StatutValidationDonnees.A_VALIDER,
        plan_importe=PlanImporte(
            nom_fichier_original="synthetic.dxf",
            type_fichier="dxf",
            version_dxf="AC1024",
            unite_detectee="metre",
            unite_retenue="metre",
            unite_confirmee=True,
        ),
        controle_technique=ControleTechnique(
            version_dxf="AC1024",
            unite_detectee="metre",
            calques=selected_layers,
            textes=selected_texts,
            polylignes_fermees=len(selected_candidates),
            zones_candidates=selected_candidates,
        ),
        planches=[
            Planche(
                id="model",
                titre="Model",
                statut=planche_status,
                methode_detection="layout_dxf",
            )
        ],
    )


def single_proposal(dossier: Dossier):
    result = reconcile_dossier(dossier)
    assert len(result.lot_proposals) == 1
    return result.lot_proposals[0]


def test_parse_direct_principal_surface() -> None:
    assert parse_principal_surface("S=17.2m²") == pytest.approx(17.2)


def test_parse_decimal_comma_surface() -> None:
    assert parse_principal_surface("S=31,8 m²") == pytest.approx(31.8)


def test_parse_above_height_ignores_below_height() -> None:
    assert parse_principal_surface("S>1.80=31.8m² S<1.80=0.6m²") == 31.8


def test_parse_complex_total_has_priority() -> None:
    value = "S1>1.80=29.4m² S2>1.80=8.9m² TOT >1.8=44.8m²"
    assert parse_principal_surface(value) == 44.8


def test_parse_rejects_non_surface_text() -> None:
    assert parse_principal_surface("H=2.72m") is None


def test_absolute_surface_tolerance() -> None:
    assert SurfaceTolerance(absolute_m2=0.05, relative=0).accepts(10.0, 10.04)


def test_relative_surface_tolerance() -> None:
    assert SurfaceTolerance(absolute_m2=0, relative=0.01).accepts(100.0, 100.5)


def test_surface_tolerance_rejects_excess() -> None:
    assert not SurfaceTolerance(absolute_m2=0.05, relative=0.001).accepts(
        10.0, 10.2
    )


def test_multiple_independent_evidence_can_auto_verify() -> None:
    proposal = single_proposal(synthetic_dossier())
    assert proposal.statut == VerificationStatus.AUTO_VERIFIE
    assert proposal.surface_geometrique_m2 == pytest.approx(10)
    assert proposal.surface_annotee_m2 == pytest.approx(10)


def test_text_without_geometry_never_auto_verifies() -> None:
    proposal = single_proposal(synthetic_dossier(candidates=[]))
    assert proposal.statut == VerificationStatus.NON_RESOLU


def test_geometry_layer_without_text_does_not_create_lot() -> None:
    result = reconcile_dossier(synthetic_dossier(texts=[]))
    assert result.business_zone_candidates
    assert result.lot_proposals == []


def test_surface_text_without_lot_anchor_does_not_create_lot() -> None:
    texts = [
        text(
            "title",
            "PLAN D'IDENTIFICATION DES LOTS",
            0,
            4,
            "01-CARTOUCHE",
        ),
        text("surface", "S=10m²", 0, 1, SURFACE_LAYER),
    ]
    assert reconcile_dossier(synthetic_dossier(texts=texts)).lot_proposals == []


def test_unretained_planche_requires_confirmation() -> None:
    proposal = single_proposal(
        synthetic_dossier(planche_status=StatutRevue.CANDIDATE)
    )
    assert proposal.statut == VerificationStatus.A_CONFIRMER


def test_surface_disagreement_is_contradictory() -> None:
    dossier = synthetic_dossier()
    dossier.controle_technique.textes[-1].contenu = "S=12m²"
    proposal = single_proposal(dossier)
    assert proposal.statut == VerificationStatus.CONTRADICTOIRE
    assert any(
        proof.polarite.value == "negative"
        for proof in dossier.reconciliation.preuves
        if proof.id in proposal.evidence_ids
    )


def test_single_contour_is_never_auto_verified() -> None:
    proposal = single_proposal(
        synthetic_dossier(candidates=[candidate("only", 0, 10)])
    )
    assert proposal.statut == VerificationStatus.A_CONFIRMER


def test_profile_excluded_layer_is_traceable() -> None:
    excluded = candidate("excluded", 0, 10, EXCLUDED)
    result = reconcile_dossier(synthetic_dossier(candidates=[excluded], texts=[]))
    assert result.candidate_ids_exclus_techniquement == ["excluded"]
    assert any(proof.id == "preuve-exclusion-excluded" for proof in result.preuves)


def test_user_retained_layer_overrides_profile_exclusion() -> None:
    excluded = candidate("overridden", 0, 10, EXCLUDED)
    layers = [
        LayerInfo(
            nom=EXCLUDED,
            visible=True,
            gele=False,
            trace=True,
            statut=StatutRevue.RETENUE,
        )
    ]
    result = reconcile_dossier(
        synthetic_dossier(candidates=[excluded], texts=[], layers=layers)
    )
    assert result.candidate_ids_exclus_techniquement == []


def test_explicitly_excluded_business_layer_is_technical_exclusion() -> None:
    layers = [
        LayerInfo(
            nom=PRINCIPAL,
            visible=True,
            gele=False,
            trace=True,
            statut=StatutRevue.EXCLUE,
        )
    ]
    result = reconcile_dossier(synthetic_dossier(layers=layers))
    assert len(result.candidate_ids_exclus_techniquement) == 2
    assert result.business_zone_candidates == []


def test_hidden_frozen_or_non_plotted_is_not_excluded_alone() -> None:
    layers = [
        LayerInfo(
            nom=PRINCIPAL,
            visible=False,
            gele=True,
            trace=False,
        ),
        LayerInfo(
            nom=LOT_LAYER,
            visible=True,
            gele=False,
            trace=True,
        ),
        LayerInfo(
            nom=SURFACE_LAYER,
            visible=True,
            gele=False,
            trace=True,
        ),
        LayerInfo(
            nom="01-CARTOUCHE",
            visible=True,
            gele=False,
            trace=True,
        ),
    ]
    result = reconcile_dossier(synthetic_dossier(layers=layers))
    assert len(result.business_zone_candidates) == 2
    assert result.candidate_ids_exclus_techniquement == []


def test_abandoned_planche_excludes_its_contours() -> None:
    dossier = synthetic_dossier(planche_status=StatutRevue.ABANDONNEE)
    result = reconcile_dossier(dossier)
    assert len(result.candidate_ids_exclus_techniquement) == 2


def test_annex_is_business_candidate_but_not_principal_surface() -> None:
    candidates = [
        candidate("principal", -1, 10),
        candidate("annex", 1, 3, ANNEX),
    ]
    result = reconcile_dossier(synthetic_dossier(candidates=candidates))
    assert len(result.business_zone_candidates) == 2
    [proposal] = result.lot_proposals
    assert proposal.candidate_zone_ids == ["principal"]
    assert proposal.surface_geometrique_m2 == 10


def test_generic_copropriete_title_can_propose_but_not_auto_verify() -> None:
    dossier = synthetic_dossier()
    dossier.controle_technique.textes[1].contenu = "PLAN DE COPROPRIETE"
    proposal = single_proposal(dossier)
    assert proposal.statut == VerificationStatus.A_CONFIRMER


def test_two_occurrences_are_grouped_by_lot_number() -> None:
    candidates = [
        candidate("a1", -1, 4),
        candidate("a2", 1, 6),
        candidate("b1", 29, 8),
        candidate("b2", 31, 12),
    ]
    texts = [
        text("lot-a", "7", 0, 0, LOT_LAYER),
        text("surface-a", "S=10m²", 0, 1, SURFACE_LAYER),
        text(
            "title-a",
            "PLAN D'IDENTIFICATION DES LOTS",
            0,
            4,
            "01-CARTOUCHE",
        ),
        text("lot-b", "7", 30, 0, LOT_LAYER),
        text("surface-b", "S=20m²", 30, 1, SURFACE_LAYER),
        text(
            "title-b",
            "PLAN D'IDENTIFICATION DES LOTS",
            30,
            4,
            "01-CARTOUCHE",
        ),
    ]
    proposal = single_proposal(
        synthetic_dossier(candidates=candidates, texts=texts)
    )
    assert proposal.numero_propose == "7"
    assert len(proposal.occurrence_ids) == 2
    assert proposal.surface_proposee_m2 == 30


def test_candidate_is_never_used_by_two_proposals() -> None:
    dossier = synthetic_dossier()
    result = reconcile_dossier(dossier)
    check = next(
        item
        for item in result.controles_globaux
        if item.code == "zones_non_reutilisees"
    )
    assert check.statut.value == "ok"


def test_manual_decision_survives_recalculation() -> None:
    dossier = synthetic_dossier()
    proposal = single_proposal(dossier)
    proposal.decision_manuelle = ManualReconciliationDecision(
        statut=VerificationStatus.A_REVOIR,
        numero_retenu="1",
        candidate_zone_ids_retenus=proposal.candidate_zone_ids,
        surface_retenue_m2=proposal.surface_geometrique_m2,
        motif="Contrôle visuel requis.",
    )
    proposal.statut = VerificationStatus.A_REVOIR
    restored = single_proposal(dossier)
    assert restored.statut == VerificationStatus.A_REVOIR
    assert restored.statut_automatique == VerificationStatus.AUTO_VERIFIE
    assert restored.decision_manuelle.motif == "Contrôle visuel requis."


def test_reconciliation_is_deterministic() -> None:
    first = synthetic_dossier()
    second = deepcopy(first)
    result_one = reconcile_dossier(first).model_dump(mode="json")
    result_two = reconcile_dossier(second).model_dump(mode="json")
    assert result_one == result_two


def test_duplicate_canonical_lot_numbers_are_blocking() -> None:
    dossier = synthetic_dossier()
    dossier.lots = [
        Lot(id="lot-a", numero="1"),
        Lot(id="lot-b", numero="1"),
    ]
    result = reconcile_dossier(dossier)
    check = next(
        item
        for item in result.controles_globaux
        if item.code == "numeros_lots_uniques"
    )
    assert check.statut.value == "bloquant"


def test_unknown_layer_remains_unresolved_not_excluded() -> None:
    unknown = candidate("unknown", 0, 10, "45-AUTRE")
    result = reconcile_dossier(
        synthetic_dossier(candidates=[unknown], texts=[])
    )
    assert result.candidate_ids_non_resolus == ["unknown"]
    assert result.candidate_ids_exclus_techniquement == []


def test_custom_rule_profile_is_extensible() -> None:
    rules = ReconciliationRules(
        profile_name="custom",
        principal_layer_patterns=(r"^SURF$",),
        annex_layer_patterns=(),
        excluded_layer_patterns=(),
        lot_label_layer_patterns=(r"^LOT$",),
        surface_label_layer_patterns=(r"^AREA$",),
        plan_title_markers=("PLAN LOTS",),
        automatic_title_markers=("PLAN LOTS",),
    )
    candidates = [
        candidate("c1", -1, 4, "SURF"),
        candidate("c2", 1, 6, "SURF"),
    ]
    texts = [
        text("lot", "2", 0, 0, "LOT"),
        text("area", "S=10m²", 0, 1, "AREA"),
        text("title", "PLAN LOTS", 0, 4, "TITLE"),
    ]
    dossier = synthetic_dossier(candidates=candidates, texts=texts)
    result = reconcile_dossier(dossier, rules)
    assert result.profil_regles == "custom"
    assert result.lot_proposals[0].statut == VerificationStatus.AUTO_VERIFIE


def test_unresolved_proposal_blocks_global_reconciliation_check() -> None:
    dossier = synthetic_dossier(candidates=[])
    result = reconcile_dossier(dossier)
    check = next(
        item
        for item in result.controles_globaux
        if item.code == "reconciliation_resolue"
    )
    assert check.statut.value == "bloquant"
