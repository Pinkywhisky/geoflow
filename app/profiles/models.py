"""Drawing profile: a reading contract for one firm's DWG/DXF conventions.

A profile only stores rules, mappings, priorities and metadata. It never
stores geometry, coordinates or annotation contents of a client drawing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.profiles.patterns import clean_label, validate_layer_pattern, validate_template

# 2 adds the rules revision; version 1 files are read as revision 1.
SCHEMA_VERSION = 2
MAX_PATTERNS = 50

LAYER_ROLES: dict[str, str] = {
    "surface_principale": "Surface principale",
    "annexe": "Annexe",
    "partie_commune": "Partie commune",
    "hors_copropriete": "Hors copropriété",
    "exclusion": "Exclusion",
    "archive": "Archive",
}
# A layer matching several roles takes the most restrictive one.
ROLE_PRECEDENCE = ("archive", "exclusion", "hors_copropriete", "partie_commune", "annexe", "surface_principale")
TEXT_SOURCES: dict[str, str] = {
    "texte_dans_zone": "Texte dans la zone",
    "texte_proche": "Texte proche",
}
UNITS = ("metre", "centimetre", "millimetre", "pied", "pouce")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _patterns(values: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(validate_layer_pattern(item) for item in values))
    if len(cleaned) > MAX_PATTERNS:
        raise ValueError(f"Au plus {MAX_PATTERNS} motifs par liste.")
    return cleaned


class Units(ProfileModel):
    unit: Literal["metre", "centimetre", "millimetre", "pied", "pouce"] = "metre"


class GeometryRules(ProfileModel):
    source: Literal["model", "layouts", "model_layouts"] = "model"
    layouts: list[str] = Field(default_factory=list)
    closed_required: bool = True
    planar_required: bool = True
    reject_self_intersections: bool = True

    _check_layouts = field_validator("layouts")(classmethod(lambda cls, value: _patterns(value)))


class LayerRoles(ProfileModel):
    surface_principale: list[str] = Field(default_factory=list)
    annexe: list[str] = Field(default_factory=list)
    partie_commune: list[str] = Field(default_factory=list)
    hors_copropriete: list[str] = Field(default_factory=list)
    exclusion: list[str] = Field(default_factory=list)
    archive: list[str] = Field(default_factory=list)

    _check = field_validator(*LAYER_ROLES)(classmethod(lambda cls, value: _patterns(value)))


class TextRule(ProfileModel):
    layers: list[str] = Field(default_factory=list)
    # Ordered by priority: the first source giving a value is authoritative.
    sources: list[Literal["texte_dans_zone", "texte_proche"]] = Field(
        default_factory=lambda: ["texte_dans_zone", "texte_proche"]
    )
    radius: float = Field(default=12.0, gt=0, le=1_000_000)

    _check_layers = field_validator("layers")(classmethod(lambda cls, value: _patterns(value)))

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, value: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value))
        if not unique:
            raise ValueError("Au moins une source est nécessaire.")
        return unique


class LotIdentification(TextRule):
    pattern: str = "{numero}"

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        return validate_template(value, "numero")


class SurfaceIdentification(TextRule):
    pattern: str = "{surface}"
    radius: float = Field(default=8.0, gt=0, le=1_000_000)
    tolerance_abs_m2: float = Field(default=0.05, ge=0, le=1000)
    tolerance_rel_pct: float = Field(default=0.1, ge=0, le=10)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, value: str) -> str:
        return validate_template(value, "surface")


class LevelAlias(ProfileModel):
    observed: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=40)

    @field_validator("observed", "value", mode="before")
    @classmethod
    def _clean(cls, value: str) -> str:
        return clean_label(str(value), 40)


class LevelRules(ProfileModel):
    layers: list[str] = Field(default_factory=list)
    radius: float = Field(default=35.0, gt=0, le=1_000_000)
    aliases: list[LevelAlias] = Field(default_factory=list, max_length=MAX_PATTERNS)

    _check_layers = field_validator("layers")(classmethod(lambda cls, value: _patterns(value)))


class PlanRules(ProfileModel):
    title_layers: list[str] = Field(default_factory=list)

    _check_layers = field_validator("title_layers")(classmethod(lambda cls, value: _patterns(value)))


class VersionRules(ProfileModel):
    # Text markers reveal an inactive version but never delimit geometry.
    markers: list[str] = Field(default_factory=list)
    excluded_layouts: list[str] = Field(default_factory=list)

    _check = field_validator("markers", "excluded_layouts")(classmethod(lambda cls, value: _patterns(value)))


class AnnexRules(ProfileModel):
    lot_source: Literal["numero_dans_annexe", "aucune"] = "aucune"
    fallback: Literal["proposer", "rien"] = "proposer"


class Priorities(ProfileModel):
    # Which source wins when an annotated surface and the geometry disagree.
    surface_conflict: Literal["signaler", "annotation", "geometrie"] = "signaler"


class ProfileCheck(ProfileModel):
    status: Literal["complet", "avertissements", "incomplet"]
    checked_at: datetime = Field(default_factory=_now)


class ProfileRules(ProfileModel):
    """What a profile says about drawings: the part a dossier snapshots."""

    units: Units = Field(default_factory=Units)
    geometry: GeometryRules = Field(default_factory=GeometryRules)
    layer_roles: LayerRoles = Field(default_factory=LayerRoles)
    lot_identification: LotIdentification = Field(default_factory=LotIdentification)
    surface_identification: SurfaceIdentification = Field(default_factory=SurfaceIdentification)
    levels: LevelRules = Field(default_factory=LevelRules)
    plans: PlanRules = Field(default_factory=PlanRules)
    versions: VersionRules = Field(default_factory=VersionRules)
    annexes: AnnexRules = Field(default_factory=AnnexRules)
    priorities: Priorities = Field(default_factory=Priorities)

    @model_validator(mode="after")
    def _layouts_need_layout_source(self) -> "ProfileRules":
        if self.geometry.source == "model" and self.geometry.layouts:
            self.geometry = self.geometry.model_copy(update={"layouts": []})
        return self


RULE_FIELDS = tuple(ProfileRules.model_fields)


def rules_hash(rules: ProfileRules) -> str:
    """SHA-256 of the canonical JSON of the rules, metadata excluded."""

    payload = json.dumps(
        rules.model_dump(mode="json", include=set(RULE_FIELDS)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DrawingProfile(ProfileRules):
    schema_version: Literal[2] = SCHEMA_VERSION
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=120)
    # Incremented by the repository each time the saved rules change; a
    # rename alone keeps the revision (the name is metadata).
    revision: int = Field(default=1, ge=1, le=1_000_000)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_check: ProfileCheck | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("schema_version") == 1:
            value = {**value, "schema_version": SCHEMA_VERSION}
        return value

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        return clean_label(str(value), 121)

    def rules(self) -> ProfileRules:
        return ProfileRules.model_validate(self.model_dump(include=set(RULE_FIELDS)))

    @property
    def content_hash(self) -> str:
        return rules_hash(self)
