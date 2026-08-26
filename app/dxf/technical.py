"""Technical DXF inventory used by the guided copropriete workflow."""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Iterable

import ezdxf
from ezdxf import bbox as ezdxf_bbox
from ezdxf.lldxf.const import DXFError

from app.domain import (
    BBox,
    CandidateZone,
    ControleTechnique,
    LayerInfo,
    Planche,
    Provenance,
    Sommet,
    TexteDxf,
)
from app.domain.models import LayoutInfo, StatutRevue

from .analyzer import DxfAnalysisError, _polyline_area, _vertices


logger = logging.getLogger(__name__)
PRIORITY_LAYERS = {"80", "81", "82", "83"}
MAX_TEXT_LENGTH = 500

_UNIT_BY_CODE: dict[int, tuple[str, float | None]] = {
    0: ("sans_unite", None),
    1: ("pouce", 0.0254),
    2: ("pied", 0.3048),
    4: ("millimetre", 0.001),
    5: ("centimetre", 0.01),
    6: ("metre", 1.0),
    7: ("kilometre", 1000.0),
    10: ("yard", 0.9144),
    14: ("decimetre", 0.1),
    21: ("pied_us", 1200.0 / 3937.0),
}
_METERS_BY_UNIT = {
    name: factor for name, factor in _UNIT_BY_CODE.values() if factor is not None
}


def meters_per_unit(unit: str) -> float | None:
    return _METERS_BY_UNIT.get(unit)


def safe_dxf_string(value: object, limit: int = MAX_TEXT_LENGTH) -> str:
    text = str(value or "")
    cleaned = "".join(
        character
        if (ord(character) >= 32 and ord(character) != 127)
        else " "
        for character in text
    )
    return " ".join(cleaned.split())[:limit]


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub("[^a-zA-Z0-9_-]+", "-", ascii_value).strip("-").lower()
    return slug[:60] or "layout"


