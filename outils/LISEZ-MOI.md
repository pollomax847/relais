# Relais

## Organisation du projet
| Dossier | Contenu |
|---|---|
| `sources-brutes/` | **tous vos exports d'origine**, archivés : `sources-brutes.zip` (99 353 fichiers, vérifiés un par un par leur empreinte, voir `liste.csv`) et `zips/` (les gros exports déjà en ZIP : Runtastic 2022, Google Takeout, Withings, Moves…). L'outil les lit directement, sans les décompresser |
| `activites-v2/` | **le résultat** : un FIT par sortie réelle, rangé par catégorie puis par année (`marche/`, `course/`, `course-tapis/`, `randonnee/`, `velo/`, `velo-interieur/`, `natation-piscine/`, `natation-eau-libre/`, `musculation/`…), avec `sommeil/sommeil.csv`, `index.csv` (provenance et remarques de chaque activité) et `ecartees.csv` (chaque séance écartée et pourquoi) |
| `sante/` | pas, poids et sommeil, une valeur par jour (CSV) ; `montre.csv` : la journée vue par la montre (pas, FC de repos, min, moyenne et max, stress, minutes d'intensité) ; `garmin.csv` : le résumé de chaque journée vu par Garmin Connect (FC, stress, Body Battery, minutes d'intensité, calories) |
| `montre-garmin/` | sauvegardes de la montre branchée en USB |
| `vers-montre/` | parcours et entraînements en attente du prochain branchement (`relais.py vers-montre`) |
| `garmin-connect/` | copie des pas du jour, des nuits, des pesées et des résumés de journée de Garmin Connect, un fichier par année, et le détail des journées dans `minute/` ; mise à jour par `relais.py sante` (lecture seule). Les activités de la montre copiées de Garmin vont dans `montre-garmin/Activity/garmin-connect/` |
| `analyse/` | `ANALYSE.md` : ce que contient chaque source et les règles de conversion |
| `endurain/` | le `docker compose` de l'interface `relais` |
| `outils/` | le code ; `historique-v1/` garde la trace de ce que l'ancienne version a envoyé à Garmin |

Pour ajouter un nouvel export : déposez son dossier ou son ZIP dans `sources-brutes/`, puis lancez « Reconvertir ».

Ces emplacements sont ceux par défaut. Pour les changer, par exemple pour lire vos exports sur un autre disque, copiez
`chemins.exemple.yml` en `chemins.yml` à la racine du projet et indiquez-y vos dossiers : `donnees` (exports lus en
entier), `sources` (autres dossiers à lire), `activites`, `sante`, `montre` et `corbeille`. Avec l'interface Docker, un
dossier hors du projet doit aussi être monté dans `endurain/docker-compose.yml`.

## Interface : http://localhost:8090
Six écrans, à garder en favori ou à ouvrir directement (`#activites`, `#sante`, `#profil`, `#reglages`, `#aide`) :
- **Accueil** : forme, VO2 max estimé, niveau, conseils chiffrés, dernière sortie, l'entraînement par semaine, par mois ou par année, puis les courbes, les calculs et les records.
- **Activités** : les cartes des sorties ou la liste, à filtrer par catégorie, par année ou par texte, avec les totaux de la sélection. Chaque sortie a sa page : temps en mouvement et allure sans les arrêts, vitesses moyenne et maximale, carte (fond OpenStreetMap), ce qu'elle a de remarquable et son classement, fréquence cardiaque, vitesse, altitude, cadence, puissance, zones, temps de passage, sorties les plus rapides sur la même distance, tours et téléchargement du FIT.
- **Santé** : pas, sommeil, poids, FC de repos, stress et Body Battery sur 1 an, 3 ans ou tout l'historique, et une journée en détail.
- **Profil** : objectifs et leur avancement, matériel et composants avec leur kilométrage, cibles de santé, et le compte Garmin (records, badges, appareils, entraînements).
- **Réglages** : mises à jour, ajout d'une activité (FIT, GPX, TCX), envois vers les services (chacun tient son journal, `outils/.envoyes-<service>-v2.txt` : fichier, statut, départ UTC et durée), compte Garmin, et le journal de la commande en cours.
- **Aide** : ce que veut dire chaque chiffre, en français simple et avec vos propres valeurs.

Après une modification du code : `docker restart relais`.

