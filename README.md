# GeoFlow — MVP

GeoFlow est distribué sous **GNU Affero General Public License version 3 uniquement**, identifiant SPDX **`AGPL-3.0-only`**. Le texte officiel complet figure dans [LICENSE](LICENSE).

La version applicative est centralisée dans [`app/version.py`](app/version.py) et affichée dans l’interface et l’API. Les changements en préparation sont décrits dans le [changelog](CHANGELOG.md).

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
- aperçu SVG des contours détectés, coloré selon l’état des lots, avec un zoom par proposition ;
- thème clair ou sombre, automatique selon le système ou choisi depuis l’en-tête.

## Assistant guidé

Le parcours principal est organisé en six écrans cohérents :

```text
Dossier → Plan → Contrôle → Lots & surfaces → Synthèse → Document
```

L’unité détectée est acceptée automatiquement. Une justification n’est demandée que si elle est modifiée. Les statuts des planches et des calques sont enregistrés à chaque changement, avec un retour visible `Enregistrement…`, `Enregistré` ou `Échec de l’enregistrement`.

L’étape Dossier enregistre une adresse structurée, la commune, le département, la date du plan ou relevé et une liste de parcelles cadastrales. Les anciens JSON contenant une adresse ou une référence cadastrale textuelle restent lisibles.

La page Lots & surfaces est centrée sur les lots proposés. Elle présente leur statut, leurs surfaces agrégées, les preuves positives ou négatives et les zones sources. L’usage, la désignation et les millièmes fournis sont éditables avec autosave. Chaque millième possède un statut explicite ; une grille déclarée complète doit couvrir tous les lots et totaliser exactement 1000. Les rapprochements sûrs sont marqués `Auto-vérifié` ; les autres restent explicitement `À confirmer`, `Non résolu` ou `Contradictoire`. Une vue technique séparée conserve l’accès aux contours individuels et aux filtres historiques.

La Synthèse réutilise les contrôles de complétude du générateur documentaire. `Valider le dossier` est l’unique validation globale du parcours. Elle enregistre une empreinte SHA-256 des données métier et déverrouille l’étape Document. Toute modification ultérieure replace le dossier à l’état `À valider`, sans supprimer les DOCX historiques.

Depuis l’accueil, l’icône poubelle supprime un dossier après confirmation explicite. La suppression couvre le JSON canonique et ses documents générés, sans pouvoir sortir du répertoire de données du dossier ciblé.

## Analyse rapide

L’Analyse rapide est un pré-diagnostic autonome, distinct du workflow de production. Un DXF ou DWG est inspecté avec les mêmes briques que l’import d’un dossier : conversion éventuelle, inventaire technique, calcul des contours, application du profil de calques et réconciliation explicable. Les layouts explicites sont retenus provisoirement pour le diagnostic et les régions portant une mention de version abandonnée sont écartées provisoirement. Ces décisions restent révisables après création d’un dossier.

L’indice de compréhension repose sur des règles déterministes, sans pourcentage statistique :

- **Bonne** : unité connue, planche exploitable, au moins un lot proposé, majorité des lots auto-vérifiée, aucune contradiction et au plus 20 % des surfaces principales (et autres zones métier hors annexes) non rapprochées ;
- **Partielle** : unité et structure métier détectées, mais confirmations, zones non rapprochées ou contradictions encore présentes ;
- **Faible** : unité, planches ou rapprochements métier insuffisants pour une lecture fiable.

L’intervention humaine est faible lorsque le diagnostic est bon et sans point restant, importante lorsque la compréhension est faible, que plusieurs contradictions existent ou que la majorité d’au moins trois zones métier reste à traiter, et moyenne dans les autres cas.

La page de diagnostic se lit de haut en bas : conclusion et niveaux (compréhension, intervention humaine), résumé factuel, points à vérifier classés par gravité (`À corriger`, `À vérifier`, `Information`), lots proposés avec leur explication « Pourquoi ? », structure, surfaces et compatibilité, puis détails techniques repliés. Toutes les phrases sont produites par des règles déterministes à partir du snapshot : aucun pourcentage, aucune estimation de durée, aucun identifiant interne affiché par défaut. Les annexes, que le moteur ne rattache pas automatiquement aux lots, sont signalées séparément des surfaces principales sans lot. L’action principale dépend du niveau — créer le dossier, examiner les points à vérifier ou voir pourquoi — sans jamais bloquer la création.

