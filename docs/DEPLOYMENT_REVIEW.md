# Rapport de stabilisation GeoFlow

Contrôle réalisé le 25 octobre 2026, sur `main`, à partir de `c11bcb24bc3016b5591c41be1316743f9a8df2ee`. Les modifications déjà présentes à l’ouverture du chantier ont été conservées et revues. Aucun commit, push, tag ou release n’a été créé.

Mise à jour licence du 25 octobre 2026 : le propriétaire a choisi **`AGPL-3.0-only`**. Le texte officiel complet est désormais dans [LICENSE](../LICENSE). Cette mise à jour porte sur les déclarations, les notices et la documentation ; le Dockerfile ajoute la licence et un label SPDX aux futures images, sans reconstruction ni redéploiement dans ce chantier. Les validations et constats de déploiement ci-dessous décrivent le chantier de stabilisation antérieur à cet ajout.

1. **Audit avant modification.** Inspection de Compose, Dockerfile, ignores, dépendances, CI, README, routes, conversion ODA, inspection DXF, temporaires, persistance atomique et verrous. L’état de référence exposait `8080` sur toutes les interfaces, exécutait ODA/DXF dans les routes async, n’avait pas de protection CSRF ni de headers applicatifs et embarquait tests/outils/fixtures dans le runtime. Les numéros de version étaient dupliqués. Aucune licence du projet n’a été trouvée. Des avis de sécurité concernaient l’ancien Starlette et le parseur multipart ; les versions concernées ont été remplacées.

2. **Exposition réseau avant/après.** Avant : `0.0.0.0:8080` et `[::]:8080`. Après : uniquement `127.0.0.1:8080:8000`, constaté dans Compose et dans le conteneur réel. Les connexions vers le port 8080 sur les cinq adresses IPv4 non loopback de l’hôte sont refusées. Aucune variable ne réouvre implicitement le LAN. Le réseau interne et le hostname Cloudflare restent indépendants de ce port publié.

3. **Protection same-origin.** Middleware ASGI commun à toutes les écritures, avant lecture du corps : comparaison exacte schéma/hôte/port de `Origin`, ou de `Referer` en son absence ; refus des origines opaques, valeurs malformées, headers de provenance dupliqués et `Sec-Fetch-Site` autre que `same-origin`. Les requêtes sans provenance et sans cookie restent possibles pour les outils HTTP ; ce contrôle ne constitue pas une authentification. L’origine HTTPS exacte du tunnel est explicitement configurée par `GEOFLOW_PUBLIC_ORIGIN`, sans faire confiance aux headers proxy fournis par un client. Sa valeur a été conservée dans le `.env` local ignoré, sans secret ajouté au dépôt. Une requête provenant de cette origine atteint une route inexistante (404), tandis qu’une origine étrangère est arrêtée avant la route (403). Les formulaires, autosaves et réponses JSON ont été vérifiés. Les GET de douze écrans ne changent aucun JSON métier dans le test dédié.

