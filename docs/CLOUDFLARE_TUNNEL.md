# Accès privé avec Cloudflare Tunnel

Cette intégration expose GeoFlow sans installer `cloudflared` dans WSL ni dans l’image applicative. Le tunnel fonctionne dans un conteneur indépendant et rejoint GeoFlow sur le réseau Docker interne à l’adresse `http://geoflow:8000`.

Le tunnel est optionnel. GeoFlow reste utilisable localement, sans compte ni token Cloudflare :

```bash
docker compose up -d --build
```

L’accès local reste disponible sur <http://localhost:8080>.

## 1. Créer le tunnel

1. Créer un compte Cloudflare et ajouter un domaine si nécessaire.
2. Ouvrir le tableau de bord Cloudflare Zero Trust.
3. Créer un Cloudflare Tunnel géré à distance et choisir Docker comme environnement.
4. Copier uniquement le token du tunnel proposé par Cloudflare.

La configuration versionnée utilise l’image officielle `cloudflare/cloudflared:2026.8.2`. Les mises à jour automatiques du processus sont désactivées : la mise à jour se fait en changeant explicitement la version de l’image après validation.

## 2. Enregistrer le token localement

Créer le fichier local suivant :

```text
secrets/cloudflare_tunnel_token.txt
```

Le fichier doit contenir uniquement le token, sur une ligne, sans guillemets ni nom de variable. Il est ignoré par Git et par le contexte de construction Docker. Ne jamais le copier dans le fichier Compose, le README, une commande shell partagée ou un ticket.

Sous Linux ou WSL, limiter sa lecture au propriétaire :

```bash
chmod 600 secrets/cloudflare_tunnel_token.txt
```

Le secret est monté en lecture seule sous `/run/secrets/cloudflare_tunnel_token` uniquement dans le conteneur `cloudflared`. GeoFlow ne reçoit pas ce secret.

## 3. Publier GeoFlow côté Cloudflare

Dans la configuration du tunnel, ajouter un hostname public, par exemple `geoflow.example.com`, et renseigner exactement ce service d’origine :

```text
http://geoflow:8000
```

Ne pas utiliser `localhost:8080` : dans le conteneur du tunnel, `localhost` désigne `cloudflared` lui-même.

## 4. Démarrer et vérifier

Démarrer GeoFlow et le tunnel :

```bash
docker compose --profile tunnel up -d --build
docker compose ps
docker compose logs --tail=100 cloudflared
```

Tester ensuite le hostname public configuré. Le statut du tunnel est également visible dans Cloudflare Zero Trust.

Pour arrêter l’ensemble :

```bash
docker compose --profile tunnel down
```

Sans le profil `tunnel`, seul GeoFlow est lancé. La CI, les tests et le build de l’image GeoFlow ne dépendent donc jamais de Cloudflare.

## Protection avec Cloudflare Access

Cloudflare Tunnel transporte les requêtes mais ne remplace pas une politique d’accès. Pour une exposition durable, protéger le hostname avec Cloudflare Access :

```text
Internet
   ↓
Cloudflare Access
   ↓
Authentification
   ↓
Cloudflare Tunnel
   ↓
GeoFlow
```

Pour un test avec un seul géomètre, une politique autorisant uniquement son adresse e-mail est adaptée. Aucun identifiant Cloudflare Access ne doit être stocké dans ce dépôt.

## Isolation et limites

Le conteneur `cloudflared` :

- attend que le healthcheck GeoFlow soit positif ;
- redémarre après une indisponibilité grâce à `restart: unless-stopped` ;
- utilise un système de fichiers racine en lecture seule ;
- abandonne toutes ses capacités Linux et interdit l’acquisition de nouveaux privilèges ;
- accède uniquement à son fichier de token et au réseau Docker partagé ;
- n’accède ni au volume `/data`, ni à ODA, ni aux fichiers DWG, ni aux sources GeoFlow, ni au socket Docker.

Le port `8080` reste publié comme avant sur les interfaces de l’hôte afin de préserver le fonctionnement actuel sous Windows/WSL. Sur un réseau non maîtrisé, utiliser le pare-feu de l’hôte ou valider la compatibilité locale avant de restreindre ce binding à l’adresse loopback.

La suppression ou la reconstruction du conteneur `cloudflared` ne modifie pas le volume métier `geoflow-data`.