Chaque analyse est conservée sous forme de snapshot JSON canonique dans `data/quick-analyses/` pendant 24 heures. Le fichier source et l’éventuel DXF de conversion ne sont pas conservés. La création d’un dossier réutilise directement le snapshot — plan importé, contrôle, zones, propositions, preuves et contrôles globaux — puis supprime l’analyse temporaire, sans deuxième parsing.

Les calculs v0.2 sont conservés : polylignes 2D fermées uniquement, formule du lacet pour les segments droits et intégration analytique exacte des arcs DXF `bulge`. Les `POLYLINE` 3D restent ignorées.

## Profils de dessin

Un profil de dessin est un contrat de lecture des plans d’un cabinet : il indique où GeoFlow doit chercher chaque information et quelle source fait autorité. GeoFlow n’impose aucune nomenclature (ni ISO 13567, ni noms de calques) : la charte du client prime sur les conventions suggérées.

Depuis « Profils de dessin », un plan déposé est inspecté une seule fois, comme pour l’analyse rapide. GeoFlow préremplit un assistant en cinq étapes — Dessin, Zones métier, Identification, Versions & priorités, Vérification — dont chaque valeur est un champ directement éditable ; si les propositions sont justes, « Suivant » jusqu’à « Enregistrer le profil » suffit. Chaque proposition est expliquée par des observations du plan (contours, numéros, surfaces) ou par la classification du moteur actuel ; aucune n’est un score.

Les rôles de calques canoniques sont `surface_principale`, `annexe`, `partie_commune`, `hors_copropriete`, `exclusion` et `archive` ; les annotations couvrent le numéro de lot, la surface annotée, le niveau et le titre de planche. Les sources de texte disponibles sont « texte dans la zone » et « texte proche », dans l’ordre de priorité choisi : la première qui donne une valeur fait foi, une source contraire est signalée. Les attributs de bloc ne sont pas encore lus par l’inspection et ne sont donc pas proposés. Une mention de version (par exemple `VERSION * ABANDONNEE`) détecte une version inactive sans jamais délimiter de géométrie.

Les motifs utilisent un mini-format contrôlé : `*` et `?` pour les calques, un unique `{numero}`, `{surface}` ou `{niveau}` pour les textes. Aucune expression régulière, aucun code ni chemin n’est accepté ; longueurs, listes et valeurs numériques sont bornées.

La vérification applique le profil au snapshot technique, sans relire le plan ni lancer la réconciliation, et à chaque modification avec un délai de saisie. Elle classe le profil :

- **Complet** : unité, surfaces principales, numéros et surfaces annotées de tous les lots, niveaux et annexes sont déterminés par des règles, sans contradiction ni mention de version ;
- **Utilisable avec avertissements** : le profil fonctionne, mais certains éléments restent ambigus (zones sans numéro, écarts de surface, sources contradictoires, annexes sans lot explicite, niveaux manquants, mention de version, calque à plusieurs rôles) ; GeoFlow garde ses règles actuelles pour ces éléments ;
- **Incomplet** : unité, calque de surface principale ou numéros de lot introuvables.

Un profil est enregistré en JSON atomique dans `data/profiles/`, sous un identifiant stable indépendant de son nom ; il ne contient que des règles, jamais de géométrie, de coordonnées ni d’annotation du plan. « Tester sur un autre plan » applique un profil existant à un nouveau dessin et propose les calques non couverts qui ressemblent à un rôle ; rien n’est ajouté sans clic ni enregistré sans « Enregistrer le profil ».

### Utilisation quotidienne

Un profil se crée occasionnellement puis se réutilise. Le parcours quotidien reste : déposer un plan, vérifier le profil proposé, analyser.

```text
Dépôt → inspection technique (une seule) → choix du profil → analyse métier → diagnostic → dossier
```

Après le dépôt, seule l’inspection technique est faite. S’il n’existe aucun profil, l’analyse automatique (legacy) est lancée directement, comme avant. Sinon l’écran « Quel profil utiliser ? » compare chaque profil au plan sur le snapshot technique, sans relire le fichier ni lancer la réconciliation, et affiche un niveau factuel avec ses raisons :

- **Compatible** : unité conforme, surfaces principales et numéros de lot reconnus, aucun avertissement ;
- **Compatible avec avertissements** : zones sans numéro, surfaces non annotées, sources contradictoires, règle de niveau non applicable, calques de contours non couverts par le profil ;
- **Peu compatible** : sur ce plan, le profil ne reconnaît ni surface principale ni numéro de lot, ou impose une unité différente de celle déclarée par le fichier. L’analyse n’est lancée qu’après « Continuer avec ce profil ».