4. **Headers HTTP.** `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, `Permissions-Policy` minimale et CSP sans `unsafe-inline` pour l’interface : scripts/styles/connexions/formulaires limités à l’origine, objets et frames interdits. Le script de thème est désormais externe. Les trois pages de documentation générées par FastAPI (`/docs`, `/redoc`, `/docs/oauth2-redirect`) sont seules exemptées de CSP pour préserver leurs scripts/CDN ; elles gardent les autres headers. Aucun HSTS n’est envoyé aveuglément sur l’accès HTTP local. Les quatre modes navigateur ne montrent aucune violation CSP.

5. **Event loop avant/après.** Les conversions ODA, `inspect_dxf()`, la préparation des analyses, l’import et l’application du profil passent par `anyio.to_thread.run_sync`. Le contexte de conversion et sa durée de vie restent dans le travail synchrone. Les fonctions et algorithmes métier ne sont pas modifiés. Les fichiers temporaires sont nettoyés en cas d’erreur, d’annulation pendant la lecture ou d’annulation pendant leur fermeture.

6. **Concurrence.** Un seul traitement lourd simultané. Une seule demande supplémentaire peut attendre ; la troisième reçoit 429 avec `Retry-After: 5`, avant consommation du corps multipart. Cette admission borne les uploads en attente en complément du limiteur de threads et du tmpfs. Les slots sont libérés même sur exception. Un import ne réécrit pas un dossier modifié ou supprimé pendant l’analyse (409/404) ; une analyse temporaire supprimée pendant application d’un profil n’est pas recréée (404). Tous les limiteurs et verrous sont locaux au processus.

7. **Disponibilité de `/health`.** Tests concurrents sur le même event loop, bloquant successivement les frontières ODA et inspection réelle des routes d’upload/import/profil. `/health` répond pendant le blocage et la seconde analyse reste en attente. En navigateur sur le runtime Docker final, une conversion ODA réelle a été précédée d’un délai QA de deux secondes : trois réponses `/health` ont pris 11.28, 1.72, 1.43 ms. Le wrapper de délai n’existe que dans le conteneur QA temporaire.

8. **Durcissement Docker.** Racine en lecture seule, `/tmp` en tmpfs de 512 Mio (`nosuid,nodev`, mode 1777), `/data` persistant, utilisateur `geoflow` non privilégié, `cap_drop: ALL` et `no-new-privileges`. ODA, Xvfb, uploads, persistance et DOCX fonctionnent réellement avec ces contraintes. Les caches/configurations XDG et le répertoire personnel de cet utilisateur vont sous `/tmp`. Uvicorn démarre explicitement avec un worker. Un futur multi-worker exigera stockage et verrouillage partagés.

9. **Contenu runtime.** Copie de `app/` et de `requirements.txt`, en conservant le template DOCX public nécessaire. Aucun test, sample, fixture, outil QA, répertoire Git, credential ou fichier client dans l’image. Le contrôle exécute des imports négatifs dans l’image réelle pour pytest, httpx, Ruff, Mypy, Playwright et coverage. Les 55 fichiers applicatifs/dépendances déployés ont les mêmes empreintes que les fichiers locaux. Un multi-stage n’apporterait ici aucun retrait de compilateur ou d’artefact de build : les dépendances utilisent des wheels et les outils de développement ne sont jamais installés dans le stage runtime.

10. **Dépendances runtime/dev.** `requirements.txt` contient le socle applicatif ; `requirements-dev.txt` ajoute pytest, httpx, PyYAML, pytest-cov, Ruff et Mypy. Les extras Uvicorn destinés notamment au rechargement et aux WebSockets inutilisés ne sont plus requis. FastAPI 0.136.3, Starlette 1.10.0 et python-multipart 0.0.31 intègrent les correctifs retenus après vérification des contraintes et avis PyPI ; les autres dépendances directes sont conservées. `pip check` passe. Playwright a servi à la QA locale, sans être ajouté au runtime ni imposé aux contributeurs.

11. **Préparation open source.** Ajout de `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, templates de bug/proposition/PR et documentation de QA synthétique. Le signalement de sécurité oriente vers le formulaire privé GitHub lorsqu’il est activé, sinon une demande de contact privé sans détail public ; aucune adresse de sécurité n’est inventée. La seule branche maintenue annoncée est `main`. Les contributions passent par fork/branche/PR et excluent toute donnée client ou DWG privé.

