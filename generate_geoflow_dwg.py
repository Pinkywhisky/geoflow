#!/usr/bin/env python3
"""
Génère geoflow_plan_test.dwg en R2018/AC1032 à partir du fixture DXF.

Prérequis :
    pip install ezdxf
    ODA File Converter installé et accessible par ezdxf.addons.odafc

La documentation ezdxf montre que l'export DWG passe par ODA File Converter.
"""
from pathlib import Path
import math

import ezdxf
from ezdxf import units
from ezdxf.addons import odafc


HERE = Path(__file__).resolve().parent
DXF_PATH = HERE / "geoflow_plan_test_source.dxf"
DWG_PATH = HERE / "geoflow_plan_test.dwg"


def build_source_dxf(path: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.units = units.M
    doc.header["$INSUNITS"] = 6

    for name in ["LOT", "COMMUN", "LOT_COURBE", "REFERENCE", "ANNOTATION", "IGNORED_3D"]:
        if name not in doc.layers:
            doc.layers.add(name)

    msp = doc.modelspace()

    msp.add_lwpolyline(
        [(-20, 0), (-10, 0), (-10, 8), (-20, 8)],
        close=True,
        dxfattribs={"layer": "LOT"},
    )
    msp.add_lwpolyline(
        [(-5, 0), (7, 0), (7, 6), (-5, 6)],
        close=True,
        dxfattribs={"layer": "LOT"},
    )
    msp.add_lwpolyline(
        [(-5, 9), (14, 9), (14, 11), (-5, 11)],
        close=True,
        dxfattribs={"layer": "COMMUN"},
    )

    # Demi-cercle r=5 : aire exacte = 25*pi/2.
    msp.add_lwpolyline(
        [(20, 0, 1.0), (30, 0, 0.0)],
        format="xyb",
        close=True,
        dxfattribs={"layer": "LOT_COURBE"},
    )

    msp.add_lwpolyline(
        [(-20, -4), (-10, -2), (0, -4)],
        close=False,
        dxfattribs={"layer": "REFERENCE"},
    )

    msp.add_text(
        "GeoFlow - fixture DWG/DXF",
        dxfattribs={"layer": "ANNOTATION", "height": 1.0},
    ).set_placement((-20, 14))

    poly3d = msp.add_polyline3d(
        [(35, 0, 0), (39, 0, 1), (39, 4, 0), (35, 4, 0)],
        dxfattribs={"layer": "IGNORED_3D"},
    )
    poly3d.close(True)

    doc.saveas(path)


def main() -> None:
    build_source_dxf(DXF_PATH)

    if not odafc.is_installed():
        raise SystemExit(
            "ODA File Converter n'est pas installé ou n'est pas détecté. "
            "Installe-le puis relance ce script."
        )

    doc = ezdxf.readfile(DXF_PATH)
    odafc.export_dwg(
        doc,
        DWG_PATH,
        version="R2018",
        replace=True,
    )

    print(f"DWG généré : {DWG_PATH}")
    print("Surfaces 2D attendues :")
    print("  LOT        : 80.000000 m²")
    print("  LOT        : 72.000000 m²")
    print("  COMMUN     : 38.000000 m²")
    print(f"  LOT_COURBE : {25 * math.pi / 2:.9f} m²")
    print(f"  TOTAL      : {190 + 25 * math.pi / 2:.9f} m²")


if __name__ == "__main__":
    main()
