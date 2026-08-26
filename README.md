# GeoFlow — MVP v0.4.3

GeoFlow fournit une première tranche verticale du workflow de mise en copropriété :

- création d’un dossier local de type copropriété ;
- import d’un plan DXF ou DWG ;
- conversion DWG vers un DXF temporaire par ODA File Converter ;
- inventaire technique du dessin et sélection des planches, calques et zones ;
- réconciliation explicable des contours, annotations et lots proposés ;
- confirmation manuelle des rapprochements ambigus ;
- conservation séparée des surfaces géométriques et retenues ;
- saisie structurée de l’adresse, de la date du plan et de plusieurs parcelles cadastrales ;
- qualification des lots et saisie contrôlée des millièmes fournis ;
- génération d’un premier document Word de travail depuis le dossier canonique validé ;
- export du dossier canonique en JSON lisible.

## Assistant guidé v0.4.3

Le parcours principal est organisé en six écrans cohérents :

```text
Dossier → Plan → Contrôle → Lots & surfaces → Synthèse → Document
```

L’unité détectée est acceptée automatiquement. Une justification n’est demandée que si elle est modifiée. Les statuts des planches et des calques sont enregistrés à chaque changement, avec un retour visible `Enregistrement…`, `Enregistré` ou `Échec de l’enregistrement`.

L’étape Dossier enregistre une adresse structurée, la commune, le département, la date du plan ou relevé et une liste de parcelles cadastrales. Les anciens JSON contenant une adresse ou une référence cadastrale textuelle restent lisibles.

La page Lots & surfaces est centrée sur les lots proposés. Elle présente leur statut, leurs surfaces agrégées, les preuves positives ou négatives et les zones sources. L’usage, la désignation et les millièmes fournis sont éditables avec autosave. Chaque millième possède un statut explicite ; une grille déclarée complète doit couvrir tous les lots et totaliser exactement 1000. Les rapprochements sûrs sont marqués `Auto-vérifié` ; les autres restent explicitement `À confirmer`, `Non résolu` ou `Contradictoire`. Une vue technique séparée conserve l’accès aux contours individuels et aux filtres historiques.

La Synthèse réutilise les contrôles de complétude du générateur documentaire. `Valider le dossier` est l’unique validation globale du parcours. Elle enregistre une empreinte SHA-256 des données métier et déverrouille l’étape Document. Toute modification ultérieure replace le dossier à l’état `À valider`, sans supprimer les DOCX historiques.

Depuis l’accueil, l’icône poubelle supprime un dossier après confirmation explicite. La suppression couvre le JSON canonique et ses documents générés, sans pouvoir sortir du répertoire de données du dossier ciblé.

Les calculs v0.2 sont conservés : polylignes 2D fermées uniquement, formule du lacet pour les segments droits et intégration analytique exacte des arcs DXF `bulge`. Les `POLYLINE` 3D restent ignorées.

## Réconciliation explicable v0.4.2

La réconciliation transforme les données techniques sans altérer leur provenance :

```text
Contour technique → zone métier candidate → proposition de lot → décision humaine éventuelle
```

Le profil de calques `geometre-npg-v1` distingue notamment les contours principaux (`80`), annexes (`81`), exclus (`82`, `Poubelle`, `00`), repères de lots (`05`) et annotations de surface (`83`). Les règles sont centralisées et versionnées dans `app/reconciliation/engine.py` afin de pouvoir ajouter d’autres profils sans modifier le modèle canonique.

Le moteur normalise les textes positionnés, les rattache à une planche et utilise un index spatial en grille. Une proposition conserve chaque preuve avec sa source, sa valeur, sa fiabilité, sa polarité, sa description et sa provenance. Une décision manuelle n’efface jamais le résultat automatique qui l’a précédée.

L’auto-vérification est volontairement stricte : elle exige un repère de lot, une annotation de surface cohérente avec la géométrie dans les tolérances absolue et relative, une planche retenue, un titre de plan suffisamment spécifique et plusieurs contours métier. Un texte, un nom de calque ou une surface isolée ne suffisent jamais. Les contradictions, doublons, propositions incomplètes et zones non rapprochées sont exposés dans les contrôles globaux et bloquent la validation documentaire lorsqu’ils sont critiques.

## Données métier et avertissements v0.4.3

Les champs métier éditables sont enregistrés sans recharger la page. Toute modification postérieure à la validation invalide l’empreinte métier et replace le dossier à l’état `À valider`.

La Synthèse et l’étape Document distinguent :

- les actions utilisateur, avec un lien vers l’étape où compléter l’adresse, le cadastre, les lots ou les millièmes ;
- les limitations du produit, notamment les clauses juridiques non prises en charge ;
- les informations techniques non bloquantes.

Un brouillon reste générable lorsque des données métier sont absentes. En revanche, une grille de millièmes déclarée complète mais incohérente est bloquante.

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

Le modèle versionné est `app/documents/templates/copropriete_draft_v1.docx` (identifiant `copropriete_draft_v1`, version `1.1`). Le DOCX emploie des libellés métier (`Zone 1`, `Zone 2`, etc.) et n’expose ni identifiant interne ni handle DXF. Les marges A4 sont fixées à 5 cm à gauche et 2 cm à droite, en haut et en bas ; les tableaux utilisent exactement la largeur utile. Le modèle peut être reconstruit de manière reproductible avec :

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

Les zones candidates sont exclusivement des `LWPOLYLINE` ou `POLYLINE` 2D fermées dont la surface est calculable. Le profil actif classe les calques `80` et `81` comme sources métier possibles. Les exclusions `82`, `Poubelle` et `00` restent explicites, traçables et réversibles par une décision de calque. Un état masqué, gelé ou non tracé est seulement signalé et ne suffit jamais, seul, à exclure une géométrie.

La première heuristique de planches repose sur les layouts DXF et sur les textes contenant à la fois `VERSION` et `ABANDONN`. La région proposée autour de ces textes est volontairement approximative et doit être validée par l’utilisateur.

## Tests

```bash
python -m pytest -q
docker compose run --rm geoflow python -m pytest -q
```

Les tests couvrent le moteur DXF, les routes HTTP, la conversion DWG isolée, l’assistant, l’autosave, la pagination, l’invalidation après modification, la suppression des dossiers, le contexte documentaire, le contenu et l’hygiène OOXML, la persistance du snapshot et la confidentialité. La v0.4.2 ajoute plus de vingt scénarios synthétiques de réconciliation ; la v0.4.3 couvre le modèle métier rétrocompatible, les parcelles multiples, l’édition des lots, les statuts de millièmes, la règle des 1000, les avertissements actionnables et les marges du DOCX. Les tests versionnés utilisent uniquement des données synthétiques. Les dossiers sous `samples/private/` restent exclus de Git et du contexte Docker.

## Limites volontaires de la v0.4.3

Le document produit reste un brouillon technique : aucune validation juridique, aucun calcul de millièmes, aucun import de grille depuis XLSX, aucune qualification automatique des droits ou servitudes, aucune table des matières automatique et aucune reproduction exhaustive d’un acte existant. Les millièmes sont uniquement repris des valeurs saisies et validées par l’utilisateur. La reconnaissance parfaite des cartouches et feuilles reste également hors périmètre.

La réconciliation est déterministe et fondée sur des règles prudentes. Elle n’emploie ni apprentissage automatique, ni OCR, ni LLM, ni dépendance géométrique lourde. Les présentations DXF ambiguës ou inconnues restent à confirmer manuellement ; l’absence de proposition automatique est préférée à un faux rapprochement.

## Arrêter GeoFlow

```bash
docker compose down
```
