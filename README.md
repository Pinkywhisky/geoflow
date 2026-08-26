# GeoFlow — MVP v0.3

GeoFlow fournit une première tranche verticale du workflow de mise en copropriété :

- création d’un dossier local de type copropriété ;
- import d’un plan DXF ou DWG ;
- conversion DWG vers un DXF temporaire par ODA File Converter ;
- inventaire technique du dessin et sélection des planches, calques et zones ;
- association manuelle des zones à des bâtiments, niveaux et lots ;
- conservation séparée des surfaces géométriques et retenues ;
- export du dossier canonique en JSON lisible.

Les calculs v0.2 sont conservés : polylignes 2D fermées uniquement, formule du lacet pour les segments droits et intégration analytique exacte des arcs DXF `bulge`. Les `POLYLINE` 3D restent ignorées.

## Lancer avec Docker

```bash
docker compose up -d --build
```

Ouvrir <http://localhost:8080>. Les dossiers JSON sont conservés dans le volume Docker `geoflow-data`. Le conteneur fonctionne avec l’utilisateur non privilégié `geoflow`.

Le healthcheck interroge `/health` :

```bash
curl http://localhost:8080/health
docker compose ps
```

## Conversion DWG

L’adaptateur cherche `ODAFileConverter` dans le `PATH`. Un autre chemin peut être fourni avec :

```bash
export ODA_FILE_CONVERTER=/usr/bin/ODAFileConverter
```

Sous WSL, `ODA_QT_PLATFORM=xcb` peut être défini si nécessaire. Le binaire installé sur l’hôte est utilisable lorsque GeoFlow est lancé localement dans WSL. Il n’est pas incorporé à l’image Docker : pour traiter des DWG dans le conteneur, ODA doit aussi être installé ou monté dans ce runtime. En son absence, les DXF continuent de fonctionner et les DWG reçoivent une erreur explicite, sans traceback client.

Chaque conversion emploie un répertoire isolé et un nom fixe `source.dwg`. Le DXF produit et tout l’espace temporaire sont supprimés après succès comme après erreur.

## Modèle et persistance

Le modèle canonique se trouve dans `app/domain`. Il contient le dossier, le plan importé, les planches, bâtiments, niveaux, lots, zones, géométries sources, provenance et décisions de validation. Une correction de valeur exige une justification et ne remplace jamais la valeur détectée.

La persistance JSON est isolée dans `app/storage.py`. Par défaut les fichiers sont écrits dans `data/`, ignoré par Git. Le chemin peut être changé avec `GEOFLOW_DATA_DIR`.

## Détection des planches et zones

Les zones candidates sont exclusivement des `LWPOLYLINE` ou `POLYLINE` 2D fermées dont la surface est calculable. Les calques `80`, `81`, `82` et `83` sont présentés en priorité, mais jamais approuvés automatiquement. Les calques masqués, gelés, non tracés ou nommés `Poubelle`/`00` sont seulement signalés.

La première heuristique de planches repose sur les layouts DXF et sur les textes contenant à la fois `VERSION` et `ABANDONN`. La région proposée autour de ces textes est volontairement approximative et doit être validée par l’utilisateur. La reconnaissance parfaite des cartouches et feuilles reste hors périmètre v0.3.

## Tests

```bash
docker compose run --rm geoflow python -m pytest -q
```

Les tests DWG utilisent des doubles synthétiques de conversion et ne nécessitent aucun DWG privé. Les dossiers sous `samples/private/` restent exclus de Git et du contexte Docker.

## Arrêter GeoFlow

```bash
docker compose down
```
