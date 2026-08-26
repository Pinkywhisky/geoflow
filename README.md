# GeoFlow — MVP v0.4.1

GeoFlow fournit une première tranche verticale du workflow de mise en copropriété :

- création d’un dossier local de type copropriété ;
- import d’un plan DXF ou DWG ;
- conversion DWG vers un DXF temporaire par ODA File Converter ;
- inventaire technique du dessin et sélection des planches, calques et zones ;
- association manuelle des zones à des bâtiments, niveaux et lots ;
- conservation séparée des surfaces géométriques et retenues ;
- génération d’un premier document Word de travail depuis le dossier canonique validé ;
- export du dossier canonique en JSON lisible.

## Assistant guidé v0.4.1

Le parcours principal est organisé en six écrans cohérents :

```text
Dossier → Plan → Contrôle → Lots & surfaces → Synthèse → Document
```

L’unité détectée est acceptée automatiquement. Une justification n’est demandée que si elle est modifiée. Les statuts des planches et des calques sont enregistrés à chaque changement, avec un retour visible `Enregistrement…`, `Enregistré` ou `Échec de l’enregistrement`.

La page Lots & surfaces affiche en priorité les zones non affectées, 25 par page. Elle permet de filtrer par texte, calque, état, affectation, bâtiment, niveau et lot.

La Synthèse réutilise les contrôles de complétude du générateur documentaire. `Valider le dossier` est l’unique validation globale du parcours. Elle enregistre une empreinte SHA-256 des données métier et déverrouille l’étape Document. Toute modification ultérieure replace le dossier à l’état `À valider`, sans supprimer les DOCX historiques.

Depuis l’accueil, l’icône poubelle supprime un dossier après confirmation explicite. La suppression couvre le JSON canonique et ses documents générés, sans pouvoir sortir du répertoire de données du dossier ciblé.

Les calculs v0.2 sont conservés : polylignes 2D fermées uniquement, formule du lacet pour les segments droits et intégration analytique exacte des arcs DXF `bulge`. Les `POLYLINE` 3D restent ignorées.

## Lancer avec Docker

```bash
docker compose up -d --build
```

Ouvrir <http://localhost:8080>. Les dossiers JSON et les documents générés sont conservés dans le volume Docker `geoflow-data`. Le conteneur fonctionne avec l’utilisateur non privilégié `geoflow`.

Le healthcheck interroge `/health` :

```bash
curl http://localhost:8080/health
docker compose ps
docker compose exec geoflow id
```

## Conversion DWG

ODA File Converter n’est ni téléchargé ni redistribué par GeoFlow. Le `compose.yaml` monte en lecture seule l’installation WSL actuellement attendue :

- exécutable : `/usr/bin/ODAFileConverter` ;
- répertoire applicatif : `/usr/bin/ODAFileConverter_27.1.0.0`.

Ces chemins sont remplaçables avant le lancement :

```bash
export ODA_FILE_CONVERTER_HOST=/chemin/vers/ODAFileConverter
export ODA_INSTALL_DIR_HOST=/chemin/vers/le/repertoire/ODA
docker compose up -d --build
```

Le conteneur exécute ODA dans un serveur X virtuel isolé (`xvfb-run`) et ne reçoit aucun accès à l’affichage graphique de l’hôte. Le runtime Python reste non privilégié. Pour une installation sans ODA et limitée aux DXF, retirer les deux montages ODA du fichier Compose : l’application continue alors à fonctionner et refuse les DWG avec un message explicite, sans traceback client.

Chaque conversion emploie un répertoire temporaire isolé et un nom fixe `source.dwg`. Le DXF produit et tout l’espace temporaire sont supprimés après succès comme après erreur.

## Génération Word v0.4

Une fois le dossier structuré, la page `/dossiers/{id}/synthese` centralise les blocages et avertissements. La page `/dossiers/{id}/documents` reste verrouillée tant que les données n’ont pas été validées depuis cette synthèse.

Le bouton de génération produit un brouillon A4 :

- depuis les seules données canoniques déjà validées, sans relire ni réanalyser le plan source ;
- avec la mention visible `BROUILLON À VALIDER` ;
- avec bâtiments, niveaux, lots, zones, surfaces géométriques et retenues, corrections justifiées, parties communes, droits, servitudes et éventuels millièmes fournis ;
- sans inventer de clause juridique, de surface, de lot ou de millième.

Le modèle versionné est `app/documents/templates/copropriete_draft_v1.docx` (identifiant `copropriete_draft_v1`, version `1.0`). Il peut être reconstruit de manière reproductible avec :

```bash
.venv/bin/python -m tools.build_copropriete_template
```

La génération applicative utilise uniquement `python-docx`. LibreOffice n’est pas une dépendance du runtime : il sert seulement à la QA visuelle hors production.

## Modèle, provenance et persistance

Le modèle canonique se trouve dans `app/domain`. Il contient le dossier, le plan importé, les planches, bâtiments, niveaux, lots, zones, géométries sources, décisions de validation, éléments juridiques optionnels et historique des générations. Une correction de valeur exige une justification et ne remplace jamais la valeur détectée.

La persistance JSON est isolée dans `app/storage.py`. Par défaut, les dossiers sont écrits sous `data/`, ignoré par Git ; ce chemin peut être changé avec `GEOFLOW_DATA_DIR`.

Chaque génération est enregistrée dans un sous-répertoire isolé avec :

- le DOCX au nom neutralisé ;
- un snapshot JSON canonique ;
- l’identifiant et la version du template ;
- la date de génération ;
- le SHA-256 du snapshot ;
- le statut `brouillon`.

## Détection des planches et zones

Les zones candidates sont exclusivement des `LWPOLYLINE` ou `POLYLINE` 2D fermées dont la surface est calculable. Les calques `80`, `81`, `82` et `83` sont présentés en priorité, mais jamais approuvés automatiquement. Les calques masqués, gelés, non tracés ou nommés `Poubelle`/`00` sont seulement signalés.

La première heuristique de planches repose sur les layouts DXF et sur les textes contenant à la fois `VERSION` et `ABANDONN`. La région proposée autour de ces textes est volontairement approximative et doit être validée par l’utilisateur.

## Tests

```bash
python -m pytest -q
docker compose run --rm geoflow python -m pytest -q
```

Les tests couvrent le moteur DXF, les routes HTTP, la conversion DWG isolée, le nouvel assistant, l’autosave, la pagination, l’invalidation après modification, la suppression des dossiers, le contexte documentaire, le contenu et l’hygiène OOXML, la persistance du snapshot et la confidentialité. Les tests versionnés utilisent uniquement des données synthétiques. Les dossiers sous `samples/private/` restent exclus de Git et du contexte Docker.

## Limites volontaires de la v0.4.1

Le document produit reste un brouillon technique : aucune validation juridique, aucun calcul de millièmes, aucune qualification automatique des droits ou servitudes, aucune table des matières automatique et aucune reproduction exhaustive d’un acte existant. La reconnaissance parfaite des cartouches et feuilles reste également hors périmètre.

## Arrêter GeoFlow

```bash
docker compose down
```
