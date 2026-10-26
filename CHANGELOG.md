# Changelog

## [Unreleased]

Aucun changement depuis `0.5.0-alpha`.

## [0.5.0-alpha] — 2026-10-26

Première préversion publiée sous `AGPL-3.0-only`. Changements depuis `v0.4.3`.

### Ajouté

- Analyse rapide d’un plan DXF/DWG avant toute création de dossier : diagnostic explicable (compréhension, intervention humaine, points à vérifier, lots proposés et leurs preuves), puis création du dossier depuis l’analyse sans relire le fichier.
- Profils de dessin : assistant en cinq étapes prérempli depuis un plan, vérification en direct, états Complet / Utilisable avec avertissements / Incomplet, persistance JSON avec identifiant stable et révision, duplication, suppression, profil par défaut et test sur un autre plan. Les motifs utilisent un mini-format contrôlé, sans expression régulière ni code.
- Choix du profil après le dépôt d’un plan, avec compatibilité mesurée sur le plan et recommandation sans score. « GeoFlow automatique / Legacy » reste toujours disponible ; sans aucun profil, l’analyse automatique est directe.
- Application réelle du profil choisi par la réconciliation, avec complément explicite par le moteur automatique (mode hybride) et provenance `profil` / `legacy` des preuves et des propositions.
- Snapshot des règles du profil (identifiant, nom, révision, empreinte) conservé dans chaque dossier : une modification, un renommage ou une suppression ultérieure du profil ne change pas un dossier déjà analysé.
- Aperçu SVG du plan, mode sombre et choix du thème.
- Référence métier du dossier modifiable et unique, distincte de l’identifiant technique immuable.
- Proposition de lot pour les annexes (repère unique à proximité ou contact avec un seul lot) et rattachement des annexes depuis l’étape Lots & surfaces, jamais appliqué sans action de l’utilisateur.
- Tunnel Cloudflare optionnel (service `cloudflared`, healthcheck, labels de surveillance) et intégration continue GitHub Actions.
- Documents de contribution et de sécurité, templates Issue/PR, version applicative centralisée (`app/version.py`), fixture DXF synthétique déplacée dans les tests.
- Ruff progressif, typage strict des frontières sécurité/concurrence/version, rapport de couverture pytest, QA runtime synthétique et mises à jour Dependabot Python/Actions.

### Modifié

- Refonte de l’accueil, du diagnostic et des étapes du dossier ; feuille de style unique fondée sur des tokens de thème.
- Une seule analyse lourde à la fois : ODA, inspection DXF et analyse du profil sont exécutés en thread, avec au plus une analyse en attente ; contrôle des modifications concurrentes pendant un import.
- Un worker Uvicorn explicite, dépendances runtime/dev séparées, image réduite sans tests ni outils de développement, durcissement Compose (capacités retirées, `no-new-privileges`, racine en lecture seule, `/tmp` en tmpfs).
- Publication du port local sur `127.0.0.1` uniquement.

### Corrigé

- Contours de l’espace objet classés à tort d’après l’emprise des présentations, et lots comptés deux fois lorsqu’une même représentation figure sur plusieurs planches.
- Débordement horizontal sur mobile du fil d’étapes du dossier et des noms de fichiers DOCX générés.

### Sécurité

- Protection same-origin des écritures, en-têtes HTTP de sécurité et Content Security Policy stricte ; script de thème externe compatible CSP.
- Mise à jour compatible de FastAPI, Starlette et du parseur multipart pour intégrer leurs correctifs de sécurité.

### Licence

- Adoption de GNU AGPL version 3 uniquement (`AGPL-3.0-only`) : texte officiel dans `LICENSE`, déclaration SPDX locale dans `pyproject.toml`, documentation des contributions et des composants tiers, `LICENSE` et label OCI dans l’image Docker.

### Compatibilité

- Les dossiers antérieurs restent lisibles, sans migration destructive. Ils sont présentés comme analysés en mode automatique, et leur empreinte de validation est inchangée.
- Les profils au format 1 sont lus comme révision 1. Le modèle DOCX est inchangé.
- ODA File Converter reste un composant propriétaire externe, monté par l’exploitant et non redistribué.

### Limites connues

- La ré-analyse d’un dossier existant avec un autre profil ou une révision plus récente n’est pas proposée.
- GeoFlow n’a pas d’authentification applicative : l’accès distant repose sur Cloudflare Access.

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/).
