"""Lightweight check of a drawing profile against an inspected plan.

The check reuses the technical snapshot (contours, texts, layouts): it never
reads the drawing again and never runs the full reconciliation, so it can be
replayed on every edit of the profile. It reports facts, not scores.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.domain import ControleTechnique, Planche
from app.dxf.technical import meters_per_unit
from app.profiles.models import LAYER_ROLES, ProfileRules
from app.profiles.reading import (
    ZoneReading,
    _center,
    _Point,
    _TextIndex,
    annex_lot_inside,
    level_words,
    read_plan,
    space_of,
)

__all__ = ["STATUS_LABELS", "evaluate_profile", "level_words", "source_coverage", "space_of"]

STATUS_LABELS = {
    "complet": "Complet",
    "avertissements": "Utilisable avec avertissements",
    "incomplet": "Incomplet",
}


@dataclass
class Facts:
    """Section of the report: titled lines tagged ok / warning / info / error."""

    title: str
    lines: list[dict[str, str]] = field(default_factory=list)

    def add(self, tone: str, text: str) -> None:
        self.lines.append({"tone": tone, "text": text})


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def evaluate_profile(
    profile: ProfileRules,
    control: ControleTechnique,
    planches: list[Planche],
    unit_declared: bool,
) -> dict[str, object]:
    reading = read_plan(profile, control)
    geometry = profile.geometry
    factor = meters_per_unit(profile.units.unit)
    roles = profile.layer_roles
    role_conflicts = reading.role_conflicts
    by_role = reading.by_role
    rejected = list(reading.rejected.values())
    outside_source = rejected.count("hors_source")
    excluded_by_layout = rejected.count("presentation_exclue")
    invalid = rejected.count("auto_intersection")
    unchecked = reading.unchecked
    lot_rule = profile.lot_identification
    surface_rule = profile.surface_identification
    level_rule = profile.levels
    lot_items, surface_items, level_items = reading.lot_items, reading.surface_items, reading.level_items

    principal = by_role["surface_principale"]
    lot_conflicts = sum(zone.lot.conflict_value is not None for zone in principal)
    lot_ambiguous = sum(bool(zone.lot.ambiguous) for zone in principal)

    lots: dict[str, list[ZoneReading]] = defaultdict(list)
    for zone in principal:
        if zone.lot.value is not None:
            lots[zone.lot.value].append(zone)
    coherent = incoherent = without_annotation = 0
    for members in lots.values():
        annotations = {zone.surface.text.handle_dxf: zone.surface.value for zone in members if zone.surface.text is not None}
        if not annotations:
            without_annotation += 1
            continue
        annotated = sum(annotations.values())
        geometric = sum(zone.area_m2 or 0 for zone in members)
        allowed = max(surface_rule.tolerance_abs_m2, surface_rule.tolerance_rel_pct / 100 * max(annotated, geometric))
        if abs(geometric - annotated) <= allowed:
            coherent += 1
        else:
            incoherent += 1

    annexes = by_role["annexe"]
    principal_index = _TextIndex(
        [
            (zone.space, _Point(*_center(zone.candidate), zone.candidate.id), zone.lot.value)
            for zone in principal
            if zone.lot.value is not None and zone.candidate.bbox is not None
        ],
        lot_rule.radius,
    )
    annex_explicit = annex_proposable = annex_ambiguous = annex_none = 0
    for zone in annexes:
        if profile.annexes.lot_source == "numero_dans_annexe" and annex_lot_inside(reading, zone) is not None:
            annex_explicit += 1
            continue
        if profile.annexes.fallback == "rien":
            annex_none += 1
            continue
        center = _center(zone.candidate)
        numbers = {value for _, _, value in principal_index.near(zone.space, center[0], center[1], lot_rule.radius)} if center else set()
        if len(numbers) == 1:
            annex_proposable += 1
        elif numbers:
            annex_ambiguous += 1
        else:
            annex_none += 1

    marker_texts = reading.marker_texts
    title_texts = reading.title_texts

    # ------------------------------------------------------------------ facts
    incomplete: list[str] = []
    warnings: list[str] = []
    drawing = Facts("Dessin")
    declared = control.unite_detectee
    if factor is None:
        drawing.add("error", "Unité du profil non prise en charge.")
        incomplete.append("unité")
    elif not unit_declared:
        drawing.add("info", "Le plan ne déclare pas son unité : le profil l’impose (" + profile.units.unit + ").")
    elif declared != profile.units.unit:
        drawing.add("warning", f"Le plan déclare « {declared} », le profil impose « {profile.units.unit} ».")
        warnings.append("unité")
    else:
        drawing.add("ok", "Unité du profil identique à celle déclarée par le plan.")
    source_label = {"model": "espace objet (Model)", "layouts": "présentations", "model_layouts": "Model et présentations"}
    drawing.add("ok", f"Source métier : {source_label[geometry.source]}.")
    if outside_source:
        drawing.add("info", f"{_plural(outside_source, 'contour')} hors de la source métier, ignoré(s).")
    if excluded_by_layout:
        drawing.add("info", f"{_plural(excluded_by_layout, 'contour')} sur une présentation exclue.")
    if invalid:
        drawing.add("warning", f"{_plural(invalid, 'contour auto-intersecté', 'contours auto-intersectés')} refusé(s).")
        warnings.append("géométrie")
    if unchecked:
        drawing.add("info", f"{_plural(unchecked, 'contour')} trop détaillé(s) pour le contrôle d’auto-intersection.")

    if title_texts:
        drawing.add("info", f"{_plural(len(title_texts), 'titre de planche reconnu', 'titres de planche reconnus')}.")

    role_facts: dict[str, list[dict[str, str]]] = {}
    for role, label in LAYER_ROLES.items():
        facts = Facts(label)
        members = by_role[role]
        if not getattr(roles, role):
            facts.add("info", "Aucun calque associé.")
        elif not members:
            facts.add("warning" if role == "surface_principale" else "info", "Aucun contour fermé reconnu dans ce plan.")
        else:
            facts.add("ok", f"{_plural(len(members), 'contour fermé reconnu', 'contours fermés reconnus')}")
        role_facts[role] = facts.lines
    zone_facts = Facts("Zones métier")
    for role, label in LAYER_ROLES.items():
        if by_role[role]:
            zone_facts.add("ok", f"{label} : {_plural(len(by_role[role]), 'contour')}")
    for layer, matched in sorted(role_conflicts.items()):
        warnings.append("rôles")
        conflict = {
            "tone": "warning",
            "text": f"« {layer} » correspond à plusieurs rôles ; « {LAYER_ROLES[matched[0]]} » est retenu.",
        }
        role_facts.setdefault(matched[0], []).append(conflict)
        zone_facts.lines.append(conflict)

    lots_facts = Facts("Surfaces principales")
    if not roles.surface_principale:
        lots_facts.add("error", "Aucun calque de surface principale.")
        incomplete.append("surface principale")
    elif not principal:
        lots_facts.add("error", "Aucune surface principale reconnue dans ce plan.")
        incomplete.append("surface principale")
    else:
        identified = sum(zone.lot.value is not None for zone in principal)
        lots_facts.add("ok", f"{_plural(len(principal), 'zone reconnue', 'zones reconnues')}")
        tone = "ok" if identified == len(principal) else "warning"
        lots_facts.add(tone, f"{identified} / {len(principal)} avec un numéro de lot · {_plural(len(lots), 'lot')}")
        if identified == 0:
            incomplete.append("numéro de lot")
        elif identified < len(principal):
            warnings.append("numéros de lot")
        if lot_ambiguous:
            lots_facts.add("warning", f"{_plural(lot_ambiguous, 'zone')} avec plusieurs numéros dans la zone.")
            warnings.append("numéros ambigus")
        if lot_conflicts:
            lots_facts.add(
                "warning",
                f"{_plural(lot_conflicts, 'zone')} où les sources se contredisent : la source prioritaire est retenue.",
            )
            warnings.append("sources contradictoires")

    surface_facts = Facts("Surfaces annotées")
    if lots:
        annotated_lots = len(lots) - without_annotation
        surface_facts.add("ok" if annotated_lots else "warning", f"{annotated_lots} / {len(lots)} lots avec une surface annotée")
        if without_annotation:
            surface_facts.add("warning", f"{_plural(without_annotation, 'lot')} sans surface annotée.")
            warnings.append("surfaces annotées")
        if coherent:
            surface_facts.add("ok", f"{coherent} cohérente(s) avec la géométrie dans la tolérance.")
        if incoherent:
            policy = {
                "signaler": "à vérifier",
                "annotation": "l’annotation fera foi",
                "geometrie": "la géométrie fera foi",
            }[profile.priorities.surface_conflict]
            surface_facts.add("warning", f"{incoherent} écart(s) hors tolérance : {policy}.")
            warnings.append("écarts de surface")
    else:
        surface_facts.add("info", "Aucun lot identifié : surfaces non évaluées.")

    level_facts = Facts("Niveaux")
    if not level_rule.aliases:
        level_facts.add("warning", "Aucune valeur de niveau définie : GeoFlow ne saura pas les reconnaître.")
        warnings.append("niveaux")
    else:
        recognized = sorted({value for _, _, value in level_items})
        level_facts.add("ok" if recognized else "warning", f"{_plural(len(recognized), 'niveau reconnu', 'niveaux reconnus')}" + (f" : {', '.join(recognized)}" if recognized else ""))
        if principal:
            with_level = sum(zone.level is not None for zone in principal)
            tone = "ok" if with_level == len(principal) else "warning"
            level_facts.add(tone, f"{with_level} / {len(principal)} surfaces principales rattachées à un niveau")
            if with_level < len(principal):
                warnings.append("niveaux")

    annex_facts = Facts("Annexes")
    if annexes:
        annex_facts.add("ok", f"{_plural(len(annexes), 'zone reconnue', 'zones reconnues')}")
        if annex_explicit:
            annex_facts.add("ok", f"{annex_explicit} avec un numéro de lot inscrit")
        if annex_proposable:
            annex_facts.add("info", f"{annex_proposable} avec un lot proposable (repère unique à proximité), sans affectation automatique")
        if annex_ambiguous:
            annex_facts.add("warning", f"{annex_ambiguous} avec plusieurs lots possibles")
        if annex_none:
            annex_facts.add("warning", f"{annex_none} sans proposition de lot")
        if annex_proposable or annex_ambiguous or annex_none:
            warnings.append("annexes")
    else:
        annex_facts.add("info", "Aucune annexe reconnue.")

    version_facts = Facts("Versions")
    archived = len(by_role["archive"])
    if roles.archive:
        version_facts.add("ok", f"Calques d’archive : {_plural(archived, 'contour écarté', 'contours écartés')}.")
    if profile.versions.markers:
        version_facts.add("info", f"{_plural(len(marker_texts), 'mention détectée', 'mentions détectées')}.")
        if marker_texts:
            version_facts.add(
                "warning",
                "Une mention de version permet de la détecter, mais pas de déterminer de manière certaine quelles géométries exclure.",
            )
            warnings.append("versions")
    if not roles.archive and not profile.versions.markers and not profile.versions.excluded_layouts:
        version_facts.add("info", "Aucune règle de version : toutes les géométries de la source sont actives.")

    if incomplete:
        status = "incomplet"
        conclusion = "Ce profil est incomplet : " + ", ".join(dict.fromkeys(incomplete)) + " à définir."
    elif warnings:
        status = "avertissements"
        count = len(dict.fromkeys(warnings))
        conclusion = (
            f"Ce profil comporte encore {_plural(count, 'règle ambiguë', 'règles ambiguës')} sur ce plan "
            f"({', '.join(dict.fromkeys(warnings))}). GeoFlow utilisera le mode legacy pour ces éléments."
        )
    else:
        status = "complet"
        conclusion = "Ce profil permet une lecture déterministe de ce plan."

    lot_examples = _examples(lot_items)
    surface_examples = _examples(surface_items)
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "conclusion": conclusion,
        "drawing": drawing.lines,
        "roles": role_facts,
        "role_ids": {role: [zone.candidate.id for zone in by_role[role]] for role in LAYER_ROLES},
        "sections": [drawing, zone_facts, lots_facts, surface_facts, level_facts, annex_facts, version_facts],
        "lots": lots_facts.lines,
        "surfaces": surface_facts.lines,
        "levels": level_facts.lines,
        "annexes": annex_facts.lines,
        "versions": version_facts.lines,
        "identified": sum(zone.lot.value is not None for zone in principal),
        "lot_values": [zone.lot.value for zone in principal],
        "annotated_zones": sum(zone.surface.value is not None for zone in principal),
        "lot_examples": lot_examples,
        "surface_examples": surface_examples,
        "metrics": {
            "unit_conflict": factor is not None and unit_declared and control.unite_detectee != profile.units.unit,
            "unit_imposed": factor is not None and not unit_declared,
            "principal": len(principal),
            "identified": sum(zone.lot.value is not None for zone in principal),
            "lots": len(lots),
            "annotated_lots": len(lots) - without_annotation,
            "lot_conflicts": lot_conflicts + lot_ambiguous,
            "roles": sorted(role for role in LAYER_ROLES if by_role[role]),
            "level_rule": bool(level_rule.aliases),
            "levels": len({value for _, _, value in level_items}),
            "uncovered": dict(sorted(Counter(zone.candidate.calque for zone in reading.zones if zone.role is None).items())),
            "incomplete": list(dict.fromkeys(incomplete)),
        },
    }


def _examples(items: list[tuple[str, "_Point", object]], limit: int = 4) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for _, point, value in items:
        shown = " ".join(point.text.contenu.split())[:40]
        if shown not in seen:
            seen[shown] = f"{value:.2f}".replace(".", ",") if isinstance(value, float) else str(value)
        if len(seen) >= limit:
            break
    return list(seen.items())


def source_coverage(profile: ProfileRules, control: ControleTechnique) -> dict[str, dict[str, int]]:
    """Principal zones identified by each text source used alone."""

    coverage: dict[str, dict[str, int]] = {"lot": {}, "surface": {}}
    for source in ("texte_dans_zone", "texte_proche"):
        single = profile.model_copy(
            update={
                "lot_identification": profile.lot_identification.model_copy(update={"sources": [source]}),
                "surface_identification": profile.surface_identification.model_copy(update={"sources": [source]}),
                "levels": profile.levels.model_copy(update={"aliases": []}),
            }
        )
        report = evaluate_profile(single, control, [], unit_declared=True)
        coverage["lot"][source] = report["identified"]
        coverage["surface"][source] = report["annotated_zones"]
    return coverage