## Commandes
```bash
outils/relais.py preparer                 # conversion précise → activites-v2/ (+ contrôle de chaque FIT)
outils/relais.py sante                    # pas, poids, sommeil → sante/ ; pesées → Garmin
outils/relais.py garmin                   # nouvelles activités de la montre chez Garmin, puis bibliothèque refaite
outils/relais.py envoyer intervals        # tout ce qui reste vers intervals.icu (garmin, strava, runalyze, smashrun)
outils/relais.py envoyer garmin 50        # seulement 50 ; --reessayer retente aussi les refusées
outils/relais.py lots runalyze            # activités pas encore envoyées → a-importer/runalyze/lot-N, à importer sur le site
outils/relais.py lots runalyze --importes # une fois l'import fait : notées envoyées, lots rangés dans a-supprimer/
outils/relais.py montre                   # sauvegarde de la montre (lancé tout seul au branchement)
outils/relais.py auto                     # montre + conversion + santé + envois choisis
outils/relais.py refaire garmin           # retire de Garmin ce que l'ancienne version y a envoyé
outils/relais.py retirer garmin           # retire ce que l'outil a envoyé et qui n'est plus dans la bibliothèque (garmin, smashrun ; --essai pour voir)
```
Une seule commande longue tourne à la fois, que ce soit dans le terminal ou l'interface. Le verrou est `outils/.verrou`.

**Limites d'envoi des services** (vérifiées le 27/10/2026) :
- Strava autorise 200 requêtes par 15 min et 2 000 par jour.
- intervals.icu autorise 5 000 requêtes par jour avec une clé personnelle.
- Garmin ne publie pas de limite.
- Runalyze n'annonce aucune limite, mais Cloudflare bloque l'API après quelques centaines d'envois. Pour un gros
  historique, utilisez `lots` et l'import du site : 1 000 fichiers au plus à la fois, doublons détectés par l'heure.

Pour les nouvelles activités, Tapiriik (lien en haut de l'interface) peut synchroniser les services entre eux.

## Sur le téléphone
Relais reste sur l'ordinateur ; le téléphone l'ouvre à travers Tailscale, sans rien exposer sur Internet. Une seule fois, sur l'ordinateur :
```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8090
```
Sur le téléphone, avec Tailscale activé, ouvrez `https://<nom de l'ordinateur>.<votre réseau>.ts.net:8443` dans Chrome, puis « Ajouter à l'écran d'accueil ». N'utilisez pas `tailscale funnel` : Relais n'a pas à être public. L'ordinateur doit être allumé.

À la maison, Tailscale n'est pas nécessaire. Choisissez d'abord le mot de passe, ajoutez `RELAIS_ECOUTE=0.0.0.0` dans `endurain/.env`, puis lancez `docker compose up -d relais` dans `endurain/`.

Chrome n'installe une application que sur une adresse qu'il juge sûre. Sur le téléphone, connecté au même Wi-Fi :
1. Dans Chrome, ouvrez `chrome://flags/#unsafely-treat-insecure-origin-as-secure`.
2. Dans la case, écrivez `http://<adresse de l'ordinateur>:8090`, choisissez « Enabled » et relancez Chrome.
3. Ouvrez cette adresse, connectez-vous, puis choisissez « Installer l'application » dans le menu ⋮ de Chrome.

Cette connexion n'est pas chiffrée : gardez-la pour un réseau de confiance.

Le mot de passe se choisit dans Réglages. Il est demandé sur chaque appareil, une fois par mois. Seule son empreinte est gardée, dans `~/.config/sport/acces.json`. En cas d'oubli : `outils/relais.py mot-de-passe`, dans un terminal (`--retirer` pour s'en passer).