Un profil est **recommandé** seulement s’il est clairement préférable : au meilleur niveau atteint, aucun autre n’identifie plus de zones ni ne laisse moins de contours non couverts, et il fait strictement mieux que chacun sur l’un de ces deux faits. Il n’y a pas de score et les égalités ne sont jamais départagées arbitrairement. La présélection suit cet ordre : le profil déjà choisi pour ce plan, puis le profil par défaut (défini dans « Profils de dessin ») s’il n’est pas peu compatible, puis le profil recommandé, sinon rien. Le profil utilisé le plus récemment apparaît en tête sans être présélectionné. « GeoFlow automatique / Legacy » est toujours proposé. « Créer un nouveau profil à partir de ce plan » ouvre l’assistant sur le même snapshot ; après l’enregistrement, « Utiliser ce profil pour analyser le plan » revient au parcours sans nouveau dépôt. La modification d’un profil existant reste une opération d’administration, depuis « Profils de dessin ».

### Application du profil

Le profil choisi est réellement appliqué par la réconciliation (`app/reconciliation/profiled.py`), avec la même lecture que la vérification du profil (`app/profiles/reading.py`) :

- unité du profil (une unité différente de celle du fichier est retenue avec une décision justifiée et signalée) ;
- source géométrique Model / présentations, présentations exclues, refus des contours auto-intersectés ;
- rôles des calques : exclusion, archive et hors copropriété écartés ; surfaces principales, annexes et parties communes qualifiées ;
- numéro de lot et surface annotée lus par les sources du profil, dans leur ordre de priorité, avec leur motif, leur rayon et la tolérance du profil. Une source secondaire contradictoire est signalée, jamais retenue ;
- niveaux par les correspondances du profil, proposés par défaut lors de la confirmation d’un lot ;
- annexes : numéro inscrit dans l’annexe si le profil le prévoit, sinon proposition par proximité ou contact ; rien n’est rattaché sans l’utilisateur.

Un lot lu selon le profil, sans contradiction et dont la surface annotée est cohérente, est auto-vérifié. Un écart hors tolérance reste à confirmer, même si le profil désigne la source qui fait foi.

**Mode hybride.** Ce que le profil ne décide pas revient explicitement au moteur automatique : calques sans rôle dans le profil, et lots qu’aucune règle du profil ne numérote, à condition de n’utiliser aucun contour que le profil a classé autrement. Une règle du profil n’est jamais remplacée par une heuristique contraire. Chaque preuve, proposition et zone porte son origine (`profil` ou `legacy`) ; le diagnostic indique par exemple « 4 lots identifiés par le profil · 1 complété par l’analyse automatique ». Les décisions humaines (planche ou calque exclus, lots confirmés, annexes rattachées) priment sur les deux.

### Révisions et reproductibilité

`schema_version` (2) décrit le format du fichier ; les profils au format 1 sont lus comme révision 1. `revision` compte les modifications des règles : elle augmente à chaque enregistrement qui change le hash SHA-256 des règles (`content_hash`). Un renommage seul est une métadonnée et ne crée pas de révision ; une duplication repart à la révision 1.

L’analyse conserve dans le dossier (`profile_usage`) le mode choisi, l’identifiant, le nom, la révision, le hash et une copie exacte des règles, jamais de données du plan. Toute réconciliation ultérieure du dossier (unité, planche, calque, confirmation, annexe, réimport d’un plan) utilise ce snapshot et jamais le profil stocké. Une révision plus récente, un renommage ou une suppression du profil sont seulement signalés dans l’étape Dossier ; ils ne changent ni les résultats ni l’historique. Les dossiers antérieurs, sans `profile_usage`, restent lus comme « Analyse automatique / Legacy » ; les champs ajoutés ne sont pas écrits tant qu’ils sont vides, si bien que leur JSON et leur empreinte de validation sont inchangés.

La ré-analyse d’un dossier existant avec un autre profil ou une révision plus récente n’est pas proposée. Elle pourrait renuméroter ou regrouper autrement les propositions, alors que les confirmations humaines, les annexes rattachées, la validation et les DOCX y sont liés. Elle demandera un aperçu des différences avant d’être sûre. Avant la création du dossier, le profil d’une analyse rapide peut en revanche être changé librement, puisqu’aucune décision humaine n’y est encore attachée.

