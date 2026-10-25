# Licence de GeoFlow et composants tiers

## Périmètre

Sur décision du propriétaire, les éléments propres à GeoFlow sont distribués sous **GNU Affero General Public License version 3 uniquement**, SPDX **`AGPL-3.0-only`**. [LICENSE](../LICENSE) contient le texte officiel complet obtenu depuis [GNU](https://www.gnu.org/licenses/agpl-3.0.txt), sans modification. Cette déclaration ne remplace aucune licence ni notice d’un composant tiers.

Le fichier GNU copié contient 34 523 octets ; son SHA-256 est `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`. Il comprend les sections 0 à 17 et l’annexe d’application. L’identifiant `AGPL-3.0-only` précise le choix de GeoFlow sans altérer le texte officiel, y compris ses exemples d’application.

`pyproject.toml` ne configure actuellement que les outils du dépôt. Le choix du propriétaire est conservé sous `[tool.geoflow]`, avec `license = "AGPL-3.0-only"`, sans créer de métadonnées de paquet. Il ne s’agit donc pas d’une déclaration de métadonnées de distribution Python au sens de PEP 639 ; une éventuelle création de paquet devra la reprendre dans une section `[project]` complète.

## Dépendances Python

Revue du 25 octobre 2026 : métadonnées et fichiers de licence des distributions installées dans l’environnement local, avec les dépendances transitives actives. Aucune dépendance, version, licence ou notice installée n’a été modifiée.

| Dépendance runtime directe | Version déclarée | Licence constatée |
| --- | --- | --- |
| FastAPI | 0.136.3 | MIT |
| Starlette | 1.10.0 | BSD-3-Clause |
| Pydantic | 2.13.4 | MIT |
| Uvicorn | 0.35.0 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| python-multipart | 0.0.31 | Apache-2.0 |
| ezdxf | 1.4.2 | MIT |
| python-docx | 1.2.0 | MIT |
| AnyIO | 4.14.2 | MIT |

Les 22 distributions de la fermeture runtime locale incluent aussi notamment NumPy, FontTools, lxml, MarkupSafe, h11 et les modules de typage. Les licences principales constatées sont permissives et ne révèlent pas d’incompatibilité évidente avec l’AGPL version 3. Les composants embarqués exigent néanmoins de conserver leurs notices particulières :

- la wheel NumPy locale comporte notamment OpenBLAS et LAPACK, ainsi que des bibliothèques GCC sous `GPL-3.0-or-later WITH GCC-exception-3.1` et libquadmath sous `LGPL-2.1-or-later` ; leurs permissions et obligations restent propres à ces bibliothèques ;
- FontTools conserve des notices de composants BSD et Apache, ainsi que de polices de test amont sous SIL OFL ; ces éléments ne deviennent pas AGPL ;
- lxml conserve ses notices BSD, PSF et celles de ses bibliothèques embarquées ; une mention de runner de tests GPL amont ne signifie pas que l’ensemble de lxml est sous GPL ;
- les outils de développement restent distincts du runtime ; la fermeture locale comprend notamment certifi et pathspec sous MPL-2.0, sans changement de licence.

Cette revue de l’environnement local ne certifie pas toute future résolution transitive ni les binaires d’une image construite sur une autre plateforme. Avant redistribution d’une image, conserver les fichiers de licence/NOTICE des distributions Python et des paquets système, et satisfaire les obligations de sources des composants GPL/LGPL effectivement embarqués. Le fichier `LICENSE` de GeoFlow ne remplace pas ces documents. Les versions transitives ne sont pas verrouillées par ce chantier.

## ODA File Converter

ODA File Converter est **propriétaire, externe et non redistribué**. GeoFlow l’appelle comme exécutable séparé ; le Dockerfile ne le télécharge ni ne l’intègre. Compose monte une installation fournie par l’exploitant en lecture seule. Aucun SDK ou binaire ODA n’est placé sous AGPL.

**Réserve identifiée :** la [FAQ officielle ODA](https://www.opendesign.com/faq/question/what-are-oda-viewer-and-oda-file-converter), consultée le 25 octobre 2026, annonce que les non-membres ne peuvent utiliser ces applications que pour des applications non commerciales. Cela concerne ODA, tandis que l’AGPL de GeoFlow autorise l’usage commercial. Vérifier le contrat de l’installation et les droits accordés par ODA avant un usage commercial, hébergé ou une redistribution ; aucun droit supplémentaire ni exception à l’AGPL n’est accordé ici. L’appel d’un processus séparé ne suffit pas à certifier à lui seul la qualification juridique de toute intégration.

Le mode DXF ne nécessite pas ODA. Son caractère externe et son absence de redistribution ne constituent pas une validation des droits d’utilisation de chaque exploitant.

## Docker, réseau et données

Les futures images construites avec le Dockerfile conservent `/app/LICENSE` et le label OCI `org.opencontainers.image.licenses=AGPL-3.0-only` pour GeoFlow. Python, les paquets système et les dépendances gardent leurs licences ; le conteneur Cloudflared est un composant séparé, non placé sous AGPL par cette décision.

La redistribution de GeoFlow doit respecter les obligations de l’AGPL, notamment la fourniture du code source correspondant dans les cas prévus par la licence. Pour une version modifiée proposée en service réseau, la section 13 impose une offre visible et gratuite du code source correspondant aux utilisateurs qui interagissent avec elle ; un accès authentifié ne supprime pas cette obligation. Ce chantier de licence ne déploie aucun service et ne publie aucune image.

La mise sous AGPL de GeoFlow n’exige pas la publication des plans clients, données métier ou secrets du seul fait de leur traitement. Les contributions acceptées sont distribuées sous la même licence, sans CLA ni transfert de copyright ; voir [CONTRIBUTING.md](../CONTRIBUTING.md).

## Contrôles du changement de licence

- Suite complète : **409 tests réussis**, 38 avertissements de dépréciation de dépendances, couverture globale de **91 %**, avec Python 3.13.5 dans le venv local. Les temporaires et le rapport de couverture sont isolés. L’exécution initiale dans le sandbox a rencontré des timeouts de concurrence ; les 13 tests concernés, puis la suite complète, ont réussi hors sandbox sans modification des sources.
- Ruff : toutes les vérifications configurées réussies. Mypy : aucun problème dans les trois fichiers de son périmètre configuré. Aucun élargissement ou affaiblissement de ces configurations.
- Contrôle de confidentialité : 114 fichiers candidats et 18 parties XML du modèle DOCX inspectés ; aucun motif de secret, chemin de donnée privée ou adresse e-mail non synthétique détecté. Les huit chemins témoins de données et credentials restent ignorés par Git. Ce contrôle par motifs ne constitue pas une certification de toute donnée possible.
- Comparaison des empreintes avant/après : code applicatif, tests, dépendances, modèle DOCX et autres fichiers hors du périmètre licence inchangés. `SECURITY.md` relu sans contradiction avec l’AGPL.
- Liens locaux vérifiés et `git diff --check` propre, y compris un contrôle séparé des nouveaux fichiers. Branche `main`, HEAD et index inchangés. Aucun commit, push, tag, release, build Docker ou déploiement.