## Destinations
| Service | Ce qu'il faut | Remarques |
|---|---|---|
| **Garmin Connect** | connexion dans l'interface (une fois) | mode import. **Les services reliés à Garmin (Strava…) reçoivent aussi les activités** : coupez la liaison pendant l'envoi de l'historique |
| **Strava** | une application sur https://www.strava.com/settings/api (« Authorization Callback Domain » : `localhost`), puis son Client ID et son Secret dans l'interface, et le bouton d'autorisation | limite d'envois de Strava : l'outil s'arrête proprement et reprend plus tard |
| **intervals.icu** | clé d'API (intervals.icu → Settings → Developer Settings) | gratuit ; refuse lui-même les doublons |
| **Runalyze** | jeton « Personal API » (Réglages, avec date d'expiration) | Runalyze ne publie pas le chemin d'envoi : il est vérifié au premier fichier, et l'outil s'arrête si Runalyze le refuse |
| **Smashrun** | bouton « Autoriser Smashrun » dans l'interface : connexion à Smashrun, le jeton est récupéré tout seul (12 semaines, recliquer ensuite) | course à pied avec distance seulement ; avant d'envoyer, l'outil lit les courses déjà sur le compte (venues de Garmin, Strava…) et ne les renvoie pas ; sans trace, la course part en JSON ; 250 envois par heure |

Decathlon n'accepte pas l'envoi de fichiers : reliez-le à Garmin.

Dans l'interface, chaque service a un seul bouton : tant que les champs sont vides, il ouvre la page du service où
l'on obtient la clé (ou autorise directement, pour Smashrun) ; une fois les champs remplis, il enregistre. « Envoyer »
envoie tout ce qui reste, au rythme permis par le service ; s'il demande d'attendre, l'envoi s'arrête et un nouveau clic
reprend.

Les clés sont vérifiées auprès du service avant d'être enregistrées dans `~/.config/sport/sport.env`, lisible par vous seul.
Le mode automatique n'envoie que vers les services listés dans `SPORT_AUTO_DESTINATIONS=garmin,intervals` (sport.env) : aucun par défaut.

## Séances lancées sur la montre
Une séance démarrée à la main sur la montre Garmin passe toujours : les filtres de durée et de vitesse visent les détections automatiques et les imports des applis. Seul un démarrage de moins d'une minute est écarté. Son heure fait foi : deux séances de la montre à 2 h d'écart ne sont jamais prises pour un décalage horaire. Le 01/11/2026, cela a rendu à la bibliothèque 180 séances que seule la montre connaissait, dont 35 de musculation.

## Ce qui vient de garmin-grafana et d'Endurain
Le code de [garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana) (BSD-3-Clause, licence et version dans `garmin_grafana/`) est repris sans modification. Seul son client InfluxDB est remplacé par une base locale, `garmin-connect/garmin-grafana.sqlite`, et il utilise la session Garmin de `relais`. `relais.py collecte [début [fin]]` le lance à la demande ; `relais.py sante` lit les 3 derniers jours. Sa sélection laisse de côté `activity` : `relais.py garmin` s'occupe des activités de la montre.

Endurain (AGPL-3.0) tourne sur Postgres et son propre serveur : son code ne s'intègre pas tel quel. Ses fonctions sont donc refaites ici d'après ses modèles de données, dans `carnet.py` : titre, description, notes, ressenti, effort et photos d'une sortie, matériel, composants et matériel par défaut, objectifs par période, cibles de santé, eau, jeûne, transit. Tout est dans `sante/carnet.json` et `sante/medias/`, hors de `activites-v2/`, que chaque conversion refait. Une sortie y est repérée par son heure de départ.

## Performance
`relais.py sante` calcule `sante/performance.json` à partir de la bibliothèque et de `sante/`, sans rien envoyer. La charge d'une séance est le TRIMP d'Edwards (minutes par zone cardiaque × numéro de la zone) ; condition et fatigue en sont les moyennes sur 42 et 7 jours, et la fraîcheur est leur différence. Le niveau est le VDOT de Daniels et Gilbert, pris sur le meilleur effort de 5 km ou plus des 6 derniers mois ; les temps possibles viennent de la formule de Riegel. Seules les courses avec GPS comptent pour la vitesse, car les distances sans trace viennent souvent des pas. Une séance d'entraînement n'étant pas une course à fond, VDOT et temps possibles sont des minimums. Une séance sans cardio n'entre pas dans la charge.

Garmin ne calcule pas de VO2 max pour l'Instinct E-Sports. `relais` l'estime depuis la vitesse et la fréquence cardiaque des courses d'au moins 15 min assez soutenues (équation de l'ACSM, réserve cardiaque) : c'est la médiane des 10 dernières sur 3 mois, à lire comme une tendance et pas comme une mesure.

`relais.py sante` copie aussi de Smashrun, en lecture seule, ses statistiques, ses badges et la météo de chaque course (`sante/smashrun.json`). Runalyze réserve la lecture de son API aux offres payantes : un compte gratuit ne peut qu'y envoyer. FitnessSyncer ne fait que relayer les données d'autres services : il n'apporte rien de plus ici.

