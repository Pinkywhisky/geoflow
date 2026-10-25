"""Explainable pre-filling of a drawing profile from an inspected plan.

Every suggestion comes with the observations behind it. Nothing here is
persisted: only the profile the user saves is kept, without plan data.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict

from app.domain import CategorieZone, Dossier
from app.dxf.technical import layout_planche_id
from app.profiles.reading import _point_in_polygon, level_words, space_of
from app.profiles.models import UNITS, DrawingProfile, LevelAlias
from app.profiles.patterns import infer_template, layer_matches, normalize_text, template_value
from app.reconciliation.engine import DEFAULT_RULES, _matches, parse_principal_surface
from app.workflow import apply_profile_choice

# Level vocabulary: observed token (normalized) → business value.
LEVEL_VOCABULARY = (
    (r"SOUS SOL(?: \d)?", "Sous-sol"),
    (r"REZ DE CHAUSSEE", "RDC"),
    (r"RDC", "RDC"),
    (r"ENTRESOL", "Entresol"),
    (r"(?:1ER|PREMIER) ETAGE", "R+1"),
    (r"(?:2EME|2E|DEUXIEME) ETAGE", "R+2"),
    (r"(?:3EME|3E|TROISIEME) ETAGE", "R+3"),
    (r"(?:4EME|4E|QUATRIEME) ETAGE", "R+4"),
    (r"(?:5EME|5E|CINQUIEME) ETAGE", "R+5"),
    (r"R\+\d{1,2}", None),
    (r"COMBLES?", "Combles"),
    (r"MEZZANINE", "Mezzanine"),
)
ROLE_KEYWORDS = {
    "annexe": ("ANNEXE", "CAVE", "BALCON", "TERRASSE", "JARDIN", "PARKING", "GARAGE", "BOX", "CELLIER", "GRENIER"),
    "partie_commune": ("COMMUN",),
    "hors_copropriete": ("HORS COPRO", "HORS-COPRO"),
    "archive": ("ARCHIVE", "ANCIEN", "OLD", "POUBELLE"),
}
VERSION_MARKER = re.compile(r"^VERSION \d{1,3} ABANDONNEE$")
LOT_SHAPE = re.compile(r"^(?:LOT\s*(?:N[O°]?\s*)?)?\d{1,4}[A-Z]?$")
SURFACE_SHAPE = re.compile(r"^(?:[A-Z]{1,6}\s*[=:]?\s*)?\d{1,6}(?:[.,]\d{1,3})?\s*M2$")


def _keyword_role(layer: str) -> str | None:
    normalized = normalize_text(layer)
    for role, words in ROLE_KEYWORDS.items():
        if any(word in normalized for word in words):
            return role
    return None


def legacy_view(dossier: Dossier) -> Dossier:
    """The dossier as the automatic engine reads it.

    Inference starts from the automatic classification; when the plan has
    not been analysed that way (profile not chosen yet, or another profile
    used), it is computed in memory from the same snapshot, without reading
    the plan again and without touching the given dossier.
    """

    usage = dossier.profile_usage
    if dossier.reconciliation is not None and (usage is None or usage.mode == "legacy"):
        return dossier
    copy = dossier.model_copy(deep=True)
    apply_profile_choice(copy, None)
    return copy


def infer_profile(dossier: Dossier) -> tuple[DrawingProfile, dict[str, object]]:
    """Draft profile plus the explanations shown while creating it."""

    dossier = legacy_view(dossier)
    control = dossier.controle_technique
    plan = dossier.plan_importe
    reconciliation = dossier.reconciliation
    assert control is not None and plan is not None and reconciliation is not None
    layout_names = {layout_planche_id(item.nom): item.nom for item in control.layouts}
    candidates = {item.id: item for item in control.zones_candidates}
    explanations: dict[str, object] = {"roles": defaultdict(dict)}

    # Units ---------------------------------------------------------------
    unit_guessed = not plan.unite_confirmee
    unit = plan.unite_detectee if plan.unite_detectee in UNITS else None
    unit_reason = "Déclarée dans le fichier."
    if unit is None:
        areas = [item.surface_geometrique_unites for item in control.zones_candidates]
        median = statistics.median(areas) if areas else 0
        if median >= 1e5:
            unit, unit_reason = "millimetre", "Surfaces brutes typiques de l’ordre de 10⁵ et plus : dessin probablement en millimètres."
        elif median >= 1e3:
            unit, unit_reason = "centimetre", "Surfaces brutes typiques de l’ordre de 10³ à 10⁵ : dessin probablement en centimètres."
        else:
            unit, unit_reason = "metre", "Surfaces brutes typiques compatibles avec des mètres carrés."
    explanations["unit"] = {"guessed": unit_guessed, "reason": unit_reason, "declared": plan.unite_detectee}

    # Geometry source -------------------------------------------------------
    business_spaces = Counter(
        space_of(candidates[item.candidate_zone_id].planche_region, layout_names)
        for item in reconciliation.business_zone_candidates
        if item.candidate_zone_id in candidates
    )
    model_count = business_spaces.pop("model", 0)
    layout_counts = dict(business_spaces)
    if model_count >= sum(layout_counts.values()):
        source, layouts = "model", []
    elif model_count == 0:
        source, layouts = "layouts", sorted(layout_counts)
    else:
        source, layouts = "model_layouts", sorted(layout_counts)
    explanations["geometry"] = {"model": model_count, "layouts": layout_counts}

    # Layer roles -------------------------------------------------------------
    roles: dict[str, list[str]] = defaultdict(list)
    legacy = Counter()
    for item in reconciliation.business_zone_candidates:
        candidate = candidates.get(item.candidate_zone_id)
        if candidate is not None:
            legacy[(candidate.calque, item.categorie_proposee)] += 1
    for (layer, category), count in sorted(legacy.items()):
        role = "surface_principale" if category == CategorieZone.PRINCIPALE else "annexe"
        if layer not in roles[role]:
            roles[role].append(layer)
            explanations["roles"][role][layer] = f"{count} contour(s) déjà classés « {category.value.replace('_', ' ')} » par le moteur actuel."
    contour_layers = Counter(item.calque for item in control.zones_candidates)
    for layer in sorted(contour_layers):
        if any(layer in values for values in roles.values()):
            continue
        if _matches(layer, DEFAULT_RULES.excluded_layer_patterns):
            role = "archive" if normalize_text(layer) in {"POUBELLE", "00"} else "exclusion"
            roles[role].append(layer)
            explanations["roles"][role][layer] = "Calque déjà écarté par le profil de calques actuel."
            continue
        role = _keyword_role(layer)
        if role is not None:
            roles[role].append(layer)
            explanations["roles"][role][layer] = "Le nom du calque évoque ce rôle ; aucun contour n’est encore classé ainsi par le moteur."
    if not roles["surface_principale"]:
        assigned = {layer for values in roles.values() for layer in values}
        fallback = _principal_fallback(control, assigned)
        if fallback:
            layer, inside, total = fallback
            roles["surface_principale"].append(layer)
            explanations["roles"]["surface_principale"][layer] = (
                f"{inside} contour(s) fermé(s) sur {total} contiennent un numéro."
            )

    # Annotations -------------------------------------------------------------
    text_layers = sorted({text.calque for text in control.textes})
    lot_layers = [layer for layer in text_layers if _matches(layer, DEFAULT_RULES.lot_label_layer_patterns)]
    lot_reason = "Calque des repères de lots du profil de calques actuel."
    if not lot_layers:
        principal_zones = [
            zone for zone in control.zones_candidates
            if any(layer_matches(pattern, zone.calque) for pattern in roles["surface_principale"])
        ]
        inside = Counter(
            text.calque
            for text in control.textes
            if LOT_SHAPE.match(normalize_text(text.contenu))
            and any(_point_in_polygon(text.x, text.y, zone) for zone in principal_zones)
        )
        if inside:
            layer, count = inside.most_common(1)[0]
            lot_layers = [layer]
            lot_reason = f"{count} numéro(s) de lot de ce calque sont situés dans des surfaces principales."
    surface_layers = [layer for layer in text_layers if _matches(layer, DEFAULT_RULES.surface_label_layer_patterns)]
    surface_reason = "Calque des surfaces annotées du profil de calques actuel."
    if not surface_layers:
        parsable = Counter(
            text.calque
            for text in control.textes
            if parse_principal_surface(text.contenu) is not None or SURFACE_SHAPE.match(normalize_text(text.contenu))
        )
        if parsable:
            layer, count = parsable.most_common(1)[0]
            surface_layers = [layer]
            surface_reason = f"{count} texte(s) de ce calque ont la forme d’une surface."
    lot_texts = [text.contenu for text in control.textes if text.calque in lot_layers]
    lot_template = infer_template(lot_texts, "numero") if lot_texts else None
    surface_texts = [text.contenu for text in control.textes if text.calque in surface_layers]
    surface_template = infer_template(surface_texts, "surface") if surface_texts else None
    explanations["lots"] = {"layers": lot_layers, "reason": lot_reason, "matched": lot_template[1] if lot_template else 0}
    explanations["surfaces"] = {"layers": surface_layers, "reason": surface_reason, "matched": surface_template[1] if surface_template else 0}

    title_layers = sorted(
        {text.calque for text in control.textes if any(marker in normalize_text(text.contenu) for marker in DEFAULT_RULES.plan_title_markers)}
    )

    # Levels ------------------------------------------------------------------
    observed: Counter[str] = Counter()
    level_layers: Counter[str] = Counter()
    for text in control.textes:
        words = level_words(text.contenu)
        for pattern, _ in LEVEL_VOCABULARY:
            match = re.search(rf"(?<=\s){pattern}(?=\s)", words)
            if match:
                observed[match.group(0)] += 1
                level_layers[text.calque] += 1
                break
    aliases = []
    for token, _ in observed.most_common(20):
        value = next(
            (value for pattern, value in LEVEL_VOCABULARY if re.fullmatch(pattern, token)),
            None,
        ) or token
        aliases.append(LevelAlias(observed=token, value=value))
    explanations["levels"] = {"observed": dict(observed)}

    # Versions ----------------------------------------------------------------
    markers = sorted(
        {
            "VERSION * ABANDONNEE"
            for text in control.textes
            if VERSION_MARKER.match(normalize_text(text.contenu))
        }
    )

    # Annexes: numbers written inside annexes make the link explicit -----------
    annex_layers = roles.get("annexe", [])
    annex_zones = [item for item in control.zones_candidates if item.calque in annex_layers]
    numbered = 0
    if lot_template and annex_zones:
        lot_points = [text for text in control.textes if text.calque in lot_layers and template_value(lot_template[0], text.contenu)]
        numbered = sum(
            any(_point_in_polygon(text.x, text.y, zone) for text in lot_points) for zone in annex_zones
        )
    annex_source = "numero_dans_annexe" if annex_zones and numbered >= len(annex_zones) / 2 else "aucune"
    explanations["annexes"] = {"total": len(annex_zones), "numbered": numbered}

    profile = DrawingProfile(
        # Never derived from the file name: a profile must not carry plan data.
        name="Nouveau profil de dessin",
        units={"unit": unit},
        geometry={"source": source, "layouts": layouts},
        layer_roles=dict(roles),
        lot_identification={
            "layers": lot_layers,
            "pattern": lot_template[0] if lot_template else "{numero}",
            "radius": DEFAULT_RULES.lot_assignment_radius,
        },
        surface_identification={
            "layers": surface_layers,
            "pattern": surface_template[0] if surface_template else "{surface}",
            "radius": DEFAULT_RULES.surface_assignment_radius,
            "tolerance_abs_m2": DEFAULT_RULES.tolerance.absolute_m2,
            "tolerance_rel_pct": DEFAULT_RULES.tolerance.relative * 100,
        },
        levels={"layers": [layer for layer, _ in level_layers.most_common(3)], "aliases": aliases, "radius": DEFAULT_RULES.title_assignment_radius},
        plans={"title_layers": title_layers},
        versions={"markers": markers},
        annexes={"lot_source": annex_source, "fallback": "proposer"},
    )
    return _order_sources(profile, dossier), explanations


def _order_sources(profile: DrawingProfile, dossier: Dossier) -> DrawingProfile:
    """Put first the text source that identifies the most principal zones."""

    from app.profiles.evaluation import source_coverage

    coverage = source_coverage(profile, dossier.controle_technique)
    lots = sorted(profile.lot_identification.sources, key=lambda item: -coverage["lot"][item])
    surfaces = sorted(profile.surface_identification.sources, key=lambda item: -coverage["surface"][item])
    return profile.model_copy(
        update={
            "lot_identification": profile.lot_identification.model_copy(update={"sources": lots}),
            "surface_identification": profile.surface_identification.model_copy(update={"sources": surfaces}),
        }
    )


def _principal_fallback(control, assigned: set[str]) -> tuple[str, int, int] | None:
    """Layer whose closed contours most often contain a lot-like number,
    among layers not already given another role."""

    numbers = [text for text in control.textes if re.fullmatch(r"(?:LOT\s*)?\d{1,4}", normalize_text(text.contenu))]
    best = None
    by_layer = defaultdict(list)
    for zone in control.zones_candidates:
        by_layer[zone.calque].append(zone)
    for layer, zones in by_layer.items():
        if len(zones) < 3 or layer in assigned:
            continue
        inside = sum(any(_point_in_polygon(text.x, text.y, zone) for text in numbers) for zone in zones)
        if inside >= len(zones) / 2 and (best is None or inside > best[1]):
            best = (layer, inside, len(zones))
    return best


def unmapped_layer_suggestions(profile: DrawingProfile, dossier: Dossier) -> list[dict[str, str]]:
    """Layers of a new plan that resemble a role but are not in the profile."""

    dossier = legacy_view(dossier)
    control = dossier.controle_technique
    reconciliation = dossier.reconciliation
    configured = [
        pattern
        for role in ("surface_principale", "annexe", "partie_commune", "hors_copropriete", "exclusion", "archive")
        for pattern in getattr(profile.layer_roles, role)
    ]
    counts = Counter(item.calque for item in control.zones_candidates)
    legacy = {}
    candidates = {item.id: item for item in control.zones_candidates}
    for item in reconciliation.business_zone_candidates if reconciliation else []:
        candidate = candidates.get(item.candidate_zone_id)
        if candidate is not None:
            legacy[candidate.calque] = item.categorie_proposee
    suggestions = []
    for layer, count in sorted(counts.items()):
        if any(layer_matches(pattern, layer) for pattern in configured):
            continue
        if layer in legacy:
            role = "surface_principale" if legacy[layer] == CategorieZone.PRINCIPALE else "annexe"
            reason = "le moteur actuel classe ses contours dans cette catégorie"
        elif (role := _keyword_role(layer)) is not None:
            reason = "son nom évoque ce rôle"
        else:
            continue
        suggestions.append({"layer": layer, "role": role, "count": count, "reason": reason})
    return suggestions
