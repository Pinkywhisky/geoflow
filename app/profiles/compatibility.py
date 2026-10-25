"""Compatibility of saved profiles with a freshly inspected plan.

Facts only: each profile is read on the technical snapshot (no new parsing,
no reconciliation) and its compatibility is one of three levels with the
reasons behind it. A profile is recommended only when it is clearly better
than every other one on those facts; otherwise none is.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from app.domain import ControleTechnique
from app.profiles.evaluation import evaluate_profile
from app.profiles.models import LAYER_ROLES, ProfileRules, rules_hash

LEVELS = {
    "compatible": "Compatible",
    "avertissements": "Compatible avec avertissements",
    "peu": "Peu compatible",
}
_RANK = {"compatible": 0, "avertissements": 1, "peu": 2}
MAX_LISTED_LAYERS = 6


@dataclass
class Compatibility:
    level: str
    reasons: list[dict[str, str]] = field(default_factory=list)
    identified: int = 0
    principal: int = 0
    uncovered_layers: dict[str, int] = field(default_factory=dict)
    # Contours the profile leaves to the automatic engine.
    fallback_zones: int = 0

    @property
    def label(self) -> str:
        return LEVELS[self.level]

    @property
    def uncovered_zones(self) -> int:
        return sum(self.uncovered_layers.values())


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def assess(rules: ProfileRules, control: ControleTechnique, unit_declared: bool) -> Compatibility:
    metrics = evaluate_profile(rules, control, [], unit_declared)["metrics"]
    reasons: list[dict[str, str]] = []

    def add(tone: str, text: str, key: str = "") -> None:
        reasons.append({"tone": tone, "text": text, "key": key})

    if metrics["unit_conflict"]:
        add("warning", f"Le plan déclare l’unité « {control.unite_detectee} », le profil impose « {rules.units.unit} »")
    elif metrics["unit_imposed"]:
        add("info", f"Le plan ne déclare pas d’unité : le profil impose « {rules.units.unit} »")
    else:
        add("ok", "Unité conforme")
    roles = metrics["roles"]
    if roles:
        add("ok", f"{_plural(len(roles), 'rôle métier reconnu', 'rôles métier reconnus')} : "
            + ", ".join(LAYER_ROLES[role].lower() for role in roles))
    principal, identified = metrics["principal"], metrics["identified"]
    if not principal:
        add("warning", "Aucune surface principale reconnue")
    elif not identified:
        add("warning", "Aucune règle fiable pour identifier les lots sur ce plan")
    elif identified == principal:
        add("ok", f"Numéro de lot reconnu sur {_plural(principal, 'zone')} ({_plural(metrics['lots'], 'lot')})")
    else:
        add("warning", f"Numéro de lot reconnu sur {identified} / {principal} zones")
    if metrics["lots"]:
        annotated = metrics["annotated_lots"]
        if annotated == metrics["lots"]:
            add("ok", "Surfaces annotées reconnues pour tous les lots")
        elif annotated:
            add("warning", f"Surfaces annotées reconnues pour {annotated} / {metrics['lots']} lots")
        else:
            add("warning", "Règle de surface non applicable")
    if metrics["lot_conflicts"]:
        add("warning", f"Sources contradictoires ou multiples sur {_plural(metrics['lot_conflicts'], 'zone')}")
    if metrics["level_rule"] and not metrics["levels"]:
        add("warning", "Règle de niveau non applicable")
    uncovered = metrics["uncovered"]
    if uncovered:
        names = list(uncovered)
        shown = ", ".join(names[:MAX_LISTED_LAYERS]) + (" …" if len(names) > MAX_LISTED_LAYERS else "")
        add(
            "warning",
            f"{_plural(len(names), 'calque', 'calques')} avec des contours non couverts par le profil : {shown}",
            "uncovered",
        )

    if metrics["incomplete"] or metrics["unit_conflict"]:
        level = "peu"
    elif any(item["tone"] == "warning" for item in reasons):
        level = "avertissements"
    else:
        level = "compatible"
    return Compatibility(
        level=level,
        reasons=reasons,
        identified=identified,
        principal=principal,
        uncovered_layers=uncovered,
        fallback_zones=sum(uncovered.values()) + principal - identified,
    )


class CompatibilityCache:
    """Assessments per (analysis, rules hash): the snapshot never changes,
    so a profile is read at most once per plan and per revision."""

    def __init__(self, size: int = 512) -> None:
        self.size = size
        self._items: OrderedDict[tuple[str, str, bool], Compatibility] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, analysis_id: str, rules: ProfileRules, control: ControleTechnique, unit_declared: bool) -> Compatibility:
        key = (analysis_id, rules_hash(rules), unit_declared)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        result = assess(rules, control, unit_declared)
        with self._lock:
            self._items[key] = result
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return result


def recommend(assessments: dict[str, Compatibility]) -> str | None:
    """The profile that is clearly preferable, or None.

    Only profiles at the best level that is not « peu compatible » compete;
    one of them is recommended when no other identifies more zones or leaves
    fewer contours uncovered, and it is strictly better than each other one
    on at least one of those two facts. Ties are never broken arbitrarily.
    """

    if not assessments:
        return None
    best = min(_RANK[item.level] for item in assessments.values())
    if best == _RANK["peu"]:
        return None
    contenders = {key: item for key, item in assessments.items() if _RANK[item.level] == best}

    def dominates(first: Compatibility, second: Compatibility) -> bool:
        no_worse = first.identified >= second.identified and first.uncovered_zones <= second.uncovered_zones
        better = first.identified > second.identified or first.uncovered_zones < second.uncovered_zones
        return no_worse and better

    winners = [
        key
        for key, item in contenders.items()
        if all(dominates(item, other) for other_key, other in contenders.items() if other_key != key)
    ]
    return winners[0] if len(winners) == 1 else None
