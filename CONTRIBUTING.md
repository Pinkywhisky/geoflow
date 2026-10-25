# Contribuer à GeoFlow

GeoFlow est distribué sous **GNU Affero General Public License version 3 uniquement (`AGPL-3.0-only`)** ; le texte complet est dans [LICENSE](LICENSE). Les contributions acceptées dans GeoFlow sont distribuées sous cette même licence. Les contributeurs conservent leurs droits d’auteur ; aucun CLA ni transfert de copyright n’est demandé.

Toute contribution doit respecter les licences et notices des composants tiers qu’elle utilise. La licence de GeoFlow ne modifie pas celles des dépendances Python ou d’ODA File Converter, qui reste propriétaire, externe et non redistribué. Voir la [revue des licences tierces](docs/THIRD_PARTY_LICENSES.md).

## Installation et vérifications

Python 3.12 est la référence CI. Depuis la racine du projet :

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
mkdir -p .tmp
TMPDIR="$PWD/.tmp" .venv/bin/python -m pytest -q --basetemp=.tmp/pytest --cov --cov-report=term-missing --cov-report=xml
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
git diff --check
```

`requirements.txt` contient uniquement les dépendances runtime ; `requirements-dev.txt` ajoute les outils de développement. Ruff commence par les erreurs de syntaxe et de correction (`E9`, `F63`, `F7`, `F82`) pour éviter une remise en forme générale. Le rapport de couverture inclut les branches ; un seuil sera fixé après mesure d’une première référence CI. Mypy vérifie strictement les nouveaux modules de sécurité, concurrence et version ; son périmètre sera étendu progressivement au stockage et aux routes.

## Proposer un changement

Créer un fork puis une branche, décrire le problème et le comportement obtenu dans une PR, puis indiquer les vérifications effectuées et leurs limites. Préserver la provenance des données, les décisions humaines et la compatibilité des JSON existants. Accompagner les changements de sécurité, de concurrence ou de logique par des tests ciblés.

Utiliser exclusivement des exemples synthétiques. Exclure les plans clients, dossiers réels, tokens, fichiers `.env`, identifiants et documents privés. Ne jamais joindre de logs contenant ces données aux issues ou PR.

La version applicative est définie dans `app/version.py`. Les versions du schéma JSON, des règles de réconciliation et du modèle DOCX sont indépendantes. Décrire les changements dans `CHANGELOG.md`, sous « Unreleased ».

## QA runtime

L’image runtime exclut les tests et les fixtures. Les validations s’exécutent sur l’hôte ou en montant explicitement les outils et les fixtures synthétiques en lecture seule :

```bash
mkdir -p .tmp/qa
TMPDIR="$PWD/.tmp" .venv/bin/python tools/qa_runtime.py --fixtures tests/fixtures --output .tmp/qa
```

Avec ODA installé, ajouter `--make-dwg` pour générer un DWG synthétique puis vérifier sa reconversion. Un échec ou une absence d’ODA ne valide pas la QA DWG. Voir le [rapport de préparation](docs/DEPLOYMENT_REVIEW.md) pour les validations restantes dans l’environnement cible.
