"""Serializable canonical data model for the GeoFlow workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BBox(CanonicalModel):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class Sommet(CanonicalModel):
    x: float
    y: float
    bulge: float = 0.0


class Provenance(CanonicalModel):
    fichier_source: str
    handle_dxf: str | None = None
    calque: str | None = None
    type_entite: str | None = None
    planche_region: str | None = None
    methode_detection: str


class GeometrieSource(CanonicalModel):
    fichier_source: str
    handle_dxf: str
    calque: str
    type_entite: str
    planche_region: str
    sommets: list[Sommet] = Field(default_factory=list)
    provenance: Provenance


class DecisionValidation(CanonicalModel):
    champ: str
    propose: Any
    retenu: Any
    justification: str | None = None
    horodatage: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def correction_requires_justification(self) -> "DecisionValidation":
        if self.propose != self.retenu and not (self.justification or "").strip():
            raise ValueError("Une correction exige une justification.")
        return self


class PlanImporte(CanonicalModel):
    nom_fichier_original: str
    type_fichier: Literal["dxf", "dwg"]
    version_dxf: str
    unite_detectee: str
    unite_retenue: str | None = None
    unite_confirmee: bool = False
    bbox: BBox | None = None
    date_import: datetime = Field(default_factory=utc_now)


class StatutRevue(str, Enum):
    CANDIDATE = "candidate"
    RETENUE = "retenue"
    EXCLUE = "exclue"
    ABANDONNEE = "abandonnee"


class Planche(CanonicalModel):
    id: str
    titre: str
    bbox_region: BBox | None = None
    statut: StatutRevue = StatutRevue.CANDIDATE
    methode_detection: str


class LayerInfo(CanonicalModel):
    nom: str
    visible: bool
    gele: bool
    trace: bool
    statut: StatutRevue = StatutRevue.CANDIDATE
    exclusion_suggeree: bool = False


class LayoutInfo(CanonicalModel):
    nom: str
    bbox: BBox | None = None
    nombre_entites: int = 0


class TexteDxf(CanonicalModel):
    contenu: str
    x: float
    y: float
    calque: str
    handle_dxf: str
    planche_region: str


class CandidateZone(CanonicalModel):
    id: str
    type_entite: str
    calque: str
    handle_dxf: str
    planche_region: str
    surface_geometrique_unites: float
    surface_geometrique_m2: float | None = None
    bbox: BBox | None = None
    sommets: list[Sommet] = Field(default_factory=list)
    textes_proches: list[str] = Field(default_factory=list)
    statut: StatutRevue = StatutRevue.CANDIDATE
    provenance: Provenance


class ControleTechnique(CanonicalModel):
    version_dxf: str
    unite_detectee: str
    bbox: BBox | None = None
    calques: list[LayerInfo] = Field(default_factory=list)
    layouts: list[LayoutInfo] = Field(default_factory=list)
    types_entites: dict[str, int] = Field(default_factory=dict)
    textes: list[TexteDxf] = Field(default_factory=list)
    polylignes_fermees: int = 0
    zones_candidates: list[CandidateZone] = Field(default_factory=list)
    avertissements: list[str] = Field(default_factory=list)

    @property
    def nombre_entites(self) -> int:
        return sum(self.types_entites.values())


class Batiment(CanonicalModel):
    id: str
    code: str
    libelle: str | None = None


class Niveau(CanonicalModel):
    id: str
    batiment_id: str
    code: str
    libelle: str | None = None


class Lot(CanonicalModel):
    id: str
    numero: str
    batiment_ids: list[str] = Field(default_factory=list)
    niveau_ids: list[str] = Field(default_factory=list)
    usage: str | None = None
    designation: str | None = None
    zone_ids: list[str] = Field(default_factory=list)


class CategorieZone(str, Enum):
    PRINCIPALE = "principale"
    SECONDAIRE_ANNEXE = "secondaire_annexe"
    EXCLUE = "exclue"
    COMMUNE = "commune"
    AUTRE = "autre"


class Zone(CanonicalModel):
    id: str
    batiment_id: str
    niveau_id: str
    lot_id: str | None = None
    categorie: CategorieZone
    surface_geometrique_m2: float = Field(ge=0)
    surface_retenue_m2: float = Field(ge=0)
    statut: StatutRevue = StatutRevue.RETENUE
    geometrie_source: GeometrieSource
    decision_surface: DecisionValidation
    provenance_association: Provenance

    @model_validator(mode="after")
    def retained_surface_matches_decision(self) -> "Zone":
        decision = self.decision_surface
        if decision.champ != "surface_retenue_m2":
            raise ValueError("La decision de surface cible un champ inattendu.")
        if float(decision.propose) != self.surface_geometrique_m2:
            raise ValueError("La surface proposee doit conserver la surface geometrique.")
        if float(decision.retenu) != self.surface_retenue_m2:
            raise ValueError("La surface retenue doit correspondre a la decision.")
        return self


class StatutValidationJuridique(str, Enum):
    A_CONFIRMER = "a_confirmer"
    CONFIRME = "confirme"


class StatutValidationDonnees(str, Enum):
    BROUILLON = "brouillon"
    A_VALIDER = "a_valider"
    VALIDE = "valide"


class DroitParticulier(CanonicalModel):
    id: str
    description: str
    lot_ids: list[str] = Field(default_factory=list)
    statut_validation: StatutValidationJuridique = (
        StatutValidationJuridique.A_CONFIRMER
    )


class Servitude(CanonicalModel):
    id: str
    description: str
    statut_validation: StatutValidationJuridique = (
        StatutValidationJuridique.A_CONFIRMER
    )


class Millieme(CanonicalModel):
    lot_id: str
    valeur: int = Field(ge=0)
    base: int | None = Field(default=None, gt=0)
    valide: bool = False


class Generation(CanonicalModel):
    id: str
    type_document: Literal["etat_descriptif_copropriete"]
    template_id: str
    template_version: str
    date_generation: datetime = Field(default_factory=utc_now)
    sha256_snapshot: str = Field(pattern=r"^[a-f0-9]{64}$")
    statut: Literal["brouillon", "valide"] = "brouillon"
    nom_fichier: str
    snapshot_filename: str = "dossier_snapshot.json"
    avertissements: list[str] = Field(default_factory=list)


class Dossier(CanonicalModel):
    schema_version: Literal["1.0", "1.1"] = "1.1"
    id: str
    reference: str
    type: Literal["copropriete"] = "copropriete"
    statut: str = "nouveau"
    statut_validation_donnees: StatutValidationDonnees = (
        StatutValidationDonnees.BROUILLON
    )
    date_validation_donnees: datetime | None = None
    sha256_validation_donnees: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    adresse: str | None = None
    commune: str | None = None
    departement: str | None = None
    references_cadastrales: list[str] = Field(default_factory=list)
    date_plan: str | None = None
    plan_importe: PlanImporte | None = None
    controle_technique: ControleTechnique | None = None
    planches: list[Planche] = Field(default_factory=list)
    batiments: list[Batiment] = Field(default_factory=list)
    niveaux: list[Niveau] = Field(default_factory=list)
    lots: list[Lot] = Field(default_factory=list)
    zones: list[Zone] = Field(default_factory=list)
    validations: list[DecisionValidation] = Field(default_factory=list)
    droits_particuliers: list[DroitParticulier] = Field(default_factory=list)
    servitudes: list[Servitude] = Field(default_factory=list)
    milliemes: list[Millieme] = Field(default_factory=list)
    generations: list[Generation] = Field(default_factory=list)