## Pas, poids et sommeil
Les sources sont Samsung, Withings, Mi Fit, Runtastic, Google Fit, Moves et Health Sync (poids et sommeil seulement : ses pas mélangent plusieurs applis sans les nommer). On garde une valeur par jour :
- **pas** : le plus grand total ;
- **poids** : la pesée la plus complète. Les recopies MyFitnessPal et Google Fit et les erreurs de saisie (plus de 8 kg d'écart) sont écartées ;
- **sommeil** : la nuit la plus détaillée, datée du jour du réveil.

Garmin n'accepte que les pesées. Le sommeil n'existe pas en FIT importable : il est dans `sante/` et dans `activites-v2/sommeil/`.

## Montre Garmin (Instinct E-Sports)
Branchée en USB, elle apparaît sur `/media/<utilisateur>/GARMIN`. Le service `relais-montre` (systemd utilisateur) sauvegarde ses fichiers dans `montre-garmin/` à chaque branchement, puis lance `relais.py sante` (pas et fréquence cardiaque de la montre compris).

`relais.py sante` lit aussi ses fichiers `Monitor/` : les pas du jour rejoignent `sante/pas.csv`, et le reste va dans `sante/montre.csv`. Le 25/10/2026, la montre ne gardait que le fichier du jour : chaque branchement ne sauvegarde que les journées encore présentes. Son sommeil n'est pas lu, car l'Instinct laisse les serveurs Garmin calculer les phases ; il vient de Garmin Connect (ci-dessous) et de Health Sync.

Comme la montre ne garde que quelques jours, `relais.py sante` copie aussi les pas du jour, les nuits (début, fin et phases) et les pesées de Garmin Connect dans `garmin-connect/`, en lecture seule. La première fois, cela fait environ 140 demandes, espacées d'une seconde. Ensuite, une vingtaine de demandes suffisent, car une année terminée n'est plus redemandée. Les pesées que l'outil a lui-même envoyées à Garmin sont laissées de côté. Le 28/10/2026, cela a ajouté 2 179 jours de pas (novembre 2020 → 2026) et 56 pesées.

La même copie reprend le résumé de chaque journée depuis fin 2020 (une demande par année) : `sante/garmin.csv` et les graphiques FC de repos, stress et Body Battery de l'interface. Le détail à la minute (fréquence cardiaque, stress, Body Battery) n'existe chez Garmin que pour les derniers mois : le 01/11/2026, le 11/07 était encore là, le 01/06 non. Chaque journée terminée est donc copiée dans `garmin-connect/minute/` pour la garder, avec les pas par quart d'heure et les phases de sommeil, et se consulte dans « Journée en détail ». La première fois, cela fait quelques centaines de demandes. Sur l'Instinct E-Sports, la VFC, le score de sommeil, la SpO2 et la respiration restent vides chez Garmin.

Chaque heure, et au début de « Tout faire », `relais.py garmin` récupère chez Garmin le FIT d'origine des nouvelles activités de la montre, sans branchement USB, puis refait la bibliothèque s'il y en a. Seules les activités d'un appareil Garmin sont prises : celles que l'outil a envoyées ne reviennent pas.

Parcours et entraînements vers la montre, sans l'appli du téléphone :
```bash
outils/relais.py vers-montre boucle.gpx              # itinéraire (AllTrails, Komoot…, avec ou sans heures), TCX ou activité FIT à refaire
outils/relais.py vers-montre course.fit --nom "Tour du lac"   # nom affiché sur la montre (16 caractères)
outils/relais.py vers-montre garmin                  # liste des parcours et entraînements de Garmin Connect
outils/relais.py vers-montre garmin Boucle 1234567890      # les prépare (nom ou numéro), sans rien changer sur Garmin
```
Les fichiers attendent dans `vers-montre/` et sont copiés dans `Garmin/NewFiles` de la montre au branchement suivant (tout de suite si elle est branchée). Ils passent ensuite dans `vers-montre/deposes/`. La montre les importe quand on la **débranche**.

Données satellites (EPO), pour que le GPS accroche plus vite, comme avec Garmin Express : à chaque branchement, `relais.py montre` remplace `Garmin/RemoteSW/EPO.BIN` par les orbites prévues des 9 prochains jours (serveur MediaTek), si celles de la montre ont plus de 2 jours. `relais.py satellites` le force. L'outil n'écrit que si la montre a déjà ce fichier, au même format ; une montre qui utilise `CPE.BIN` (puces Sony) est laissée telle quelle, faute de source publique.

L'interface web (port 8090) montre la santé : la dernière valeur de chaque mesure, les pas, le sommeil et le poids sur 1 an, 3 ans ou tout l'historique, et le tableau des mêmes valeurs.
