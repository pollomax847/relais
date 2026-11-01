# Docker de Relais

Un seul conteneur, `relais`, sert l'interface http://localhost:8090 : conversion en FIT, bibliothèque d'activités, santé, envois vers les services et sauvegarde de la montre. Voir `outils/LISEZ-MOI.md`.

## Démarrer / arrêter
```bash
cd endurain               # le dossier a gardé son nom
docker compose up -d      # démarrer
docker compose down       # arrêter (les données sont sur le disque, dans le projet)
docker compose logs -f relais
```
L'interface n'est accessible que depuis ce PC. Pour un autre appareil du réseau, remplacez `127.0.0.1:8090:8090` par `8090:8090` dans docker-compose.yml.

Copiez `.env.example` en `.env` : ce fichier indique les chemins de l'ordinateur.
