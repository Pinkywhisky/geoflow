"""DXF geometry analysis, independent from the web layer."""

import logging
import math
from pathlib import Path
from typing import TypedDict

import ezdxf
from ezdxf.lldxf.const import DXFError

logger = logging.getLogger(__name__)


class DxfAnalysisError(Exception):
    """Raised when a DXF document cannot be read or analysed."""


class PolylineResult(TypedDict):
    entity_type: str
    layer: str
    closed: bool
    area: float


Vertex = tuple[float, float, float]


def _shoelace_area(points: list[tuple[float, float]]) -> float:
    """Return polygon area using the general-purpose shoelace formula."""
    if len(points) < 3:
        return 0.0
    cross_sum = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
    )
    return abs(cross_sum) / 2.0


def _vertices(entity: object) -> list[Vertex]:
    """Return (x, y, outgoing bulge) tuples for a supported polyline."""
    if entity.dxftype() == "LWPOLYLINE":  # type: ignore[attr-defined]
        return [
            (float(x), float(y), float(bulge))
            for x, y, bulge in entity.get_points("xyb")  # type: ignore[attr-defined]
        ]
    return [
        (
            float(vertex.dxf.location.x),
            float(vertex.dxf.location.y),
            float(vertex.dxf.get("bulge", 0.0)),
        )
        for vertex in entity.vertices  # type: ignore[attr-defined]
    ]


def _arc_segment_area(chord_length_squared: float, bulge: float) -> float:
    """Return the exact signed area between a bulge arc and its chord.

    DXF defines the signed included angle as ``4 * atan(bulge)``. The circular
    segment area is ``r² * (theta - sin(theta)) / 2``. For tiny bulges the
    equivalent power series avoids cancellation in ``theta - sin(theta)``.
    """
    if bulge == 0.0 or chord_length_squared == 0.0:
        return 0.0
    if abs(bulge) < 1e-4:
        bulge_squared = bulge * bulge
        return chord_length_squared * bulge * (
            1.0 / 3.0 + bulge_squared / 15.0 - bulge_squared * bulge_squared / 105.0
        )

    theta = 4.0 * math.atan(bulge)
    radius_squared = (
        chord_length_squared * (1.0 + bulge * bulge) ** 2 / (16.0 * bulge * bulge)
    )
    return radius_squared * (theta - math.sin(theta)) / 2.0


def _polyline_area(vertices: list[Vertex]) -> float:
    """Return exact area for straight and circular-arc polyline segments."""
    if len(vertices) < 2:
        return 0.0
    if all(bulge == 0.0 for _, _, bulge in vertices):
        return _shoelace_area([(x, y) for x, y, _ in vertices])

    signed_area = 0.0
    for start, end in zip(vertices, vertices[1:] + vertices[:1]):
        x1, y1, bulge = start
        x2, y2, _ = end
        signed_area += (x1 * y2 - x2 * y1) / 2.0
        chord_length_squared = (x2 - x1) ** 2 + (y2 - y1) ** 2
        signed_area += _arc_segment_area(chord_length_squared, bulge)
    return abs(signed_area)


def analyze_dxf(path: str | Path) -> list[PolylineResult]:
    """Read *path* and return closed 2D LWPOLYLINE/POLYLINE entities."""
    try:
        document = ezdxf.readfile(Path(path))
        modelspace = document.modelspace()
    except (OSError, DXFError, ValueError) as exc:
        raise DxfAnalysisError("Impossible de lire le fichier DXF.") from exc

    results: list[PolylineResult] = []
    for entity in modelspace.query("LWPOLYLINE POLYLINE"):
        if entity.dxftype() == "POLYLINE" and not entity.is_2d_polyline:
            logger.warning("Ignoring non-2D POLYLINE entity")
            continue
        if not entity.is_closed:
            continue
        results.append(
            {
                "entity_type": entity.dxftype(),
                "layer": entity.dxf.layer,
                "closed": True,
                "area": _polyline_area(_vertices(entity)),
            }
        )
    return results
