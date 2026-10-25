"""Build a drawing profile from the wizard form, field order kept as priority."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from app.profiles.models import LAYER_ROLES, DrawingProfile

FIELD_LABELS = {
    "name": "Nom du profil",
    "units": "Unité",
    "geometry": "Source géométrique",
    "layer_roles": "Calques",
    "lot_identification": "Numéro de lot",
    "surface_identification": "Surface annotée",
    "levels": "Niveaux",
    "versions": "Versions",
    "annexes": "Annexes",
    "priorities": "Priorités",
}


class ProfileFormError(ValueError):
    def __init__(self, messages: list[str]) -> None:
        super().__init__(" ; ".join(messages))
        self.messages = messages


def _values(form, name: str) -> list[str]:
    values = form.getlist(name) if hasattr(form, "getlist") else form.get(name, [])
    return [str(item).strip() for item in values if str(item).strip()][:200]


def _text(form, name: str, default: str = "") -> str:
    value = form.get(name, default)
    return str(value).strip() if value is not None else default


def _number(form, name: str, default: float) -> float | str:
    raw = _text(form, name)
    if not raw:
        return default
    try:
        return float(raw.replace(",", ".").replace(" ", ""))
    except ValueError:
        return raw  # left to the model, which reports a readable error


def profile_from_form(form: Mapping, base: DrawingProfile | None = None) -> DrawingProfile:
    """Validated profile from the wizard; identity fields come from ``base``."""

    observed = _values(form, "level_observed") if hasattr(form, "getlist") else []
    values = form.getlist("level_value") if hasattr(form, "getlist") else []
    aliases = [
        {"observed": item, "value": str(values[index]).strip() or item}
        for index, item in enumerate(observed)
        if index < len(values)
    ]
    data = {
        "name": _text(form, "name") or (base.name if base else "Nouveau profil de dessin"),
        "units": {"unit": _text(form, "unit", "metre")},
        "geometry": {
            "source": _text(form, "geometry_source", "model"),
            "layouts": _values(form, "geometry_layouts"),
            "closed_required": bool(form.get("rule_closed")),
            "planar_required": bool(form.get("rule_planar")),
            "reject_self_intersections": bool(form.get("rule_self_intersection")),
        },
        "layer_roles": {role: _values(form, f"role_{role}") for role in LAYER_ROLES},
        "lot_identification": {
            "layers": _values(form, "lot_layers"),
            "sources": _values(form, "lot_sources"),
            "pattern": _text(form, "lot_pattern", "{numero}"),
            "radius": _number(form, "lot_radius", 12.0),
        },
        "surface_identification": {
            "layers": _values(form, "surface_layers"),
            "sources": _values(form, "surface_sources"),
            "pattern": _text(form, "surface_pattern", "{surface}"),
            "radius": _number(form, "surface_radius", 8.0),
            "tolerance_abs_m2": _number(form, "tolerance_abs", 0.05),
            "tolerance_rel_pct": _number(form, "tolerance_rel", 0.1),
        },
        "levels": {
            "layers": _values(form, "level_layers"),
            "radius": _number(form, "level_radius", 35.0),
            "aliases": aliases,
        },
        "plans": {"title_layers": _values(form, "title_layers")},
        "versions": {
            "markers": _values(form, "version_markers"),
            "excluded_layouts": _values(form, "excluded_layouts"),
        },
        "annexes": {
            "lot_source": _text(form, "annex_lot_source", "aucune"),
            "fallback": _text(form, "annex_fallback", "proposer"),
        },
        "priorities": {"surface_conflict": _text(form, "surface_conflict", "signaler")},
    }
    if base is not None:
        data.update(
            {"id": base.id, "created_at": base.created_at, "revision": base.revision, "last_check": base.last_check}
        )
    try:
        return DrawingProfile.model_validate(data)
    except ValidationError as exc:
        messages = []
        for error in exc.errors():
            section = FIELD_LABELS.get(str(error["loc"][0]), str(error["loc"][0])) if error["loc"] else "Profil"
            message = str(error.get("ctx", {}).get("error", error["msg"]))
            messages.append(f"{section} : {message.removeprefix('Value error, ')}")
        raise ProfileFormError(list(dict.fromkeys(messages))) from exc
