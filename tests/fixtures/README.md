# Fixtures publiques

- `copropriete_complete.json` : dossier de copropriété entièrement synthétique utilisé par les tests documentaires.
- `geoflow_plan_test_source.dxf` : dessin synthétique reproductible par `generate_geoflow_dwg.build_source_dxf()`, déplacé depuis la racine du dépôt. Il contient deux rectangles LOT (80 et 72 m²), un rectangle COMMUN (38 m²), un demi-disque LOT_COURBE (25π/2 m²), une polyligne ouverte de référence, une annotation « GeoFlow - fixture DWG/DXF » et une polyligne 3D ignorée. Il ne représente aucun relevé client.

Le DXF sert à la QA runtime DXF/DWG et au contrôle des arcs. ODA produit les DWG localement ; ils ne sont pas versionnés ni inclus dans l’image runtime. Les métadonnées techniques DXF (horodatages, GUID, handles) ne sont pas des données métier.