def _normalized(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return normalized.encode("ascii", "ignore").decode("ascii").upper()


def _bbox_from_entities(entities: Iterable[object]) -> BBox | None:
    try:
        extents = ezdxf_bbox.extents(entities, fast=True)
        if not extents.has_data:
            return None
        return BBox(
            min_x=float(extents.extmin.x),
            min_y=float(extents.extmin.y),
            max_x=float(extents.extmax.x),
            max_y=float(extents.extmax.y),
        )
    except Exception:
        logger.warning("Could not compute an entity bounding box", exc_info=True)
        return None


def _bbox_from_vertices(vertices: list[tuple[float, float, float]]) -> BBox | None:
    if not vertices:
        return None
    xs = [vertex[0] for vertex in vertices]
    ys = [vertex[1] for vertex in vertices]
    return BBox(min_x=min(xs), min_y=min(ys), max_x=max(xs), max_y=max(ys))


def _text_value(entity: object) -> str:
    try:
        if entity.dxftype() == "MTEXT":
            return safe_dxf_string(entity.plain_text())
        return safe_dxf_string(entity.dxf.get("text", ""))
    except (AttributeError, ValueError):
        return ""


def _text_point(entity: object) -> tuple[float, float]:
    try:
        insert = entity.dxf.get("insert")
        return float(insert.x), float(insert.y)
    except (AttributeError, TypeError, ValueError):
        return 0.0, 0.0


def _nearby_texts(
    bbox: BBox | None, planche_id: str, texts: list[TexteDxf]
) -> list[str]:
    if bbox is None:
        return []
    center_x = (bbox.min_x + bbox.max_x) / 2
    center_y = (bbox.min_y + bbox.max_y) / 2
    diagonal = math.hypot(bbox.max_x - bbox.min_x, bbox.max_y - bbox.min_y)
    radius = max(3.0, min(30.0, diagonal * 2.0))
    nearby = sorted(
        (
            (math.hypot(text.x - center_x, text.y - center_y), text.contenu)
            for text in texts
            if text.planche_region == planche_id and text.contenu
        ),
        key=lambda item: item[0],
    )
    return [content for distance, content in nearby if distance <= radius][:5]


def _region_for_candidate(
    layout_id: str, candidate_bbox: BBox | None, planches: list[Planche]
) -> str:
    if layout_id != "model" or candidate_bbox is None:
        return layout_id
    center_x = (candidate_bbox.min_x + candidate_bbox.max_x) / 2
    center_y = (candidate_bbox.min_y + candidate_bbox.max_y) / 2
    for planche in planches:
        region = planche.bbox_region
        if (
            planche.id != "model"
            and region is not None
            and region.min_x <= center_x <= region.max_x
            and region.min_y <= center_y <= region.max_y
        ):
            return planche.id
    return "model"


def inspect_dxf(
    path: str | Path, source_filename: str
) -> tuple[ControleTechnique, list[Planche]]:
    """Return a safe technical inventory and computable 2D zone candidates."""

    try:
        document = ezdxf.readfile(Path(path))
    except (OSError, DXFError, ValueError) as exc:
        raise DxfAnalysisError("Impossible de lire le fichier DXF.") from exc

    unit_code = int(document.header.get("$INSUNITS", 0) or 0)
    unit_name, unit_factor = _UNIT_BY_CODE.get(
        unit_code, (f"unite_dxf_{unit_code}", None)
    )
    version = safe_dxf_string(document.dxfversion, 32)
    all_layouts: list[tuple[str, list[object]]] = []
    layout_infos: list[LayoutInfo] = []
    planches: list[Planche] = []
    entity_counts: Counter[str] = Counter()
    texts: list[TexteDxf] = []

    for layout in document.layouts:
        layout_name = safe_dxf_string(layout.name, 128)
        layout_id = "model" if layout_name.casefold() == "model" else f"layout-{_slug(layout_name)}"
        entities = list(layout)
        all_layouts.append((layout_id, entities))
        entity_counts.update(entity.dxftype() for entity in entities)
        layout_bbox = _bbox_from_entities(entities)
        layout_infos.append(
            LayoutInfo(
                nom=layout_name,
                bbox=layout_bbox,
                nombre_entites=len(entities),
            )
        )
        planches.append(
            Planche(
                id=layout_id,
                titre=(
                    "Espace objet (Model)"
                    if layout_id == "model"
                    else f"Presentation : {layout_name}"
                ),
                bbox_region=layout_bbox,
                methode_detection="layout_dxf",
            )
        )
        for entity in entities:
            if entity.dxftype() not in {"TEXT", "MTEXT"}:
                continue
            content = _text_value(entity)
            x, y = _text_point(entity)
            texts.append(
                TexteDxf(
                    contenu=content,
                    contenu_normalise=_normalized(content),
                    x=x,
                    y=y,
                    calque=safe_dxf_string(entity.dxf.get("layer", "0"), 255),
                    handle_dxf=safe_dxf_string(entity.dxf.get("handle", ""), 64),
                    planche_region=layout_id,
                    provenance=Provenance(
                        fichier_source=source_filename,
                        handle_dxf=safe_dxf_string(
                            entity.dxf.get("handle", ""), 64
                        ),
                        calque=safe_dxf_string(
                            entity.dxf.get("layer", "0"), 255
                        ),
                        type_entite=entity.dxftype(),
                        planche_region=layout_id,
                        methode_detection="texte_dxf_positionne",
                    ),
                )
            )

    # This intentionally modest heuristic only surfaces explicit abandoned-version
    # labels. Its region is a review aid, never an automatic business decision.
    for text in texts:
        normalized = _normalized(text.contenu)
        if (
            text.planche_region == "model"
            and "VERSION" in normalized
            and "ABANDONN" in normalized
        ):
            planches.append(
                Planche(
                    id=f"region-abandonnee-{text.handle_dxf or len(planches)}",
                    titre=text.contenu,
                    bbox_region=BBox(
                        min_x=text.x - 30,
                        min_y=text.y - 22,
                        max_x=text.x + 30,
                        max_y=text.y + 22,
                    ),
                    statut=StatutRevue.CANDIDATE,
                    methode_detection="texte_version_abandonnee_a_valider",
                )
            )

    layers: list[LayerInfo] = []
    for layer in document.layers:
        name = safe_dxf_string(layer.dxf.name, 255)
        normalized_name = _normalized(name).strip()
        visible = not layer.is_off()
        frozen = layer.is_frozen()
        plotted = bool(layer.dxf.get("plot", 1))
        suggested = (
            normalized_name in {"POUBELLE", "00"}
            or not visible
            or frozen
            or not plotted
        )
        layers.append(
            LayerInfo(
                nom=name,
                visible=visible,
                gele=frozen,
                trace=plotted,
                exclusion_suggeree=suggested,
            )
        )
    layers.sort(
        key=lambda layer: (
            0 if layer.nom.strip() in PRIORITY_LAYERS else 1,
            layer.nom.casefold(),
        )
    )

    candidates: list[CandidateZone] = []
    ignored_3d = 0
    sequence = 0
    for layout_id, entities in all_layouts:
        for entity in entities:
            if entity.dxftype() not in {"LWPOLYLINE", "POLYLINE"}:
                continue
            if entity.dxftype() == "POLYLINE" and not entity.is_2d_polyline:
                ignored_3d += 1
                continue
            if not entity.is_closed:
                continue
            vertices = _vertices(entity)
            area = _polyline_area(vertices)
            if not math.isfinite(area) or area <= 0:
                continue
            sequence += 1
            handle = safe_dxf_string(
                entity.dxf.get("handle", f"sans-handle-{sequence}"), 64
            )
            layer = safe_dxf_string(entity.dxf.get("layer", "0"), 255)
            candidate_bbox = _bbox_from_vertices(vertices)
            planche_id = _region_for_candidate(layout_id, candidate_bbox, planches)
            provenance = Provenance(
                fichier_source=source_filename,
                handle_dxf=handle,
                calque=layer,
                type_entite=entity.dxftype(),
                planche_region=planche_id,
                methode_detection="polyligne_2d_fermee_surface_analytique",
            )
            candidates.append(
                CandidateZone(
                    id=f"zone-{_slug(planche_id)}-{_slug(handle)}",
                    type_entite=entity.dxftype(),
                    calque=layer,
                    handle_dxf=handle,
                    planche_region=planche_id,
                    surface_geometrique_unites=area,
                    surface_geometrique_m2=(
                        area * unit_factor * unit_factor
                        if unit_factor is not None
                        else None
                    ),
                    bbox=candidate_bbox,
                    sommets=[
                        Sommet(x=x, y=y, bulge=bulge)
                        for x, y, bulge in vertices
                    ],
                    textes_proches=_nearby_texts(candidate_bbox, layout_id, texts),
                    provenance=provenance,
                )
            )
    candidates.sort(
        key=lambda candidate: (
            0 if candidate.calque.strip() in PRIORITY_LAYERS else 1,
            candidate.planche_region,
            candidate.calque.casefold(),
            candidate.handle_dxf,
        )
    )

    warnings: list[str] = [
        (
            "Les planches sont proposees a partir des layouts et des mentions "
            "explicites de versions abandonnees. Leurs regions restent a valider."
        )
    ]
    if unit_factor is None:
        warnings.append(
            "L'unite DXF est absente ou inconnue : une confirmation est obligatoire."
        )
    if ignored_3d:
        warnings.append(
            f"{ignored_3d} polyligne(s) non 2D ignoree(s), sans calcul de surface."
        )
    if not candidates:
        warnings.append("Aucune polyligne 2D fermee calculable n'a ete detectee.")
    if any(layer.exclusion_suggeree for layer in layers):
        warnings.append(
            "Des calques masques, geles, non traces ou nommes Poubelle/00 "
            "sont signales pour exclusion manuelle."
        )

    model_layout = next(
        (layout for layout in layout_infos if layout.nom.casefold() == "model"),
        None,
    )
    control = ControleTechnique(
        version_dxf=version,
        unite_detectee=unit_name,
        bbox=model_layout.bbox if model_layout else None,
        calques=layers,
        layouts=layout_infos,
        types_entites=dict(sorted(entity_counts.items())),
        textes=texts,
        polylignes_fermees=len(candidates),
        zones_candidates=candidates,
        avertissements=warnings,
    )
    return control, planches


def apply_confirmed_unit(control: ControleTechnique, unit: str) -> None:
    factor = meters_per_unit(unit)
    if factor is None:
        raise ValueError("Unite non prise en charge.")
    for candidate in control.zones_candidates:
        candidate.surface_geometrique_m2 = (
            candidate.surface_geometrique_unites * factor * factor
        )
