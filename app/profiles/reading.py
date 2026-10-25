"""Reading of an inspected plan under the rules of a drawing profile.

One implementation serves the profile check, the compatibility shown before
analysis and the reconciliation itself, so that what the user is shown is
exactly what the engine applies. It only reads the technical snapshot.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from app.domain.models import CandidateZone, ControleTechnique, TexteDxf
from app.dxf.technical import layout_planche_id, meters_per_unit
from app.profiles.models import ROLE_PRECEDENCE, ProfileRules
from app.profiles.patterns import layer_matches, normalize_text, surface_value, template_value

MAX_INTERSECTION_VERTICES = 300
# Roles whose contours are business zones; the others are set aside.
ZONE_ROLES = ("surface_principale", "annexe", "partie_commune")


def space_of(region: str, layout_names: dict[str, str]) -> str:
    """'model' for model space (and its sub-regions), else the layout name."""

    return layout_names.get(region, "model") if region.startswith("layout-") else "model"


def level_words(value: str) -> str:
    """Level label as space-padded words: hyphens and case do not matter."""

    return " " + " ".join(normalize_text(value).replace("-", " ").split()) + " "


def _point_in_polygon(x: float, y: float, zone: CandidateZone) -> bool:
    box = zone.bbox
    if box is None or not (box.min_x <= x <= box.max_x and box.min_y <= y <= box.max_y):
        return False
    points = zone.sommets
    if len(points) < 3:
        return True
    inside = False
    previous = points[-1]
    for current in points:
        if (current.y > y) != (previous.y > y):
            crossing = previous.x + (y - previous.y) * (current.x - previous.x) / (current.y - previous.y)
            if x < crossing:
                inside = not inside
        previous = current
    return inside


def _self_intersects(zone: CandidateZone) -> bool | None:
    """Chord-level self-intersection test; None when too large to check."""

    points = [(vertex.x, vertex.y) for vertex in zone.sommets]
    count = len(points)
    if count < 4:
        return False
    if count > MAX_INTERSECTION_VERTICES:
        return None

    def orientation(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    segments = [(points[index], points[(index + 1) % count]) for index in range(count)]
    for i in range(count):
        a, b = segments[i]
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            c, d = segments[j]
            if (
                orientation(a, b, c) * orientation(a, b, d) < 0
                and orientation(c, d, a) * orientation(c, d, b) < 0
            ):
                return True
    return False


def _center(zone: CandidateZone) -> tuple[float, float] | None:
    box = zone.bbox
    return ((box.min_x + box.max_x) / 2, (box.min_y + box.max_y) / 2) if box else None


@dataclass(frozen=True)
class _Point:
    x: float
    y: float
    key: str
    text: TexteDxf | None = None


class _TextIndex:
    """Grid of matching points per drawing space for radius queries."""

    def __init__(self, items: list[tuple[str, _Point, object]], cell: float) -> None:
        self.cell = max(cell, 1e-6)
        self.grid: dict[tuple[str, int, int], list[tuple[_Point, object]]] = defaultdict(list)
        for space, point, value in items:
            self.grid[(space, int(point.x // self.cell), int(point.y // self.cell))].append((point, value))

    def near(self, space: str, x: float, y: float, radius: float):
        reach = int(radius // self.cell) + 1
        cx, cy = int(x // self.cell), int(y // self.cell)
        found = []
        for ix in range(cx - reach, cx + reach + 1):
            for iy in range(cy - reach, cy + reach + 1):
                for point, value in self.grid.get((space, ix, iy), ()):
                    distance = math.hypot(point.x - x, point.y - y)
                    if distance <= radius:
                        found.append((distance, point, value))
        return sorted(found, key=lambda item: (item[0], item[1].key))

    def inside(self, zone: "ZoneReading"):
        box = zone.candidate.bbox
        if box is None:
            return []
        size = max(box.max_x - box.min_x, box.max_y - box.min_y) / 2 + self.cell
        center = _center(zone.candidate)
        return [
            (point, value)
            for _, point, value in self.near(zone.space, center[0], center[1], size * 1.5)
            if _point_in_polygon(point.x, point.y, zone.candidate)
        ]


@dataclass
class ValueRead:
    """Outcome of reading one value through prioritised sources."""

    value: object | None = None
    text: TexteDxf | None = None
    source: str | None = None
    distance: float | None = None
    # First lower-priority source that gave another value.
    conflict_value: object | None = None
    conflict_source: str | None = None
    conflict_text: TexteDxf | None = None
    # Distinct values found inside the zone when that source was skipped.
    ambiguous: tuple[object, ...] = ()


def read_value(zone: "ZoneReading", sources: list[str], index: _TextIndex, radius: float) -> ValueRead:
    """Value from the first source that yields one; never the nearest text
    when a higher-priority source answered."""

    result = ValueRead()
    for source in sources:
        distance = None
        if source == "texte_dans_zone":
            values = {value: point.text for point, value in index.inside(zone)}
            if len(values) > 1:
                if result.value is None:
                    result.ambiguous = tuple(sorted(values, key=str))
                continue
            value, text = next(iter(values.items())) if values else (None, None)
        else:
            center = _center(zone.candidate)
            near = index.near(zone.space, center[0], center[1], radius) if center else []
            value, text, distance = (near[0][2], near[0][1].text, near[0][0]) if near else (None, None, None)
        if value is None:
            continue
        if result.value is None:
            result.value, result.text, result.source, result.distance = value, text, source, distance
        elif value != result.value and result.conflict_value is None:
            result.conflict_value, result.conflict_source, result.conflict_text = value, source, text
    return result


@dataclass
class ZoneReading:
    candidate: CandidateZone
    space: str
    role: str | None
    area_m2: float | None
    lot: ValueRead = field(default_factory=ValueRead)
    surface: ValueRead = field(default_factory=ValueRead)
    level: str | None = None
    level_text: TexteDxf | None = None


@dataclass
class PlanReading:
    rules: ProfileRules
    layout_names: dict[str, str]
    zones: list[ZoneReading]
    # Contours set aside by a geometric rule of the profile, with the rule.
    rejected: dict[str, str]
    unchecked: int
    role_conflicts: dict[str, list[str]]
    by_role: dict[str, list[ZoneReading]]
    lot_items: list[tuple[str, _Point, object]]
    surface_items: list[tuple[str, _Point, object]]
    level_items: list[tuple[str, _Point, object]]
    lot_index: _TextIndex
    marker_texts: list[TexteDxf]
    title_texts: list[TexteDxf]

    def role_of(self, layer: str) -> str | None:
        return role_of(self.rules, layer)[0]


def role_of(rules: ProfileRules, layer: str) -> tuple[str | None, list[str]]:
    """Role of a layer and every role it matches (most restrictive first)."""

    roles = rules.layer_roles
    matched = [role for role in ROLE_PRECEDENCE if any(layer_matches(p, layer) for p in getattr(roles, role))]
    return (matched[0] if matched else None), matched


def read_plan(
    rules: ProfileRules,
    control: ControleTechnique,
    *,
    skip_ids: frozenset[str] = frozenset(),
    use_retained_areas: bool = False,
) -> PlanReading:
    """Apply the profile to the snapshot.

    ``skip_ids`` are contours already set aside by a human decision (planche
    or layer excluded). With ``use_retained_areas`` the areas come from the
    dossier's retained unit instead of the profile's unit.
    """

    layout_names = {layout_planche_id(item.nom): item.nom for item in control.layouts}
    geometry = rules.geometry
    selected_layouts = set(geometry.layouts) or {name for name in layout_names.values() if name.casefold() != "model"}
    excluded_layouts = {
        name for name in layout_names.values() if any(layer_matches(p, name) for p in rules.versions.excluded_layouts)
    }

    def space_allowed(space: str) -> bool:
        if space != "model" and space in excluded_layouts:
            return False
        if geometry.source == "model":
            return space == "model"
        in_layouts = space != "model" and any(layer_matches(p, space) for p in selected_layouts)
        return in_layouts if geometry.source == "layouts" else space == "model" or in_layouts

    factor = meters_per_unit(rules.units.unit)
    role_conflicts = {
        layer.nom: matched for layer in control.calques if len(matched := role_of(rules, layer.nom)[1]) > 1
    }

    zones: list[ZoneReading] = []
    rejected: dict[str, str] = {}
    unchecked = 0
    for candidate in control.zones_candidates:
        if candidate.id in skip_ids:
            continue
        space = space_of(candidate.planche_region, layout_names)
        if not space_allowed(space):
            rejected[candidate.id] = "presentation_exclue" if space in excluded_layouts else "hors_source"
            continue
        role, _ = role_of(rules, candidate.calque)
        if role in ZONE_ROLES and geometry.reject_self_intersections:
            intersects = _self_intersects(candidate)
            if intersects is None:
                unchecked += 1
            elif intersects:
                rejected[candidate.id] = "auto_intersection"
                continue
        if use_retained_areas:
            area = candidate.surface_geometrique_m2
        else:
            area = candidate.surface_geometrique_unites * factor * factor if factor else None
        zones.append(ZoneReading(candidate=candidate, space=space, role=role, area_m2=area))
    by_role: dict[str, list[ZoneReading]] = defaultdict(list)
    for zone in zones:
        if zone.role:
            by_role[zone.role].append(zone)
    archived_layers = {layer.nom for layer in control.calques if role_of(rules, layer.nom)[0] == "archive"}

    def point(text: TexteDxf) -> _Point:
        return _Point(text.x, text.y, text.handle_dxf, text)

    def usable_texts(patterns: list[str]):
        for text in control.textes:
            space = space_of(text.planche_region, layout_names)
            if (
                space_allowed(space)
                and text.calque not in archived_layers
                and (not patterns or any(layer_matches(p, text.calque) for p in patterns))
            ):
                yield space, text

    lot_rule = rules.lot_identification
    lot_items = [
        (space, point(text), value)
        for space, text in usable_texts(lot_rule.layers)
        if (value := template_value(lot_rule.pattern, text.contenu)) is not None
    ]
    lot_index = _TextIndex(lot_items, lot_rule.radius)
    surface_rule = rules.surface_identification
    surface_items = [
        (space, point(text), value)
        for space, text in usable_texts(surface_rule.layers)
        if (value := surface_value(surface_rule.pattern, text.contenu)) is not None
    ]
    surface_index = _TextIndex(surface_items, surface_rule.radius)
    level_rule = rules.levels
    level_items = []
    for space, text in usable_texts(level_rule.layers):
        words = level_words(text.contenu)
        alias = next((item for item in level_rule.aliases if level_words(item.observed) in words), None)
        if alias is not None:
            level_items.append((space, point(text), alias.value))
    level_index = _TextIndex(level_items, level_rule.radius)

    for zone in by_role["surface_principale"]:
        zone.lot = read_value(zone, lot_rule.sources, lot_index, lot_rule.radius)
        zone.surface = read_value(zone, surface_rule.sources, surface_index, surface_rule.radius)
        center = _center(zone.candidate)
        near = level_index.near(zone.space, center[0], center[1], level_rule.radius) if center else []
        if near:
            zone.level, zone.level_text = near[0][2], near[0][1].text

    marker_texts = [
        text for text in control.textes if any(layer_matches(p, text.contenu) for p in rules.versions.markers)
    ]
    title_texts = [text for _, text in usable_texts(rules.plans.title_layers)] if rules.plans.title_layers else []
    return PlanReading(
        rules=rules,
        layout_names=layout_names,
        zones=zones,
        rejected=rejected,
        unchecked=unchecked,
        role_conflicts=role_conflicts,
        by_role=by_role,
        lot_items=lot_items,
        surface_items=surface_items,
        level_items=level_items,
        lot_index=lot_index,
        marker_texts=marker_texts,
        title_texts=title_texts,
    )


def annex_lot_inside(reading: PlanReading, zone: ZoneReading) -> object | None:
    """The single lot number written inside an annex, if any."""

    values = {value for _, value in reading.lot_index.inside(zone)}
    return next(iter(values)) if len(values) == 1 else None