## Référence du dossier

Chaque dossier possède un identifiant technique immuable (`id`), qui sert de clé de stockage, d’URL et de répertoire des documents générés, et une référence métier modifiable (`reference`). Lors d’une création depuis l’analyse rapide, le nom du fichier sans extension est proposé par défaut ; il peut être remplacé avant la création, puis à tout moment dans l’étape Dossier.

La référence est conservée telle que saisie : seuls les caractères de contrôle et les espaces superflus sont retirés. Elle est unique, sans distinction de casse ni d’espacement, afin que la liste de reprise ne présente jamais deux dossiers indiscernables ; une collision est refusée avec un message explicite, sans suffixe ajouté automatiquement. La référence figurant dans le brouillon Word, son changement replace le dossier à l’état `À valider` ; les DOCX déjà générés conservent leur nom et restent téléchargeables.

## Réconciliation explicable v0.4.2

La réconciliation transforme les données techniques sans altérer leur provenance :

```text
Contour technique → zone métier candidate → proposition de lot → décision humaine éventuelle
```

Le profil de calques `geometre-npg-v1` distingue notamment les contours principaux (`80`), annexes (`81`), exclus (`82`, `Poubelle`, `00`), repères de lots (`05`) et annotations de surface (`83`). Les règles sont centralisées et versionnées dans `app/reconciliation/engine.py` afin de pouvoir ajouter d’autres profils sans modifier le modèle canonique.

Le moteur normalise les textes positionnés, les rattache à une planche et utilise un index spatial en grille. Une proposition conserve chaque preuve avec sa source, sa valeur, sa fiabilité, sa polarité, sa description et sa provenance. Une décision manuelle n’efface jamais le résultat automatique qui l’a précédée.

L’auto-vérification est volontairement stricte : elle exige un repère de lot, une annotation de surface cohérente avec la géométrie dans les tolérances absolue et relative, une planche retenue, un titre de plan suffisamment spécifique et plusieurs contours métier. Un texte, un nom de calque ou une surface isolée ne suffisent jamais. Les contradictions, doublons, propositions incomplètes et zones non rapprochées sont exposés dans les contrôles globaux et bloquent la validation documentaire lorsqu’ils sont critiques.

### Espaces DXF et représentations multiples

Les présentations (layouts) sont exprimées en coordonnées papier : leur emprise ne sert jamais à classer un contour ou un texte de l’espace objet. Seules les sous-régions de l’espace objet, comme les mentions explicites de version abandonnée, affinent la région d’une entité de Model ; une même règle s’applique aux contours et aux textes. Les dossiers importés avant cette correction conservent leurs régions d’origine : réimporter le plan les recalcule.

Une proposition regroupe les occurrences d’un même numéro de lot, ce qui permet un lot sur plusieurs niveaux. Deux règles conservatrices évitent de compter deux fois le même objet :

- une occurrence copie exacte d’une occurrence retenue sur une autre planche — mêmes contours, sommets et arcs après une translation unique, même surface annotée — est une représentation dupliquée ;
- lorsqu’au moins une occurrence porte sa propre surface annotée, les occurrences sans annotation (versions antérieures, brouillons) ne sont pas additionnées.

Une représentation écartée reste rattachée à la proposition avec une preuve négative ; elle est proposée décochée lors de la confirmation du lot, pour le cas d’un autre niveau. Un lot concerné ne peut pas être auto-vérifié. Deux copies sur la même planche ne sont jamais fusionnées : elles peuvent représenter deux niveaux. Une surface identique, un numéro commun ou la proximité ne suffisent jamais à fusionner.

### Annexes

Les annexes (calque 81) sont des zones métier distinctes. Elles ne dégradent pas la compréhension du plan, mais restent comptées dans l’intervention humaine tant qu’elles ne sont pas rattachées. GeoFlow propose un lot pour une annexe uniquement si un seul numéro de lot retenu est à proximité sur la même planche, ou si, parmi plusieurs, l’annexe touche les contours d’un seul lot et reste nettement séparée des autres ; sinon l’annexe reste ambiguë ou sans proposition. Une proposition n’est jamais appliquée sans action de l’utilisateur.

Dans l’étape Lots & surfaces, chaque annexe se rattache à un lot constitué avec sauvegarde automatique ; un rattachement crée une zone `secondaire_annexe` du lot, avec sa surface et sa provenance, sans fusion avec la surface principale. Le niveau reste « À préciser » tant qu’il n’est pas saisi. Rattacher, déplacer ou détacher une annexe invalide la validation du dossier et ne relit jamais le plan. Les écritures d’un même dossier sont sérialisées.

