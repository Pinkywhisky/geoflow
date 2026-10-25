# Sécurité

La branche `main` est la seule version maintenue pour le moment. Le projet est un MVP ; il ne fournit pas d’authentification applicative ni d’isolation entre utilisateurs.

## Signaler une vulnérabilité

Utiliser le signalement privé GitHub (« Security → Report a vulnerability ») lorsqu’il est activé sur ce dépôt. S’il est indisponible, ouvrir une demande de contact privé sans détailler la vulnérabilité. Ne publier ni exploit, ni plan client, ni donnée cadastrale réelle, ni token dans une issue publique.

Fournir en privé le commit concerné, les conditions de reproduction sur des données synthétiques et l’impact observé. Aucun délai de réponse contractuel n’est annoncé.

## Déploiement

- Le port hôte est limité à `127.0.0.1:8080` ; Cloudflared rejoint `http://geoflow:8000` sur le réseau Docker.
- Protéger le hostname public avec Cloudflare Access. Le contrôle same-origin protège les écritures du navigateur ; il ne remplace pas le contrôle d’accès.
- Définir `GEOFLOW_PUBLIC_ORIGIN=https://votre-hostname` pour la terminaison TLS du tunnel, sans chemin ni slash final. Les en-têtes `X-Forwarded-*` fournis par un client ne sont pas utilisés pour autoriser une origine.
- Chaque méthode d’écriture vérifie `Origin`, ou `Referer` en son absence. Les requêtes `Sec-Fetch-Site` autres que `same-origin`, les origines opaques (`null`) et les origines différentes sont refusées avant lecture du corps. Une requête portant un cookie doit fournir une provenance vérifiable.
- Les outils HTTP sans provenance navigateur et sans cookie restent utilisables sur une origine protégée ; ils ne bénéficient d’aucune authentification implicite. Si une authentification applicative est ajoutée, réévaluer ce contrat et ajouter des tokens CSRF liés à la session.
- Les scripts, styles, formulaires et connexions de l’interface sont limités par CSP à l’origine courante. Les pages de documentation générées par FastAPI (`/docs`, `/redoc`, `/docs/oauth2-redirect`) conservent leurs ressources CDN et scripts intégrés : elles sont exemptées de CSP, tout en gardant les autres headers. L’intégration dans un frame et les objets embarqués sont interdits. Les headers `nosniff`, `Referrer-Policy: same-origin` et `Permissions-Policy` complètent ces protections. HSTS relève du frontal HTTPS : l’accès HTTP local reste prévu.
- Conserver un seul worker et une seule instance par volume JSON. Les verrous et la limite d’analyse sont locaux au processus (voir [README](README.md)).
- Une analyse lourde est exécutée à la fois, avec une seule demande en attente. Les suivantes sont refusées (429) avant lecture des uploads ; cette borne complète le tmpfs de 512 Mio et la limite de fichier de 50 Mio.
- Protéger et sauvegarder `/data` : dossiers, analyses temporaires et documents contiennent des informations métier. La lecture seule de la racine du conteneur ne chiffre pas ce volume.

ODA est une dépendance externe distincte. GeoFlow ne le télécharge pas et ne le redistribue pas. Garder ODA et les images à jour ; ne jamais accepter de données privées dans les fixtures publiques.
