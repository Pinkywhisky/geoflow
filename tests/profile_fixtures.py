"""Synthetic DXF following a firm charter unlike the legacy 80/81/05/83 one."""

from __future__ import annotations

from pathlib import Path

import ezdxf

PRINCIPAL = "A-SURF-LOGEMENT"
CAVE = "A-SURF-CAVE"
BALCONY = "A-SURF-BALCON"
TERRACE = "A-SURF-TERRASSE"
COMMON = "A-COMMUN"
ARCHIVE = "ARCHIVE_2023"
LOT_TEXT = "T-LOT"
SURFACE_TEXT = "T-SURFACE"
LEVEL_TEXT = "T-NIVEAU"
TITLE_TEXT = "T-CARTOUCHE"


def build_charter_dxf(
    path: Path, *, with_terrace: bool = False, with_balconies: bool = True, units: int = 6, marker: bool = False
) -> Path:
    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = units
    for name in (PRINCIPAL, CAVE, BALCONY, TERRACE, COMMON, ARCHIVE, LOT_TEXT, SURFACE_TEXT, LEVEL_TEXT, TITLE_TEXT):
        document.layers.add(name)
    msp = document.modelspace()

    def rect(x, y, w, h, layer):
        msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs={"layer": layer})
        return w * h

    def text(value, x, y, layer, height=0.3):
        msp.add_text(value, dxfattribs={"layer": layer, "height": height}).set_placement((x, y))

    # Two levels of two flats each; each flat is two rooms.
    for level, base_y, label in ((0, 0, "RDC"), (1, 20, "R+1")):
        text(label, -6, base_y + 5, LEVEL_TEXT)
        text("PLAN DE COPROPRIETE", -6, base_y + 7, TITLE_TEXT)
        for index, base_x in enumerate((0, 12)):
            number = level * 2 + index + 1
            area = rect(base_x, base_y, 6, 6, PRINCIPAL) + rect(base_x + 6, base_y, 3, 6, PRINCIPAL)
            text(f"LOT {number}", base_x + 2, base_y + 3, LOT_TEXT)
            text(f"{area:.2f}".replace(".", ",") + " m²", base_x + 2, base_y + 2, SURFACE_TEXT)
            if with_balconies:
                rect(base_x + 1, base_y - 1.5, 4, 1.5, BALCONY)
        rect(9.5, base_y, 2, 6, COMMON)

    # Cellars in the basement, each with its lot number written inside.
    text("SOUS-SOL", -6, -15, LEVEL_TEXT)
    for number in range(1, 5):
        x = (number - 1) * 4
        rect(x, -20, 3, 3, CAVE)
        text(f"LOT {number}", x + 0.5, -19, LOT_TEXT)
    if with_terrace:
        rect(30, 0, 5, 5, TERRACE)
    rect(60, 60, 8, 8, ARCHIVE)
    if marker:
        text("VERSION 1 ABANDONNEE", 60, 70, TITLE_TEXT)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.saveas(path)
    return path