## Données métier et avertissements

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

Ouvrir <http://localhost:8080>. Les dossiers JSON et les documents générés sont conservés dans le volume Docker `geoflow-data`. Le conteneur fonctionne avec l’utilisateur non privilégié `geoflow`. Le port est publié uniquement sur `127.0.0.1`, sans exposition directe au réseau local.

La racine du conteneur est en lecture seule, toutes les capacités Linux sont abandonnées et l’acquisition de privilèges est interdite. Seuls le volume `/data` et un `/tmp` tmpfs de 512 Mio sont inscriptibles. Le cache et la configuration utilisateur ODA/Qt sont dirigés vers `/tmp`. Le tmpfs conserve la possibilité d’exécuter des fichiers pour rester compatible avec ODA ; sa taille doit être adaptée au volume réel des conversions. La compatibilité DWG de cette configuration doit être validée avec l’installation ODA cible avant activation en production.

L’image contient seulement l’application et ses dépendances runtime, y compris le modèle DOCX, ainsi que le fichier `/app/LICENSE`. Le label OCI `org.opencontainers.image.licenses` indique `AGPL-3.0-only` pour GeoFlow ; les composants tiers conservent leurs licences. Les tests, fixtures, exemples et outils de développement restent hors de l’image. Les dépendances de développement sont dans `requirements-dev.txt`.

Le healthcheck interroge `/health` :

```bash
curl http://localhost:8080/health
docker compose ps
docker compose exec geoflow id
```

## Accès privé optionnel

GeoFlow peut être exposé par un conteneur Cloudflare Tunnel indépendant, activé uniquement avec le profil `tunnel`. Le fonctionnement local standard ne demande aucun compte ni token Cloudflare.

La création du tunnel, le stockage local du token et la protection du hostname avec Cloudflare Access sont décrits dans [la documentation Cloudflare Tunnel](docs/CLOUDFLARE_TUNNEL.md).

## Conversion DWG

ODA File Converter reste un logiciel propriétaire externe, ni téléchargé ni redistribué par GeoFlow et non couvert par sa licence AGPL. Ses conditions d’utilisation doivent être vérifiées séparément, notamment pour un usage commercial ou hébergé ; voir les [composants tiers](docs/THIRD_PARTY_LICENSES.md). Le `compose.yaml` monte en lecture seule l’installation WSL actuellement attendue :

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
.venv/bin/python -m pip install -r requirements-dev.txt
mkdir -p .tmp
TMPDIR="$PWD/.tmp" .venv/bin/python -m pytest -q --basetemp=.tmp/pytest --cov --cov-report=term-missing --cov-report=xml
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
git diff --check
```

Les tests ne sont pas embarqués dans l’image runtime. La [documentation de contribution](CONTRIBUTING.md) décrit la QA synthétique DXF/DWG/DOCX et les dépendances optionnelles.

Les tests couvrent le moteur DXF, les routes HTTP, la conversion DWG isolée, l’assistant, l’autosave, la pagination, l’invalidation après modification, la suppression des dossiers, le contexte documentaire, le contenu et l’hygiène OOXML, la persistance du snapshot et la confidentialité. La v0.4.2 ajoute plus de vingt scénarios synthétiques de réconciliation ; les tests documentaires couvrent le modèle métier rétrocompatible, les parcelles multiples, l’édition des lots, les statuts de millièmes, la règle des 1000, les avertissements actionnables et les marges du DOCX. Les tests versionnés utilisent uniquement des données synthétiques. Les dossiers sous `samples/private/` restent exclus de Git et du contexte Docker.

## Intégration continue

GitHub Actions exécute Ruff avec des règles de correction progressives, les tests publics avec couverture des lignes et branches sous Python 3.12, vérifie les erreurs de whitespace Git et construit l’image Docker à chaque push ou pull request visant `main`. Le rapport `coverage.xml` est conservé en artefact ; aucun seuil arbitraire n’est imposé avant une première mesure CI. Un smoke test DXF/DOCX utilise l’image durcie sans dépendance de test. Dependabot surveille les dépendances Python et GitHub Actions chaque semaine. La CI n’installe pas ODA File Converter et n’utilise aucune donnée privée.

Les recettes métier de référence restent réservées à la QA locale privée. Elles ne font pas partie du socle public automatisé et les éventuels XLSX associés ne constituent pas une dépendance produit.

## Limites volontaires

Le document produit reste un brouillon technique : aucune validation juridique, aucun calcul de millièmes, aucun import de grille depuis XLSX, aucune qualification automatique des droits ou servitudes, aucune table des matières automatique et aucune reproduction exhaustive d’un acte existant. Les millièmes sont uniquement repris des valeurs saisies et validées par l’utilisateur. La reconnaissance parfaite des cartouches et feuilles reste également hors périmètre.

La réconciliation est déterministe et fondée sur des règles prudentes. Elle n’emploie ni apprentissage automatique, ni OCR, ni LLM, ni dépendance géométrique lourde. Les présentations DXF ambiguës ou inconnues restent à confirmer manuellement ; l’absence de proposition automatique est préférée à un faux rapprochement.

## Sécurité et concurrence

Toutes les routes d’écriture partagent une protection same-origin : `Origin`, ou `Referer` en son absence, doit correspondre à l’origine courante. Les écritures depuis un autre site, y compris un sous-domaine voisin, sont refusées. Les formulaires classiques, uploads et autosave ne nécessitent aucun champ supplémentaire. Les clients HTTP sans provenance navigateur restent acceptés s’ils n’envoient aucun cookie ; ce contrôle n’est pas une authentification. Voir [SECURITY.md](SECURITY.md).

Pour un tunnel HTTPS, définir `GEOFLOW_PUBLIC_ORIGIN=https://votre-hostname` avant le démarrage. Cette origine doit être exacte, sans chemin ni slash final. Elle permet la terminaison TLS sans élargir la confiance aux en-têtes proxy d’un client. L’accès HTTP local reste disponible.

