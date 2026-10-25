# Changelog

## [Unreleased]

- Adoption de GNU AGPL version 3 uniquement (`AGPL-3.0-only`) : texte officiel dans `LICENSE`, déclaration SPDX locale dans `pyproject.toml`, documentation des contributions et des composants tiers, licence et label SPDX dans les futures images Docker.
- Publication locale sur loopback, protection same-origin des écritures et headers HTTP de sécurité ; script de thème externe compatible CSP.
- ODA, inspection DXF et analyse du profil exécutés en thread, une seule analyse lourde à la fois ; contrôle des modifications concurrentes pendant un import.
- Un worker Uvicorn explicite, dépendances runtime/dev séparées, image réduite et durcissement Compose (capacités, privilèges, racine en lecture seule et `/tmp` en tmpfs).
- Documents de contribution et de sécurité, templates Issue/PR, version applicative centralisée et fixture DXF synthétique déplacée dans les tests.
- Ruff progressif, typage strict des frontières sécurité/concurrence/version, rapport de couverture pytest, QA runtime synthétique et mises à jour Dependabot Python/Actions.
- Mise à jour compatible de FastAPI, Starlette et du parseur multipart pour intégrer leurs correctifs de sécurité.

Cette entrée ne constitue pas une release ; aucun tag n’est créé. La licence est désormais `AGPL-3.0-only`, sur décision du propriétaire. Les règles métier, les versions des schémas et le modèle DOCX sont conservés.

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).