12. **Licence adoptée : `AGPL-3.0-only`.** Sur décision du propriétaire, GeoFlow est sous GNU Affero General Public License version 3 uniquement, sans option « ou toute version ultérieure ». [LICENSE](../LICENSE) reproduit intégralement le texte officiel [GNU](https://www.gnu.org/licenses/agpl-3.0.txt), sans modification. La déclaration SPDX figure sous `[tool.geoflow]` dans `pyproject.toml`, ce fichier restant dédié à la configuration des outils, sans métadonnées de paquet Python.

    L’utilisation, la modification et la redistribution, y compris commerciales, sont permises sous les conditions de l’AGPL. Une version modifiée utilisée via un service réseau doit proposer de manière visible son code source correspondant aux utilisateurs concernés, conformément à la section 13. Les contributions acceptées sont distribuées sous la même licence, sans CLA ni transfert de copyright. Les plans clients, données métier et secrets ne deviennent pas publics du seul fait de leur traitement par GeoFlow.

    Les dépendances Python conservent leurs licences. La [revue des composants tiers](THIRD_PARTY_LICENSES.md) ne relève pas d’incompatibilité évidente dans la fermeture Python locale examinée ; les notices et obligations particulières des bibliothèques embarquées restent applicables. ODA demeure propriétaire, externe et non redistribué. Sa FAQ limite l’usage des non-membres aux applications non commerciales : vérifier les droits de l’installation avant un usage commercial ou hébergé. Cette réserve propre à ODA ne restreint pas la licence AGPL de GeoFlow.

13. **Version.** Source unique Python : `app/version.py`, valeur de développement `0.5.0-alpha`. FastAPI et le footer l’utilisent ; le README pointe vers cette source sans recopier un numéro. Les versions de schémas, de règles de réconciliation, des profils et du template documentaire ne changent pas. Aucun tag de cette version n’est créé.

14. **Nettoyage du dépôt.** Recherche des usages avant déplacement et vérification des candidats à publication. Les fichiers utiles sont conservés, dont le générateur de fixture et le template DOCX. `.gitignore` protège données privées/runtime, temporaires, résultats de couverture, caches, `.env`, credentials locaux et contenu de `secrets/` sauf son placeholder. `.dockerignore` exclut également tests/outils/samples, documentation et fichiers de développement du contexte runtime. Le test de confidentialité lit uniquement les candidats Git, sans parcourir les secrets ou données runtime ignorés.

15. **DXF à la racine.** `geoflow_plan_test_source.dxf` est un dessin synthétique construit par `generate_geoflow_dwg.build_source_dxf()`. Il a été déplacé sans modification d’octet dans `tests/fixtures/`, et le générateur, les tests et la QA utilisent ce chemin. Ses quatre contours 2D, ses surfaces exactes et son arc bulge ont été vérifiés ; aucune donnée client n’est utilisée.

16. **CI avant/après.** Avant : dépendances, pytest, whitespace du commit et build Docker. Après : dépendances dev séparées, Ruff, Mypy ciblé, tests avec couverture XML, artefact de couverture, comparaison whitespace depuis la base PR/push, build et smoke test de l’image réellement durcie avec DXF/DOCX et audit de son contenu. Le checkout récupère la base nécessaire sans conserver de credentials, et le cache dépend des manifests runtime/dev et de la configuration. Les actions checkout/setup-python/upload-artifact utilisent v7. La CI reste indépendante d’ODA et des données privées. Elle n’a pas été déclenchée sur GitHub puisqu’aucun push n’est autorisé ici.

17. **Ruff.** `ruff check .` passe. Socle progressif de correction `E9`, `F63`, `F7`, `F82`, sans reformatage massif ni neutralisation globale du lint. Il détecte syntaxe, variables non définies et erreurs de contrôle. L’extension progressive aux imports inutilisés pourra suivre séparément.

18. **Typage.** `mypy` passe en mode strict sur les trois nouveaux modules sécurité, concurrence et version. Les imports externes restent typés, sans basculer tout le modèle métier et ses structures historiques en migration globale. Prochaine étape : étendre ce périmètre aux repositories puis aux routes.

19. **Couverture.** 409 tests, avec couverture des branches : 93.27 % des lignes, 83.67 % des branches, 91.08 % combinés (91 % arrondi dans pytest-cov). Les rapports XML/JSON sont locaux ou des artefacts CI, ignorés par Git. Aucun seuil artificiel n’est imposé.

20. **Dependabot.** Configuration hebdomadaire pour pip et GitHub Actions, nombre de PR limité et regroupement Python. Aucun merge automatique.

21. **Tests et autocontrole.** 409 tests passants contre 296 au socle initial, soit 113 cas supplémentaires de sécurité, concurrence et déploiement, dont des cas paramétrés. Les attentes métier existantes sont conservées. Des régressions ont été réellement introduites puis restaurées octet pour octet : binding `0.0.0.0` → un test échoue ; travail lourd direct dans l’event loop → un test échoue ; bypass same-origin → quatorze tests échouent. La suite correcte complète repasse ensuite. Les 38 avertissements proviennent des dépréciations Pyparsing/ezdxf et du TestClient Starlette avec httpx ; ils ne masquent pas d’échec.

22. **QA DXF.** Fixture synthétique, calcul des quatre surfaces dont bulge exact, import, diagnostic, choix de profil et mode Legacy. Tests de la vraie limite de 50 Mio : accepté à la borne, refusé à +1 octet avant inspection ; extension invalide, noms neutralisés, traversée de chemin et suppression sûre couverts par le socle et les nouveaux contrôles. Douze pages GET sont vérifiées sans écriture métier.

23. **QA DWG/ODA.** Génération d’un DWG synthétique avec l’installation ODA réelle, reconversion via le code runtime, surfaces et unités conformes. Dépôt du DWG au navigateur dans une instance Docker durcie isolée, choix Legacy et diagnostic validés. ODA n’étant pas installé dans l’exécuteur hôte, aucune réussite DWG hôte n’est revendiquée ; la validation réelle utilise ses montages Docker habituels.

24. **QA DOCX.** Génération réelle dans l’image durcie, OOXML valide, avertissement de brouillon, absence de relations externes, persistance du snapshot et document. Dans Chromium : validation d’un dossier synthétique, génération et téléchargement d’un DOCX relu comme archive ZIP valide. Les autres tests documentaires et de reproductibilité existants passent.

25. **QA Cloudflare.** Cloudflared est toujours healthy. Sa configuration effective observée cible `http://geoflow:8000`. Une requête depuis son espace réseau atteint `/health` avec 200. Le runtime accepte l’origine publique HTTPS configurée sans accepter une origine étrangère. L’entrée externe anonyme reçoit 403 avec des headers Cloudflare ; cela prouve le refus de cette requête, pas un parcours Access authentifié. Aucune configuration Cloudflare Access n’a été modifiée. Une session authentifiée reste nécessaire pour valider le parcours public complet.

26. **Docker final.** `docker compose up -d --build --wait --wait-timeout 90` terminé ; GeoFlow et Cloudflared healthy, runtime minimal déployé, binding uniquement loopback. La QA Chromium finale couvre onze écrans dans chaque combinaison desktop/mobile (1440/390 px) et clair/sombre, avec interactions, profil, référence, lots, annexes, validation, suppression de profil et document. Aucun débordement horizontal, aucune erreur JS ni violation CSP. Des captures synthétiques ont été inspectées ; elles restent hors du dépôt.

27. **Health.** Local : `http://127.0.0.1:8080/health` → 200 et `{"status":"ok"}`. Interne tunnel : même résultat. Les headers de sécurité sont présents sur pages, statiques, erreurs gérées, refus same-origin et refus d’admission.

28. **Fichiers modifiés, ajoutés et supprimés.** Inventaire ci-dessous. La seule suppression de contenu à la racine est le déplacement de la fixture DXF, conservée à l’identique. Aucun algorithme métier, schéma ou template DOCX n’a changé. `.env` est une configuration locale ignorée, absente de cet inventaire.

29. **Limites restantes.** Pas d’authentification applicative ni d’isolation multi-utilisateur ; Cloudflare Access et la confiance dans l’hôte restent nécessaires. Verrous/processus uniques, file très courte et tmpfs borné conviennent à cette instance. Les rendus et certaines corrections métier synchrones restent dans les routes ; les threads Python n’offrent pas de garantie stricte de latence CPU. La limite applicative porte sur le fichier retenu après le parsing multipart ; l’admission et le tmpfs bornent la pression locale, sans remplacer des limites de requête au frontal contre du trafic volumétrique. Les dépendances directes sont épinglées, sans lock transitif ni digest d’image de base garantissant un build identique bit à bit. Le parcours public Access authentifié et l’exécution CI distante restent à confirmer. La licence est désormais `AGPL-3.0-only` ; les droits d’usage d’ODA et les obligations de redistribution des composants tiers restent à vérifier pour chaque distribution cible.

30. **Suites recommandées.** Respecter les obligations AGPL de fourniture des sources pour la distribution ou le service réseau modifié, confirmer les droits ODA, le signalement privé GitHub et une session Access réelle, puis autoriser séparément le commit et la CI distante. Étendre progressivement lint/typage et suivre la couverture mesurée. Prévoir un lock reproductible et une revue régulière des dépendances et d’ODA ; migrer le TestClient vers son futur client supporté lorsqu’utile. Avant plusieurs workers, adopter un stockage et des verrous partagés. Aucune de ces opérations n’est exécutée implicitement.

31. **Git status.** `main`, HEAD initial inchangé, index vide, changements non commités laissés pour revue. Inventaire exact ci-dessous, complété par ce rapport lui-même. Aucun push, tag ou release.

32. **Whitespace et confidentialité.** `git diff --check` propre. Vérification des noms et du contenu des fichiers candidats sans imprimer de valeurs sensibles, et règles d’ignore contrôlées pour le token, les données, échantillons privés, `.env` et credentials. Aucun secret ni donnée privée détecté parmi les candidats audités. Les captures, temporaires, sorties QA et rapports de couverture ne sont pas ajoutés à Git.

## Inventaire Git

Modifications et suppression vues depuis HEAD :

```text
M	.dockerignore
M	.github/workflows/ci.yml
M	.gitignore
M	Dockerfile
M	README.md
M	app/main.py
M	app/templates/_head.html
M	app/templates/index.html
M	compose.yaml
M	docs/CLOUDFLARE_TUNNEL.md
M	generate_geoflow_dwg.py
D	geoflow_plan_test_source.dxf
M	requirements.txt
M	tests/test_cloudflare_tunnel.py
M	tests/test_v04_confidentiality.py
```

Nouveaux fichiers (non suivis, non stagés) :

```text
.github/ISSUE_TEMPLATE/bug_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml
.github/dependabot.yml
.github/pull_request_template.md
CHANGELOG.md
CONTRIBUTING.md
SECURITY.md
app/concurrency.py
app/security.py
app/static/theme.js
app/version.py
pyproject.toml
requirements-dev.txt
tests/fixtures/README.md
tests/fixtures/geoflow_plan_test_source.dxf
tests/test_concurrency.py
tests/test_security.py
tools/qa_runtime.py
docs/DEPLOYMENT_REVIEW.md
```

`git status --short` au contrôle :

```text
 M .dockerignore
 M .github/workflows/ci.yml
 M .gitignore
 M Dockerfile
 M README.md
 M app/main.py
 M app/templates/_head.html
 M app/templates/index.html
 M compose.yaml
 M docs/CLOUDFLARE_TUNNEL.md
 M generate_geoflow_dwg.py
 D geoflow_plan_test_source.dxf
 M requirements.txt
 M tests/test_cloudflare_tunnel.py
 M tests/test_v04_confidentiality.py
?? .github/ISSUE_TEMPLATE/
?? .github/dependabot.yml
?? .github/pull_request_template.md
?? CHANGELOG.md
?? CONTRIBUTING.md
?? SECURITY.md
?? app/concurrency.py
?? app/security.py
?? app/static/theme.js
?? app/version.py
?? pyproject.toml
?? requirements-dev.txt
?? tests/fixtures/README.md
?? tests/fixtures/geoflow_plan_test_source.dxf
?? tests/test_concurrency.py
?? tests/test_security.py
?? tools/qa_runtime.py
?? docs/DEPLOYMENT_REVIEW.md
```

## Références de contrôle

- [AnyIO : threads, annulation et limiteurs](https://anyio.readthedocs.io/en/stable/threads.html).
- [Avis Starlette sur FileResponse](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8) et [notes de versions Starlette](https://starlette.dev/release-notes/).
- Métadonnées et avis des versions retenues : [FastAPI](https://pypi.org/pypi/fastapi/0.136.3/json), [Starlette](https://pypi.org/pypi/starlette/1.10.0/json), [python-multipart](https://pypi.org/pypi/python-multipart/0.0.31/json).
- [GitHub : signalement privé](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).
- [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).