ODA, `inspect_dxf()` et la réconciliation d’une analyse rapide ou d’un import s’exécutent dans des threads AnyIO, avec un seul traitement lourd simultané. Une seconde demande peut attendre sans occuper l’event loop ; toute demande supplémentaire reçoit HTTP 429 et `Retry-After: 5` avant la lecture du corps multipart, pour borner aussi les fichiers temporaires. `/health` reste disponible pendant une conversion. Un thread n’est pas abandonné à l’annulation de la requête, afin de conserver ses fichiers temporaires jusqu’à la fin du traitement. Une modification ou une suppression du dossier pendant l’import empêche la sauvegarde d’un résultat devenu obsolète (409 ou 404). Une analyse temporaire supprimée pendant l’application d’un profil n’est jamais recréée (404).

Uvicorn démarre explicitement avec `--workers 1`. Les verrous des dossiers/profils et la limite d’analyse sont locaux au processus : ne pas augmenter le nombre de workers ni partager le volume JSON entre plusieurs instances. Ils ne constituent pas des verrous distribués ni des transactions globales. Les opérations synchrones courtes, les rendus HTML et certaines réconciliations déclenchées par une correction manuelle restent exécutés dans la route ; la réactivité sous charge CPU réelle n’a pas de garantie de latence. Une file de travaux dédiée et une base transactionnelle seraient nécessaires pour changer d’échelle.

## Licence et contributions

GeoFlow est sous **`AGPL-3.0-only`**, sans option « ou toute version ultérieure ». L’utilisation, la modification et la redistribution, y compris commerciales, sont permises sous les conditions de l’[AGPL version 3](LICENSE), notamment la conservation des notices et la fourniture du code source correspondant dans les cas prévus par la licence.

Les obligations s’appliquent aussi aux versions modifiées utilisées via un service réseau : la section 13 impose de proposer de manière visible et sans frais le code source correspondant de cette version aux utilisateurs qui interagissent avec elle à distance. Un accès réservé par Cloudflare Access ne supprime pas cette obligation. La licence ne demande pas de publier les plans clients, données métier ou secrets d’exploitation du seul fait de leur traitement par GeoFlow.

La licence couvre les éléments propres à GeoFlow ; les dépendances Python, ODA et les autres composants tiers conservent leurs licences et notices respectives. Les [contributions acceptées](CONTRIBUTING.md) sont distribuées sous la même licence. Consulter aussi la [sécurité](SECURITY.md), la [revue des licences tierces](docs/THIRD_PARTY_LICENSES.md) et le [rapport de stabilisation](docs/DEPLOYMENT_REVIEW.md).

## Arrêter GeoFlow

```bash
docker compose down
```
