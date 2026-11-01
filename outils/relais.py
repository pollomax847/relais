#!/usr/bin/env python3
"""Outil unique pour les données sport : conversion précise, montre Garmin, santé, envoi vers les services.

  relais.py preparer     conversion précise de toutes les sources (sources-brutes/, ZIP compris) : une activité
                         par sortie réelle, en FIT, rangée par catégorie dans activites-v2/ (+ contrôle)
  relais.py sante        pas, poids et sommeil de toutes les sources → sante/ (et les pesées → Garmin)
  relais.py envoyer <garmin|strava|intervals|runalyze|smashrun> [N] [--reessayer]
                         envoie tout ce qui reste de activites-v2/ (ou N activités), de la plus ancienne à la plus
                         récente, au rythme permis par le service
  relais.py lots <destination> [--importes]
                         copie les activités pas encore envoyées en lots à importer à la main sur le site du service
                         (quand son API bloque un gros historique) ; --importes les note envoyées une fois l'import fait
  relais.py montre       sauvegarde la montre branchée et lui dépose
                         les parcours et entraînements en attente
  relais.py vers-montre <trace.gpx|.tcx|.fit>… [--nom NOM]
                         prépare des parcours à suivre sur la montre (itinéraire AllTrails, activité à refaire…)
  relais.py vers-montre garmin [nom ou numéro…]
                         parcours et entraînements de Garmin Connect pour la montre (sans nom : la liste)
  relais.py garmin       récupère chez Garmin les nouvelles activités de la montre, puis refait la bibliothèque
  relais.py collecte [AAAA-MM-JJ [AAAA-MM-JJ]]  le code de garmin-grafana (garmin_grafana/), base locale : les 3 derniers jours, ou la période
  relais.py mot-de-passe [--retirer]  choisit le mot de passe de l'interface, par exemple après un oubli
  relais.py certificat <adresse de l'ordinateur> [--refaire]  chiffre l'accès depuis le réseau de la maison
  relais.py auto         tout en une fois : montre, conversion, santé, envois choisis (SPORT_AUTO_DESTINATIONS)
  relais.py refaire garmin   retire de Garmin ce que l'ancienne version y a envoyé (avant d'envoyer activites-v2)
  relais.py satellites   met à jour les données satellites (EPO) de la montre branchée, pour un GPS plus rapide
  relais.py etat         montre branchée ? archive ?

Configuration (clés des services) : ~/.config/sport/sport.env. Interface : http://localhost:8090
"""
import csv
import fcntl
import getpass
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent


def lire_chemins():
    """Emplacements des données, lus dans chemins.yml à la racine du projet (modèle : chemins.exemple.yml).
    Sans ce fichier, tout est dans le projet. Chemins absolus, ou relatifs au projet ; « ~ » accepté."""
    fichier = PROJET / "chemins.yml"
    if not fichier.exists():
        return {}
    try:
        import yaml
    except ImportError:
        sys.exit(f"{fichier.name} demande PyYAML : pip install pyyaml")
    return yaml.safe_load(fichier.read_text()) or {}


def chemin(valeur):
    return PROJET / Path(str(valeur)).expanduser()  # un chemin absolu remplace PROJET


CHEMINS = lire_chemins()
ARCHIVE = chemin(CHEMINS.get("montre", "montre-garmin"))
CONFIG = Path.home() / ".config" / "sport" / "sport.env"
JETONS_GARMIN = Path.home() / ".config" / "sport" / "garmin-jetons"
JETONS_STRAVA = Path.home() / ".config" / "sport" / "strava.json"
VENV = Path.home() / ".local" / "share" / "sport" / "venv"
VERROU = PROJET / "outils" / ".verrou"
COMMANDES_EXCLUSIVES = {"envoyer", "sante", "auto", "refaire", "retirer", "preparer", "lots", "garmin", "collecte"}
V2 = chemin(CHEMINS.get("activites", "activites-v2"))  # conversion précise : une activité par sortie réelle (relais.py preparer)
CORBEILLE = chemin(CHEMINS.get("corbeille", "a-supprimer"))  # anciennes versions de activites-v2 (jamais effacées par l'outil)
VERS_MONTRE = chemin(CHEMINS.get("vers_montre", "vers-montre"))  # parcours et entraînements à déposer sur la montre
SANTE = chemin(CHEMINS.get("sante", "sante"))  # pas, poids et sommeil retenus, en CSV lisibles
POIDS_GARMIN = PROJET / "outils" / ".poids-garmin.txt"  # pesées créées sur Garmin par l'outil (jour, kg)
GARMIN_CONNECT = chemin(CHEMINS.get("garmin_connect", "garmin-connect"))  # copie de Garmin Connect : pas, nuits, pesées, résumés du jour, détail à la minute
GARMIN_DEPUIS = 2008  # première année demandée à Garmin Connect (ouverture du service)
PAUSE_GARMIN = 1  # secondes entre deux demandes à Garmin Connect, au rythme d'un client ordinaire
MINUTE_RECUL, MINUTE_RELUS, MINUTE_VIDES = 200, 3, 7  # détail à la minute : jours en arrière au plus, jours relus, jours vides pour s'arrêter
AUTO_GARMIN = 300  # activités envoyées par destination et par passage automatique (SPORT_AUTO_NOMBRE dans sport.env)

# Toutes les sources lues par « inventaire » et « historique » : « donnees » et « sources » de chemins.yml.
# SPORT_SOURCES=libellé=chemin;… dans sport.env en ajoute d'autres.
SOURCES = {"projet": chemin(CHEMINS.get("donnees", ".")),
           **{nom: chemin(dossier) for nom, dossier in (CHEMINS.get("sources") or {}).items()}}
# Google Drive/sport n'est plus lu par défaut : tout son contenu est déjà copié dans sport/ (inventaire du 25/10/2026 :
# aucune activité unique) et ce disque NTFS est très lent quand il est occupé. Pour le relire :
#   relais.py convertir "<disque>/Google Drive/sport"
# Dans le projet : outils, documentation et le dossier Docker ne sont pas des données
EXCLUS_PROJET = {"endurain", "outils", "scripts", "documentation", "sante", "a-supprimer", "activites-v2", "analyse",
                 "a-importer",  # lots préparés pour un import manuel (relais.py lots) : des copies de activites-v2
                 SANTE.name, CORBEILLE.name, V2.name, VERS_MONTRE.name,
                 GARMIN_CONNECT.name}  # lu seulement par « sante », pas comme activités

# Dossiers de la montre qui contiennent vos données. Les autres (Debug, EDM, REMOTESW, TEXT…) sont internes à la montre.
DOSSIERS_MONTRE = ["Activity", "Monitor", "Sleep", "Summary", "Records", "Totals",
                   "Courses", "Workouts", "Location", "Goals", "Settings", "Sports"]


def config():
    conf = {}
    if CONFIG.exists():
        for ligne in CONFIG.read_text().splitlines():
            if "=" in ligne and not ligne.lstrip().startswith("#"):
                cle, valeur = ligne.split("=", 1)
                conf[cle.strip()] = valeur.strip()
    return conf


def notifier(message):
    print(message)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-i", "watch", "Montre Garmin", message], check=False)


def trouver_montre():
    """Point de montage de la montre (stockage USB contenant un dossier Garmin/), ou None."""
    with open("/proc/mounts") as f:
        # /proc/mounts écrit les espaces en \040
        points = [re.sub(r"\\(\d{3})", lambda m: chr(int(m[1], 8)), ligne.split()[1]) for ligne in f]
    for point in points:
        if not point.startswith(("/media/", "/run/media/", "/mnt/")):
            continue
        try:
            if (Path(point) / "Garmin" / "Activity").is_dir():
                return Path(point)
        except OSError:
            continue
    return None


def cmd_montre():
    montre = trouver_montre()
    if not montre:
        sys.exit("Aucune montre Garmin branchée (pas de dossier Garmin/ sur un disque USB monté).")

    copies = 0
    for dossier in DOSSIERS_MONTRE:
        source = montre / "Garmin" / dossier
        if not source.is_dir():
            continue
        for fichier in source.rglob("*"):
            if not fichier.is_file():
                continue
            cible = ARCHIVE / dossier / fichier.relative_to(source)
            # Les fichiers du jour (Monitor, Sleep) grossissent au fil de la journée : on recopie s'ils ont changé
            if cible.exists() and cible.stat().st_size == fichier.stat().st_size:
                continue
            cible.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fichier, cible)
            copies += 1
    print(f"Sauvegarde : {copies} fichier(s) copiés dans {ARCHIVE}")
    try:
        satellites(montre)
    except (OSError, ValueError) as e:
        print(f"Données satellites : non mises à jour ({e})")
    deposes = deposer_sur_montre(montre)
    if deposes:
        notifier(f"{deposes} parcours ou entraînement(s) déposés sur la montre : débranchez-la pour qu'elle les importe.")
    notifier(f"Montre sauvegardée ({copies} fichiers).")


EPO_URL = "https://epodownload.mediatek.com/EPO.DAT"  # la source de Garmin pour ses puces MediaTek
EPO_JEU = 2304  # octets par jeu de 6 h (32 satellites × 72 octets), chacun commençant par son heure GPS sur 3 octets
EPO_JEUX = 4 * 9  # les montres Garmin n'en lisent que 9 jours
EPO_VIEUX = 2  # jours au-delà desquels « relais.py montre » remplace les données de la montre
DEBUT_GPS = datetime(1980, 1, 6, tzinfo=timezone.utc)


def epo_debut(donnees):
    """Début de validité de données EPO (heure GPS du premier jeu)."""
    return DEBUT_GPS + timedelta(hours=int.from_bytes(donnees[:3], "little"))


def fichier_epo(montre):
    """Garmin/RemoteSW/EPO.BIN de la montre (casse quelconque), ou None si elle n'en a pas."""
    for dossier in (montre / "Garmin").iterdir():
        if dossier.name.lower() == "remotesw" and dossier.is_dir():
            return next((f for f in dossier.iterdir() if f.name.lower() == "epo.bin"), None)
    return None


def satellites(montre, forcer=False):
    """Remplace l'EPO.BIN de la montre par les orbites prévues des 9 prochains jours, comme Garmin Express.

    Seulement si la montre a déjà ce fichier : celles qui utilisent CPE.BIN (puces Sony) n'ont pas de source
    publique et sont laissées telles quelles. Les données sont vérifiées (taille, date) avant d'être écrites."""
    actuel = fichier_epo(montre)
    if actuel is None:
        print("Données satellites : la montre n'a pas de RemoteSW/EPO.BIN, rien n'est écrit")
        return False
    ancien = actuel.read_bytes()
    if ancien and len(ancien) % EPO_JEU:
        print(f"Données satellites : l'EPO.BIN de la montre n'a pas le format attendu ({len(ancien)} octets), laissé tel quel")
        return False
    age = datetime.now(timezone.utc) - epo_debut(ancien) if len(ancien) >= 3 else None
    if not forcer and age is not None and age < timedelta(days=EPO_VIEUX):
        print(f"Données satellites : à jour (depuis le {epo_debut(ancien):%d/%m %H h} UTC)")
        return False
    with urllib.request.urlopen(EPO_URL, timeout=30) as resp:
        donnees = resp.read()
    if len(donnees) != 120 * EPO_JEU:
        raise ValueError(f"taille inattendue des données MediaTek ({len(donnees)} octets)")
    if abs(datetime.now(timezone.utc) - epo_debut(donnees)) > timedelta(days=3):
        raise ValueError(f"données MediaTek datées du {epo_debut(donnees):%d/%m/%Y}")
    temporaire = actuel.with_name(actuel.name + ".tmp")
    temporaire.write_bytes(donnees[:EPO_JEUX * EPO_JEU])
    os.replace(temporaire, actuel)
    print(f"Données satellites : mises à jour, valables 9 jours à partir du {epo_debut(donnees):%d/%m %H h} UTC")
    return True


def cmd_satellites():
    montre = trouver_montre()
    if not montre:
        sys.exit("Aucune montre Garmin branchée (pas de dossier Garmin/ sur un disque USB monté).")
    try:
        satellites(montre, forcer=True)
    except (OSError, ValueError) as e:
        sys.exit(f"Données satellites : non mises à jour ({e})")


def deposer_sur_montre(montre):
    """Copie dans Garmin/NewFiles les FIT en attente dans VERS_MONTRE : la montre les range dans Courses/ ou Workouts/
    quand on la débranche. Les fichiers déposés passent dans VERS_MONTRE/deposes/."""
    attente = sorted(VERS_MONTRE.glob("*.fit")) if VERS_MONTRE.is_dir() else []
    if not attente:
        return 0
    (montre / "Garmin" / "NewFiles").mkdir(exist_ok=True)
    (VERS_MONTRE / "deposes").mkdir(exist_ok=True)
    for fichier in attente:
        shutil.copyfile(fichier, montre / "Garmin" / "NewFiles" / fichier.name)
        fichier.replace(VERS_MONTRE / "deposes" / fichier.name)
        print(f"  {fichier.name} : déposé sur la montre")
    return len(attente)


def mettre_en_attente(nom, contenu, prets):
    """Range un FIT prêt pour la montre dans VERS_MONTRE, sous un nom sans accents (mémoire FAT de la montre).
    Un fichier en attente du même nom est remplacé, sauf s'il vient de la même commande (prets)."""
    simple = unicodedata.normalize("NFKD", nom).encode("ascii", "ignore").decode()
    base = re.sub(r"[^A-Za-z0-9-]+", "_", simple).strip("_")[:30] or "parcours"
    cible, n = VERS_MONTRE / f"{base}.fit", 1
    while cible in prets:
        n += 1
        cible = VERS_MONTRE / f"{base}-{n}.fit"
    VERS_MONTRE.mkdir(parents=True, exist_ok=True)
    cible.write_bytes(contenu)
    prets.append(cible)


def parcours_depuis_fichiers(args):
    import fitconv

    nom_force = args[args.index("--nom") + 1] if "--nom" in args[:-1] else None
    fichiers = [a for i, a in enumerate(args) if a != "--nom" and (i == 0 or args[i - 1] != "--nom")]
    if nom_force and len(fichiers) > 1:
        sys.exit("--nom ne s'utilise qu'avec un seul fichier")
    prets = []
    for fichier in map(lambda a: Path(a).expanduser(), fichiers):
        if not fichier.is_file():
            print(f"  {fichier} : introuvable")
            continue
        contenu, ext = fichier.read_bytes(), fichier.suffix.lower()
        if ext == ".fit" and fitconv.type_fit(contenu) in ("course", "workout"):
            mettre_en_attente(nom_force or fichier.stem, contenu, prets)  # déjà au format de la montre
            print(f"  {fichier.name} : prêt tel quel")
            continue
        nom, sport, points = fitconv.lire_parcours(contenu, ext)
        try:
            octets, distance = fitconv.parcours_fit(nom_force or nom or fichier.stem, sport, points)
        except ValueError as e:
            print(f"  {fichier.name} : ignoré ({e}, {len(points)} point(s) GPS)")
            continue
        mettre_en_attente(nom_force or nom or fichier.stem, octets, prets)
        print(f"  {fichier.name} : parcours « {(nom_force or nom or fichier.stem)[:16]} », {distance / 1000:.1f} km")
    return prets


def parcours_depuis_garmin(demandes):
    """Parcours (course-service) et entraînements (workout-service) de Garmin Connect, téléchargés en FIT : la même
    chose que ce que l'appli du téléphone enverrait à la montre. Rien n'est modifié sur Garmin."""
    api = connexion_garmin()
    parcours = api.get_courses() or []
    if isinstance(parcours, dict):
        parcours = parcours.get("coursesForUser") or []
    entrainements = api.get_workouts(0, 500) or []
    elements = ([("parcours", c["courseId"], c.get("courseName") or "", f"{(c.get('distanceInMeters') or 0) / 1000:.1f} km",
                  lambda i: api.download_course(i, "fit")) for c in parcours]
                + [("entraînement", w["workoutId"], w.get("workoutName") or "",
                    (w.get("sportType") or {}).get("sportTypeKey", ""), api.download_workout) for w in entrainements])
    if not demandes:
        for genre, ident, nom, detail, _ in elements:
            print(f"  {genre:12} {ident:>12}  {nom}  ({detail})")
        print(f"{len(elements)} élément(s). Pour les préparer : relais.py vers-montre garmin <nom ou numéro>…")
        return []
    prets = []
    for demande in demandes:
        choisis = [e for e in elements if demande == str(e[1]) or demande.lower() in e[2].lower()]
        if not choisis:
            print(f"  « {demande} » : rien de ce nom sur Garmin Connect")
        for genre, ident, nom, detail, telecharger in choisis:
            mettre_en_attente(nom or str(ident), telecharger(ident), prets)
            print(f"  {genre} « {nom} » ({detail}) : prêt")
    return prets


def cmd_vers_montre():
    """relais.py vers-montre <fichier…> [--nom NOM] | garmin [nom ou numéro…] : parcours et entraînements pour la
    montre. Ils attendent dans VERS_MONTRE et sont déposés au prochain branchement, ou tout de suite si elle l'est."""
    args = sys.argv[2:]
    if not args:
        sys.exit("Usage : relais.py vers-montre <trace.gpx|.tcx|.fit>… [--nom NOM]\n"
                 "        relais.py vers-montre garmin [nom ou numéro…]  (sans nom : la liste de Garmin Connect)")
    utiliser_venv()
    prets = parcours_depuis_garmin(args[1:]) if args[0] == "garmin" else parcours_depuis_fichiers(args)
    if not prets:
        return
    montre = trouver_montre()
    if montre:
        deposer_sur_montre(montre)
        print("Débranchez la montre : elle importe les nouveaux fichiers à ce moment-là.")
    else:
        print(f"Montre non branchée : {len(prets)} fichier(s) attendent dans {VERS_MONTRE} (déposés au prochain branchement).")


def cmd_etat():
    montre = trouver_montre()
    if montre:
        libre = shutil.disk_usage(montre)
        activites = sorted((montre / "Garmin" / "Activity").glob("*.[fF][iI][tT]"))
        print(f"Montre       : branchée sur {montre}, mémoire {libre.used * 100 // libre.total} % utilisée")
        print(f"               {len(activites)} activité(s) dessus, la plus récente : {activites[-1].name if activites else 'aucune'}")
        epo = fichier_epo(montre)
        print("               données satellites : " + (f"depuis le {epo_debut(epo.read_bytes()):%d/%m/%Y}" if epo and epo.stat().st_size >= 3
                                                       else "pas de RemoteSW/EPO.BIN"))
    else:
        print("Montre       : non branchée")

    if ARCHIVE.exists():
        detail = ", ".join(f"{d.name} {sum(1 for f in d.rglob('*') if f.is_file())}"
                           for d in sorted(ARCHIVE.iterdir()) if d.is_dir())
        print(f"Archive      : {detail}")
    else:
        print("Archive      : vide (lancez « relais.py montre »)")


def sources():
    toutes = dict(SOURCES)
    for paire in filter(None, config().get("SPORT_SOURCES", "").split(";")):
        libelle, chemin = paire.split("=", 1)
        toutes[libelle.strip()] = Path(chemin.strip()).expanduser()
    return toutes


def utiliser_venv():
    """garminconnect et garmin-fit-sdk sont dans un environnement à part : on relance la commande avec son Python."""
    try:
        import garmin_fit_sdk  # noqa: F401
        import garminconnect  # noqa: F401
    except ImportError:
        python_venv = VENV / "bin" / "python"
        if python_venv.exists() and Path(sys.executable) != python_venv:
            os.execv(python_venv, [str(python_venv), __file__, *sys.argv[1:]])
        sys.exit(f"Bibliothèques absentes : python3 -m venv {VENV} && {VENV}/bin/pip install garminconnect garmin-fit-sdk pyyaml")


def connexion_garmin():
    """Session Garmin Connect. Le mot de passe n'est demandé qu'à la première connexion, ensuite les jetons suffisent."""
    from garminconnect import Garmin

    logging.getLogger("garminconnect").setLevel(logging.CRITICAL)  # les erreurs sont résumées par relais.py
    JETONS_GARMIN.mkdir(parents=True, exist_ok=True)
    JETONS_GARMIN.chmod(0o700)
    try:
        api = Garmin()
        api.login(str(JETONS_GARMIN))
        return api
    except Exception:
        pass  # pas encore de jetons, ou jetons expirés
    if not sys.stdin.isatty():
        raise RuntimeError("connectez-vous une fois à Garmin dans l'interface web ou le terminal")
    email = input("E-mail Garmin Connect : ")
    api = Garmin(email, getpass.getpass("Mot de passe Garmin Connect : "),
                 prompt_mfa=lambda: input("Code de vérification Garmin (MFA) : "))
    api.login(str(JETONS_GARMIN))
    return api


DESTINATIONS = ("garmin", "strava", "intervals", "runalyze", "smashrun")
PAUSES = {"garmin": 5, "strava": 10, "intervals": 1, "runalyze": 5,
          "smashrun": 15}  # s entre deux envois (Smashrun : 250 requêtes par heure)
SPORTS_ACCEPTES = {"smashrun": {"running"}}  # les autres activités ne sont pas proposées à ces services
DISTANCE_EXIGEE = {"smashrun"}  # une activité sans distance n'est pas proposée à ces services


def proposee(nom, a):
    """L'activité (ligne de index.csv) peut-elle aller vers ce service ?"""
    return ((nom not in SPORTS_ACCEPTES or a["sport"] in SPORTS_ACCEPTES[nom])
            and (nom not in DISTANCE_EXIGEE or bool(a["distance_km"])))
EXPORT_DU_SERVICE = {"smashrun": "Smashrun"}  # activité lue dans l'export de ce service : elle y est déjà


class GarminV2:
    """Garmin Connect, pour les FIT de activites-v2/ (déjà compacts, avec des points réguliers sans trace)."""
    nom, titre = "garmin", "Garmin Connect"

    def __init__(self):
        self.api = connexion_garmin()

    def verifier(self):
        return self.api.get_full_name() if hasattr(self.api, "get_full_name") else "connecté"

    def envoyer(self, fichier, activite):
        import destinations
        from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError

        try:
            # « import » comme sur le site Garmin. Les services reliés à Garmin (Strava…) la reçoivent quand même.
            resultat = self.api.import_activity(str(fichier))
        except GarminConnectTooManyRequestsError as e:
            raise destinations.Limite("Garmin limite le nombre d'envois") from e
        except GarminConnectConnectionError as e:
            return "deja" if "uplicate" in str(e) or "409" in str(e) else f"refuse : {str(e)[:120]}"
        echecs = (resultat or {}).get("detailedImportResult", {}).get("failures") or []
        if not echecs:
            return "envoye"
        return "deja" if "uplicate" in str(echecs) else f"refuse : {str(echecs)[:120]}"


def destination(nom):
    import destinations

    conf = config()
    if nom == "garmin":
        return GarminV2()
    if nom == "strava":
        return destinations.Strava(conf, JETONS_STRAVA)
    if nom == "intervals":
        return destinations.Intervals(conf)
    if nom == "runalyze":
        return destinations.Runalyze(conf)
    if nom == "smashrun":
        return destinations.Smashrun(conf)
    raise ValueError(f"destination inconnue : {nom} (possibles : {', '.join(DESTINATIONS)})")


def journal_envois(nom):
    return PROJET / "outils" / f".envoyes-{nom}-v2.txt"


def lignes_envois(nom):
    """Lignes du journal : [fichier, statut, début UTC, durée min]. Début et durée manquent dans les lignes écrites avant
    le 26/10/2026 que completer_journal() n'a pas pu compléter."""
    journal = journal_envois(nom)
    if not journal.exists():
        return []
    return [(c + ["", ""])[:4] for c in (l.split("\t") for l in journal.read_text().splitlines()) if len(c) >= 2]


def etat_envois(nom):
    """{fichier de activites-v2 : dernier statut}."""
    return {l[0]: l[1] for l in lignes_envois(nom)}


def completer_journal(nom):
    """Ajoute début et durée aux lignes écrites avant le 26/10/2026, d'après activites-v2/index.csv et ses anciennes
    versions (a-supprimer/) : une activité déjà envoyée est ainsi reconnue même si son fichier change de nom."""
    lignes = lignes_envois(nom)
    if all(l[2] for l in lignes):
        return
    connus = {}
    for index in [V2 / "index.csv", *sorted(CORBEILLE.glob("*/activites-v2/index.csv"), reverse=True)]:
        if index.exists():
            with index.open() as f:
                for a in csv.DictReader(f):
                    connus.setdefault(a["fichier"], (a["debut_utc"], a["duree_min"]))
    for l in lignes:
        if not l[2] and l[0] in connus:
            l[2], l[3] = connus[l[0]]
    provisoire = journal_envois(nom).with_suffix(".tmp")
    provisoire.write_text("".join("\t".join(l) + "\n" for l in lignes))
    provisoire.replace(journal_envois(nom))


ECART_DEPART, RECOUVREMENT = 120, 0.6  # mêmes règles que regrouper.py pour reconnaître la même sortie
# appli d'origine (colonne « enregistreur ») des activités lues par Health Sync dans un service : y sont déjà
ORIGINE_SERVICE = {"garmin": "Garmin", "strava": "Strava"}


def deja_envoyee(a, envoyees):
    """Fichier d'une activité déjà envoyée sous un autre nom (la même sortie, dont le meilleur exemplaire ou l'heure
    a changé depuis l'envoi), ou None. envoyees : [(début, durée s ou None, fichier)]."""
    debut, duree = datetime.fromisoformat(a["debut_utc"]), float(a["duree_min"]) * 60
    for debut_e, duree_e, fichier in envoyees:
        ecart = abs((debut_e - debut).total_seconds())
        if duree_e is None:  # ligne ancienne non complétée : le départ seul
            if ecart <= ECART_DEPART:
                return fichier
            continue
        if ecart <= ECART_DEPART and abs(duree - duree_e) <= 0.1 * max(duree, duree_e) + 60:
            return fichier
        commun = (min(debut + timedelta(seconds=duree), debut_e + timedelta(seconds=duree_e)) - max(debut, debut_e)).total_seconds()
        if commun > 0 and commun / max(min(duree, duree_e), 1) >= RECOUVREMENT:
            return fichier
    return None


def a_envoyer(nom, reessayer=False):
    """Activités de activites-v2/ pas encore envoyées à une destination, de la plus ancienne à la plus récente.

    Une activité déjà envoyée sous un autre nom (même sortie) ou venue de ce service (Health Sync) n'est pas renvoyée :
    elle est notée « deja » dans le journal. Renvoie (toutes les activités, état du journal, activités à envoyer)."""
    with (V2 / "index.csv").open() as f:
        activites = sorted(csv.DictReader(f), key=lambda a: a["debut_utc"])
    completer_journal(nom)
    etat = etat_envois(nom)
    envoyees = []
    for fichier, statut, debut, duree in lignes_envois(nom):
        if not statut.startswith("refuse") and etat[fichier] != "retiree":
            m = re.search(r"(\d{4}-\d\d-\d\d)_(\d\d)(\d\d)(\d\d)", fichier)  # nom = départ UTC
            debut = debut or (m and f"{m[1]}T{m[2]}:{m[3]}:{m[4]}+00:00")
            if debut:
                envoyees.append((datetime.fromisoformat(debut), float(duree) * 60 if duree else None, fichier))
    a_faire, doublons = [], []
    export = EXPORT_DU_SERVICE.get(nom)
    for a in activites:
        if a["fichier"] in etat and not (reessayer and etat[a["fichier"]].startswith("refuse")):
            continue
        if not proposee(nom, a):
            continue
        if a["enregistreur"] == ORIGINE_SERVICE.get(nom):  # la montre Garmin se synchronise elle-même avec Garmin
            doublons.append((a, f"deja : vient de {a['enregistreur']}"
                                + (" (lue par Health Sync)" if a["source"] == "Health Sync" else "")))
        elif export and export in {a["source"], *a["autres_sources"].split(", ")}:
            doublons.append((a, f"deja : présente dans l'export {export}"))
        elif a["fichier"] not in etat and (ancien := deja_envoyee(a, envoyees)):
            doublons.append((a, f"deja : envoyée sous {ancien}"))
        else:
            a_faire.append(a)
    if doublons:
        with journal_envois(nom).open("a") as journal:
            for a, statut in doublons:
                journal.write(f"{a['fichier']}\t{statut}\t{a['debut_utc']}\t{a['duree_min']}\n")
                print(f"  {a['fichier']} : {statut}")
        etat = etat_envois(nom)
    return activites, etat, a_faire


DECALAGES_SERVICE = (0, 3600, -3600, 7200, -7200)  # imports Smashrun ou Runtastic décalés d'1 ou 2 h (certaines séances)


def sans_les_presentes(nom, a_faire, presentes):
    """Retire les activités déjà sur le service (même départ à 3 min près, éventuellement décalé d'1 ou 2 h, et durée
    à 15 % près) et les note « deja » dans le journal."""
    reste = []
    with journal_envois(nom).open("a") as journal:
        for a in a_faire:
            debut, duree = datetime.fromisoformat(a["debut_utc"]), float(a["duree_min"]) * 60
            if any(any(abs((d - debut).total_seconds() - dec) <= 180 for dec in DECALAGES_SERVICE)
                   and abs(du - duree) <= 0.15 * max(du, duree) + 60 for d, du in presentes):
                journal.write(f"{a['fichier']}\tdeja : déjà sur le compte\t{a['debut_utc']}\t{a['duree_min']}\n")
            else:
                reste.append(a)
    if len(reste) < len(a_faire):
        print(f"  {len(a_faire) - len(reste)} activité(s) déjà sur le compte, notées « deja »")
    return reste


def envoyer_v2(nom, limite, reessayer=False):
    """Envoie au plus « limite » activités de activites-v2/ vers une destination (voir a_envoyer)."""
    import destinations

    activites, etat, a_faire = a_envoyer(nom, reessayer)
    if not a_faire:
        print(f"{nom} : {len(activites)} activités, {len(etat)} déjà traitées, rien à envoyer")
        return Counter()
    try:
        cible = destination(nom)
        print(f"  {cible.titre} : {cible.verifier()}")
        if hasattr(cible, "presentes"):  # le service dit ce qu'il a déjà, d'où qu'il vienne
            a_faire = sans_les_presentes(nom, a_faire, cible.presentes())
    except destinations.NonConfiguree as e:
        print(f"  {nom} : {e}")
        return Counter()
    a_faire = a_faire[:limite]
    print(f"{nom} : {len(activites)} activités, {len(etat_envois(nom))} déjà traitées, {len(a_faire)} à envoyer maintenant")
    if not a_faire:
        return Counter()
    compte = Counter()
    with journal_envois(nom).open("a") as journal:
        for n, a in enumerate(a_faire, 1):
            try:
                statut = cible.envoyer(V2 / a["fichier"], a)
            except destinations.Limite as e:
                print(f"  {e} : arrêt après {n - 1} envoi(s), relancer plus tard")
                break
            except destinations.NonConfiguree as e:
                print(f"  {e}")
                break
            statut = re.sub(r"\s+", " ", statut)  # une ligne par envoi, colonnes séparées par des tabulations
            journal.write(f"{a['fichier']}\t{statut}\t{a['debut_utc']}\t{a['duree_min']}\n")
            journal.flush()
            compte[statut.split(" :")[0]] += 1
            if statut.startswith("refuse"):
                print(f"  {a['fichier']} : {statut}")
            if n % 10 == 0:
                print(f"  {n}/{len(a_faire)} ({a['debut_utc'][:10]})", flush=True)
            time.sleep(PAUSES.get(nom, 2))
    print(f"  {nom} : " + (", ".join(f"{v} {k}" for k, v in compte.items()) or "rien envoyé"))
    return compte


def cmd_envoyer():
    """relais.py envoyer <destination> [N] [--reessayer] : tout ce qui reste (ou N activités), au rythme permis par le
    service (PAUSES) ; s'il demande d'attendre, l'envoi s'arrête proprement et reprend au lancement suivant."""
    args = sys.argv[2:]
    if not args or args[0] not in DESTINATIONS:
        sys.exit(f"Usage : relais.py envoyer <{'|'.join(DESTINATIONS)}> [nombre, tout par défaut] [--reessayer]")
    utiliser_venv()
    nombres = [a for a in args[1:] if a.isdigit()]
    envoyer_v2(args[0], int(nombres[0]) if nombres else None, "--reessayer" in args)


LOTS = PROJET / "a-importer"  # lots à importer à la main sur le site d'un service (relais.py lots)
TAILLE_LOT = 900  # fichiers par lot : Runalyze conseille au plus 1000 à la fois dans le navigateur


def cmd_lots():
    """relais.py lots <destination> [--importes] : quand l'API d'un service bloque un gros historique (Runalyze et
    Cloudflare, 27/10/2026), copie les activités pas encore envoyées dans a-importer/<destination>/lot-N/, à glisser
    dans la page d'import du site. Une fois l'import fait, --importes les note envoyées dans le journal et range les
    lots dans a-supprimer/."""
    args = sys.argv[2:]
    if not args or args[0] not in DESTINATIONS:
        sys.exit(f"Usage : relais.py lots <{'|'.join(DESTINATIONS)}> [--importes]")
    nom, dossier = args[0], LOTS / args[0]
    liste = dossier / "liste.csv"
    if "--importes" in args:
        if not liste.exists():
            sys.exit(f"Aucun lot préparé pour {nom} ({dossier})")
        with liste.open() as f, journal_envois(nom).open("a") as journal:
            lignes = list(csv.DictReader(f))
            for l in lignes:
                journal.write(f"{l['fichier']}\timport manuel\t{l['debut_utc']}\t{l['duree_min']}\n")
        rangement = CORBEILLE / time.strftime("%Y-%m-%d_%H%M") / f"a-importer-{nom}"
        rangement.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(dossier, rangement)
        print(f"{nom} : {len(lignes)} activités notées « import manuel » ; lots rangés dans {rangement}")
        return
    if dossier.exists():
        sys.exit(f"Des lots attendent déjà dans {dossier} : importez-les puis « relais.py lots {nom} --importes »")
    _, _, a_faire = a_envoyer(nom)
    if not a_faire:
        print(f"{nom} : rien à importer")
        return
    noms = Counter(Path(a["fichier"]).name for a in a_faire)
    dossier.mkdir(parents=True)
    with liste.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["lot", "copie", "fichier", "debut_utc", "duree_min"])
        w.writeheader()
        for n, a in enumerate(a_faire):
            lot = dossier / f"lot-{n // TAILLE_LOT + 1}"
            lot.mkdir(exist_ok=True)
            source = V2 / a["fichier"]
            # même nom dans deux catégories : la catégorie en préfixe
            copie = source.name if noms[source.name] == 1 else f"{a['categorie']}_{source.name}"
            shutil.copyfile(source, lot / copie)
            w.writerow({"lot": lot.name, "copie": copie, "fichier": a["fichier"], "debut_utc": a["debut_utc"],
                        "duree_min": a["duree_min"]})
    lots = (len(a_faire) - 1) // TAILLE_LOT + 1
    print(f"{nom} : {len(a_faire)} activités copiées en {lots} lot(s) dans {dossier}")
    print(f"  importez chaque lot sur le site, puis : relais.py lots {nom} --importes")


def poids_garmin(api, poids):
    """Synchronise les pesées retenues avec Garmin Connect (Garmin n'accepte ni les pas ni le sommeil).

    Le journal POIDS_GARMIN note chaque pesée que l'outil a créée (« jour<TAB>kg »). Une pesée créée par l'outil
    et qui n'est plus retenue (recopie MyFitnessPal, erreur de saisie…) est retirée de Garmin ; les pesées
    saisies ailleurs (balance, appli Garmin) ne sont jamais touchées, et un jour qui en a déjà une est sauté.
    """
    envoyees = {}
    if POIDS_GARMIN.exists():
        for ligne in POIDS_GARMIN.read_text().splitlines():
            jour, _, kg = ligne.partition("\t")
            envoyees[jour] = float(kg) if kg else None

    retirees = 0
    for jour, kg in list(envoyees.items()):
        retenue = poids.get(date.fromisoformat(jour))
        if retenue and (kg is None or abs(retenue.kg - kg) <= 0.05):
            continue
        for pesee in (api.get_daily_weigh_ins(jour) or {}).get("dateWeightList", []):
            # seulement une saisie « MANUAL » à la valeur que l'outil avait envoyée
            if pesee.get("sourceType") == "MANUAL" and kg is not None and abs(pesee.get("weight", 0) / 1000 - kg) <= 0.05:
                api.delete_weigh_in(pesee["samplePk"], jour)
                retirees += 1
        del envoyees[jour]
        time.sleep(0.5)
    if retirees:
        print(f"Garmin : {retirees} pesée(s) créées par l'outil et plus retenues, retirées")

    jours = sorted(j for j in poids if j.isoformat() not in envoyees)
    deja = set()  # jours où Garmin a une pesée venant d'ailleurs
    for annee in sorted({j.year for j in jours}):
        try:
            reponse = api.get_weigh_ins(f"{annee}-01-01", f"{annee}-12-31") or {}
        except Exception:
            continue
        deja |= {r.get("summaryDate") for r in reponse.get("dailyWeightSummaries", [])}
    ajoutees = 0
    for jour in jours:
        if jour.isoformat() in deja:
            continue
        p = poids[jour]
        api.add_weigh_in_with_timestamps(weight=round(p.kg, 2), unitKey="kg",
                                         dateTimestamp=p.locale().replace(tzinfo=None).isoformat(timespec="seconds"),
                                         gmtTimestamp=p.heure.replace(tzinfo=None).isoformat(timespec="seconds"))
        envoyees[jour.isoformat()] = round(p.kg, 2)
        ajoutees += 1
        time.sleep(1)
    POIDS_GARMIN.write_text("".join(f"{j}\t{kg if kg is not None else ''}\n" for j, kg in sorted(envoyees.items())))
    print(f"Garmin : {ajoutees} pesée(s) ajoutée(s), {len(envoyees)} au total venant de l'outil")


def copier_garmin(api):
    """Copie en lecture seule les pas du jour, les nuits, les pesées et les résumés de chaque journée de Garmin Connect
    dans GARMIN_CONNECT, un fichier par type et par année (garmin-connect-pas-AAAA.json…), lus ensuite par sante.py comme
    les autres sources, et le détail des journées, deux fichiers par jour dans minute/.

    Une année copiée jusqu'au 31/12 n'est plus redemandée : après la première fois (une centaine de demandes par type, 9
    pour les résumés, qui tiennent en une demande par année), un passage n'en fait plus qu'une vingtaine. Pas, sommeil et
    résumés commencent avec la montre : on remonte le temps jusqu'à deux années vides de suite. Les pesées que l'outil a lui-même créées sur
    Garmin (POIDS_GARMIN) sont laissées de côté : ce sont des copies des autres sources.
    """
    envoyees = {}
    if POIDS_GARMIN.exists():
        for ligne in POIDS_GARMIN.read_text().splitlines():
            jour, _, kg = ligne.partition("\t")
            if kg:
                envoyees[jour] = float(kg)
    GARMIN_CONNECT.mkdir(parents=True, exist_ok=True)
    aujourdhui = date.today()
    demandes = 0

    def demander(fonction, *args):
        nonlocal demandes
        demandes += 1
        reponse = fonction(*args)
        time.sleep(PAUSE_GARMIN)
        return reponse

    def par_jour(appel, garder):
        """Lecture d'une valeur par jour, 28 jours au plus par demande ; garder(l) renvoie ce qu'on copie, ou None."""
        def lire(annee, ancien, fin):
            debut = date(annee, 1, 1)
            if ancien["jusqu_au"]:
                debut = max(debut, date.fromisoformat(ancien["jusqu_au"]) - timedelta(days=7))  # le dernier jour était partiel
            jours = {l["calendarDate"]: l for l in ancien["valeurs"]}
            while debut <= fin:
                bout = min(debut + timedelta(days=27), fin)
                for l in demander(appel, debut.isoformat(), bout.isoformat()) or []:
                    if copie := garder(l):
                        jours[l["calendarDate"]] = copie
                debut = bout + timedelta(days=1)
            return sorted(jours.values(), key=lambda l: l["calendarDate"])
        return lire

    pas = par_jour(api.get_daily_steps, lambda l: l.get("totalSteps") and
                   {"calendarDate": l["calendarDate"], "totalSteps": l["totalSteps"]})
    # une nuit par jour de réveil : heures en ms (UTC et locales), phases en secondes ; les champs vides sont omis
    sommeil = par_jour(api.get_sleep_daily, lambda l: (l.get("values") or {}).get("totalSleepTimeInSeconds") and
                       {"calendarDate": l["calendarDate"], **{k: v for k, v in l["values"].items() if v is not None}})

    def resume(annee, ancien, fin):
        reponse = demander(api.get_daily_summaries, f"{annee}-01-01", fin.isoformat()) or {}
        return [j for j in reponse.get("data", []) if j.get("includesWellnessData")]

    def minute(jour):
        coeur = demander(api.get_heart_rates, jour) or {}
        stress = demander(api.get_stress_data, jour) or {}
        return {"fc": [l for l in coeur.get("heartRateValues") or [] if l[1]],
                "stress": [l for l in stress.get("stressValuesArray") or [] if l[1] is not None and l[1] >= 0],
                "batterie": [[l[0], l[2]] for l in stress.get("bodyBatteryValuesArray") or [] if l[2] is not None]}

    def nuit(jour):
        """Pas par quart d'heure, et la nuit terminée ce jour-là : phases, mouvements, FC, stress et Body Battery."""
        sommeil = demander(api.get_sleep_data, jour) or {}
        pas = demander(api.get_steps_data, jour) or []
        return {"pas": pas if any(l.get("steps") for l in pas) else [],  # Garmin renvoie 96 quarts d'heure à zéro pour un jour archivé
                "sommeil": {c: sommeil.get(c) for c in ("dailySleepDTO", "sleepLevels", "sleepMovement", "sleepHeartRate",
                                                        "sleepStress", "sleepBodyBattery")} if sommeil.get("sleepLevels") else {}}

    def journees(nom, lire):
        """Garmin ne garde le détail des journées que quelques mois (11/07/2026 présent, 01/06 absent le 01/11) : chaque
        journée terminée est copiée pour de bon. Du plus récent au plus ancien, jusqu'à MINUTE_VIDES jours vides de suite ;
        les MINUTE_RELUS derniers jours sont relus, car la montre a pu synchroniser depuis."""
        dossier = GARMIN_CONNECT / "minute"
        dossier.mkdir(exist_ok=True)
        copies = vides = 0
        for recul in range(1, MINUTE_RECUL + 1):
            jour = (aujourdhui - timedelta(days=recul)).isoformat()
            fichier = dossier / f"garmin-connect-{nom}-{jour}.json"
            if fichier.exists() and recul > MINUTE_RELUS:
                vides = 0
                continue
            valeurs = lire(jour)
            if any(valeurs.values()):
                fichier.write_text(json.dumps(valeurs))
                copies, vides = copies + 1, 0
            else:
                vides += 1
                if vides == MINUTE_VIDES:
                    break
        return copies

    def poids(annee, ancien, fin):
        valeurs = []
        reponse = demander(api.get_weigh_ins, f"{annee}-01-01", fin.isoformat()) or {}
        for jour in reponse.get("dailyWeightSummaries", []):
            mesures = [m for m in jour.get("allWeightMetrics", [])
                       if not (m.get("sourceType") == "MANUAL" and m.get("calendarDate") in envoyees
                               and abs((m.get("weight") or 0) / 1000 - envoyees[m["calendarDate"]]) <= 0.05)]
            if mesures:
                valeurs.append({"summaryDate": jour.get("summaryDate"), "allWeightMetrics": mesures})
        return valeurs

    def copier(nom, annee, lire):
        fichier = GARMIN_CONNECT / f"garmin-connect-{nom}-{annee}.json"
        fin = min(date(annee, 12, 31), aujourdhui)
        ancien = json.loads(fichier.read_text()) if fichier.exists() else {"jusqu_au": None, "valeurs": []}
        if annee < aujourdhui.year and ancien["jusqu_au"] == fin.isoformat():
            return ancien["valeurs"]
        valeurs = lire(annee, ancien, fin)
        fichier.write_text(json.dumps({"jusqu_au": fin.isoformat(), "valeurs": valeurs}))
        return valeurs

    def remonter(nom, lire):
        """Du plus récent au plus ancien, jusqu'à deux années vides de suite."""
        total = vides = 0
        for annee in range(aujourdhui.year, GARMIN_DEPUIS - 1, -1):
            n = len(copier(nom, annee, lire))
            total += n
            vides = 0 if n else vides + 1
            if vides == 2:
                break
        return total

    pesees = 0
    try:
        jours_pas = remonter("pas", pas)
        nuits = remonter("sommeil", sommeil)
        resumes = remonter("jour", resume)
        for annee in range(GARMIN_DEPUIS, aujourdhui.year + 1):
            pesees += sum(len(j["allWeightMetrics"]) for j in copier("poids", annee, poids))
        journees_minute, journees_nuit = journees("minute", minute), journees("nuit", nuit)
    except Exception as e:  # trop de demandes, coupure… : les années déjà copiées sont gardées, la suite au prochain passage
        print(f"Garmin Connect : copie interrompue après {demandes} demande(s) ({e}), reprise au prochain passage")
        return
    print(f"Garmin Connect : {jours_pas} jours de pas, {nuits} nuits, {resumes} résumés du jour, {pesees} pesée(s) et "
          f"{journees_minute} journée(s) à la minute et {journees_nuit} au quart d'heure en copie dans {GARMIN_CONNECT} "
          f"({demandes} demande(s), lecture seule)")


def copier_activites_garmin(api):
    """Télécharge chez Garmin le FIT d'origine des activités enregistrées par une montre, dans Activity/garmin-connect/ de
    la sauvegarde de la montre, où « preparer » les lit comme les siennes : une sortie arrive ainsi dans la bibliothèque
    sans branchement USB. Seules les activités d'un appareil Garmin (« manufacturer » GARMIN, avec un deviceId) sont prises :
    celles que relais.py a envoyées sont DEVELOPMENT, et les reprendre créerait une boucle de synchro. La liste complète du
    compte reste dans garmin-connect-liste-AAAA.json ; une année terminée n'est plus relue. Renvoie le nombre de nouvelles activités."""
    dossier = ARCHIVE / "Activity" / "garmin-connect"
    dossier.mkdir(parents=True, exist_ok=True)
    GARMIN_CONNECT.mkdir(parents=True, exist_ok=True)
    aujourdhui = date.today()
    demandes = nouvelles = 0

    def liste(annee):
        nonlocal demandes
        fichier = GARMIN_CONNECT / f"garmin-connect-liste-{annee}.json"
        fin = min(date(annee, 12, 31), aujourdhui)
        ancien = json.loads(fichier.read_text()) if fichier.exists() else {"jusqu_au": None, "valeurs": []}
        if annee < aujourdhui.year and ancien["jusqu_au"] == fin.isoformat():
            return ancien["valeurs"]
        debut = date(annee, 1, 1)
        if ancien["jusqu_au"]:
            debut = max(debut, date.fromisoformat(ancien["jusqu_au"]) - timedelta(days=7))
        vues = {a["activityId"]: a for a in ancien["valeurs"]}
        while debut <= fin:  # mois par mois : Garmin refuse de lister au-delà de 10 000 activités, et une année en a davantage
            bout = min(date(debut.year + debut.month // 12, debut.month % 12 + 1, 1) - timedelta(days=1), fin)
            rang = 0
            while True:
                page = api.connectapi(api.garmin_connect_activities, params={
                    "startDate": debut.isoformat(), "endDate": bout.isoformat(), "start": str(rang), "limit": "1000"}) or []
                demandes += 1
                time.sleep(PAUSE_GARMIN)
                for a in page:
                    vues[a["activityId"]] = {"activityId": a["activityId"], "startTimeGMT": a.get("startTimeGMT"), "activityName": a.get("activityName"),
                                             "deviceId": a.get("deviceId"), "manufacturer": a.get("manufacturer"), "typeKey": (a.get("activityType") or {}).get("typeKey"),
                                             "duration": a.get("duration"), "distance": a.get("distance")}
                rang += len(page)
                if len(page) < 1000:
                    break
            debut = bout + timedelta(days=1)
        valeurs = sorted(vues.values(), key=lambda a: a["startTimeGMT"] or "")
        fichier.write_text(json.dumps({"jusqu_au": fin.isoformat(), "valeurs": valeurs}))
        return valeurs

    total = vides = 0
    try:
        for annee in range(aujourdhui.year, GARMIN_DEPUIS - 1, -1):
            tout = liste(annee)
            activites = [a for a in tout if a.get("manufacturer") == "GARMIN" and a.get("deviceId")]
            total += len(activites)
            vides = 0 if tout else vides + 1
            for a in activites:
                fit = dossier / f"{a['activityId']}.fit"
                if fit.exists():
                    continue
                archive = api.download_activity(a["activityId"], dl_fmt=api.ActivityDownloadFormat.ORIGINAL)
                demandes += 1
                time.sleep(PAUSE_GARMIN)
                with zipfile.ZipFile(io.BytesIO(archive)) as z:
                    noms = [n for n in z.namelist() if n.lower().endswith(".fit")]
                    if noms:
                        fit.write_bytes(z.read(noms[0]))
                        nouvelles += 1
            if vides == 2:
                break
    except Exception as e:  # les activités déjà copiées sont gardées, la suite au prochain passage
        print(f"Garmin Connect : copie des activités interrompue après {demandes} demande(s) ({e}), reprise au prochain passage")
        return nouvelles
    print(f"Garmin Connect : {total} activités de la montre, {nouvelles} nouvelle(s) ({demandes} demande(s), lecture seule)")
    return nouvelles


COLLECTE = "daily_avg,sleep,steps,heartrate,stress,breathing,hrv,fitness_age,vo2,race_prediction,body_composition,lifestyle," \
           "hydration,training_readiness,training_status,hill_score,endurance_score,blood_pressure"  # tout sauf activity : voir copier_activites_garmin


def collecte_garmin_grafana(api, debut, fin):
    """Le code de garmin-grafana, inchangé (garmin_grafana/), avec la session de relais.py et une base locale à la place d'InfluxDB."""
    GARMIN_CONNECT.mkdir(parents=True, exist_ok=True)
    os.environ.update(GARMIN_GRAFANA_BASE=str(GARMIN_CONNECT / "garmin-grafana.sqlite"), FETCH_SELECTION=COLLECTE, IGNORE_ERRORS="True",
                      USER_TIMEZONE="Europe/Paris", RATE_LIMIT_CALLS_SECONDS=str(PAUSE_GARMIN), LOG_LEVEL="WARNING")
    sys.path.insert(0, str(Path(__file__).resolve().parent / "garmin_grafana"))
    import garmin_fetch

    garmin_fetch.garmin_obj = api
    garmin_fetch.fetch_write_bulk(debut, fin)
    print(f"garmin-grafana : collecte du {debut} au {fin} dans {GARMIN_CONNECT / 'garmin-grafana.sqlite'}")


def cmd_collecte():
    """relais.py collecte [début [fin]] : par défaut les 3 derniers jours. Une période longue prend environ 30 s par jour."""
    utiliser_venv()
    args = sys.argv[2:]
    debut = args[0] if args else (date.today() - timedelta(days=3)).isoformat()
    collecte_garmin_grafana(connexion_garmin(), debut, args[1] if len(args) > 1 else date.today().isoformat())


def copier_compte_garmin(api):
    """Le reste du compte, en lecture seule, dans garmin-connect/compte.json : appareils, records, badges, matériel, objectifs,
    défis, plans, entraînements et parcours. Ni le nom ni l'adresse du profil : ils ne servent à rien ici. Une rubrique que Garmin
    refuse reste simplement vide. S'y ajoutent les séries de musculation de chaque séance, dans exercices/<activité>.json."""
    def lire(fonction, *args, **options):
        time.sleep(PAUSE_GARMIN)
        try:
            return fonction(*args, **options)
        except Exception as e:
            print(f"Garmin Connect : {fonction.__name__} indisponible ({str(e)[:80]})")
            return None

    base = lire(api.get_user_profile_base) or {}
    numero = str(base.get("userProfilePk") or "")
    materiel = (lire(api.get_gear, numero) or []) if numero else []
    for g in materiel:
        g["stats"] = lire(api.get_gear_stats, g["uuid"])
    compte = {"date": date.today().isoformat(), "appareils": lire(api.get_devices), "records": lire(api.get_personal_record), "badges": lire(api.get_earned_badges),
              "materiel": materiel, "objectifs": {etat: lire(api.get_goals, etat) for etat in ("active", "future", "past")}, "defis": lire(api.get_adhoc_challenges, 0, 100),
              "plans": lire(api.get_training_plans), "entrainements": lire(api.get_workouts), "parcours": lire(api.get_courses) if hasattr(api, "get_courses") else None}
    GARMIN_CONNECT.mkdir(parents=True, exist_ok=True)
    (GARMIN_CONNECT / "compte.json").write_text(json.dumps(compte, ensure_ascii=False))

    dossier = GARMIN_CONNECT / "exercices"
    dossier.mkdir(exist_ok=True)
    copies = 0
    for fichier in GARMIN_CONNECT.glob("garmin-connect-liste-*.json"):
        for a in json.loads(fichier.read_text())["valeurs"]:
            cible = dossier / f"{a['activityId']}.json"
            if a.get("typeKey") == "strength_training" and a.get("manufacturer") == "GARMIN" and not cible.exists():
                series = lire(api.get_activity_exercise_sets, a["activityId"])
                if series:
                    cible.write_text(json.dumps(series, ensure_ascii=False))
                    copies += 1
    print(f"Garmin Connect : compte copié ({len(compte['records'] or [])} records, {len(compte['badges'] or [])} badges, {copies} séance(s) de musculation détaillée(s))")


def cmd_reconvertir():
    """La conversion demandée à la main : les statistiques suivent la bibliothèque."""
    utiliser_venv()
    cmd_preparer()
    ecrire_performance()


def cmd_mot_de_passe():
    import acces

    if "--retirer" in sys.argv:
        acces.retirer()
        print("Mot de passe retiré : l'interface est ouverte sans connexion.")
        return
    mot = getpass.getpass("Nouveau mot de passe : ")
    if mot != getpass.getpass("Encore une fois : "):
        sys.exit("Les deux saisies sont différentes : rien n'a changé.")
    try:
        acces.definir(mot)
    except ValueError as e:
        sys.exit(str(e))
    print("Mot de passe enregistré. Tous les appareils doivent se reconnecter.")


def cmd_certificat():
    import acces

    if len(sys.argv) < 3:
        sys.exit("Indiquez l'adresse de l'ordinateur sur le réseau : relais.py certificat 192.168.1.10")
    if (acces.CERTIFICATS / "relais.crt").exists() and "--refaire" not in sys.argv:
        print("Le certificat existe déjà. --refaire le remplace : il faudra le réinstaller sur le téléphone.")
        return
    try:
        acces.creer_certificat(sys.argv[2])
    except ValueError as e:
        sys.exit(str(e))
    print("Certificat créé : redémarrez relais pour l'utiliser.")


def cmd_garmin():
    """Récupère les nouvelles activités de la montre chez Garmin et, s'il y en a, refait la bibliothèque."""
    utiliser_venv()
    if copier_activites_garmin(connexion_garmin()):
        cmd_preparer()
        ecrire_performance()


def copier_smashrun():
    """Copie en lecture seule, dans sante/smashrun.json, ce que seul Smashrun sait : météo de chaque course, badges et
    statistiques. Une dizaine de demandes par passage. Son jeton dure 12 semaines : s'il a expiré, la copie précédente reste."""
    conf = config()
    if not conf.get("SMASHRUN_TOKEN"):
        return
    import destinations

    def lire(chemin):
        time.sleep(PAUSE_GARMIN)
        statut, reponse = destinations._requete(destinations.Smashrun.URL + chemin, entetes={"Authorization": "Bearer " + conf["SMASHRUN_TOKEN"]})
        if statut != 200:
            raise RuntimeError(f"réponse {statut}")
        return reponse

    try:
        courses, page = [], 0
        while True:
            lot = lire(f"/my/activities/search?count=100&page={page}")
            courses += [{c: a.get(c) for c in ("startDateTimeLocal", "temperature", "temperatureApparent", "humidity", "windSpeed", "weatherType")} for a in lot]
            page += 1
            if len(lot) < 100:
                break
        copie = {"stats": lire("/my/stats"), "annee": lire(f"/my/stats/{date.today().year}"), "badges": lire("/my/badges"), "courses": courses}
    except (OSError, RuntimeError, ValueError, destinations.Limite) as e:
        print(f"Smashrun : copie non mise à jour ({e})")
        return
    SANTE.mkdir(parents=True, exist_ok=True)
    (SANTE / "smashrun.json").write_text(json.dumps(copie, ensure_ascii=False))
    print(f"Smashrun : {len(courses)} courses et {len(copie['badges'])} badges copiés")


def ecrire_performance():
    import performance

    try:
        performance.ecrire(V2, SANTE)
    except OSError as e:  # bibliothèque pas encore préparée
        print(f"Performance : non calculée ({e})")


def faire_sante(api_garmin=None):
    import sante

    print("Lecture des pas, du poids et du sommeil de toutes les sources…")
    racines = list(sources().values())
    if not any(ARCHIVE.resolve().is_relative_to(r.resolve()) for r in racines):
        racines.append(ARCHIVE)  # sauvegarde de la montre rangée hors des sources (chemins.yml)
    if api_garmin is not None:
        copier_garmin(api_garmin)
        copier_compte_garmin(api_garmin)
        try:
            collecte_garmin_grafana(api_garmin, (date.today() - timedelta(days=3)).isoformat(), date.today().isoformat())
        except Exception as e:  # la santé et les pesées ne doivent pas en dépendre
            print(f"garmin-grafana : collecte interrompue ({e})")
    racines.append(GARMIN_CONNECT)  # exclu des autres racines : lu ici seulement
    pas, poids, sommeil, montre, lus = sante.collecter(racines, EXCLUS_PROJET | {"convertis", SANTE.name})
    print(f"  {lus['pas']} valeurs de pas, {lus['poids']} pesées, {lus['sommeil']} nuits lues ; {lus['écartés']} vides ou aberrantes écartées")
    print(f"  retenu après dédoublonnage : {len(pas)} jours de pas, {len(poids)} jours de poids, {len(sommeil)} nuits, "
          f"{len(montre)} journées de la montre")
    sante.ecrire_csv(SANTE, pas, poids, sommeil, montre)
    sante.ecrire_journees_garmin(GARMIN_CONNECT, SANTE)
    print(f"  détail : {SANTE}/pas.csv, poids.csv, sommeil.csv, montre.csv, garmin.csv")
    copier_sommeil()
    copier_smashrun()
    ecrire_performance()
    if api_garmin is not None:
        poids_garmin(api_garmin, poids)


def cmd_sante():
    utiliser_venv()
    try:
        api = connexion_garmin()
    except Exception as e:
        print(f"Garmin : pas de connexion ({e}), les pesées ne sont pas envoyées")
        api = None
    faire_sante(api)


def cmd_auto():
    """Tout en une fois : montre, conversion précise (v2) de toutes les sources, santé, puis envoi vers les
    destinations choisies (SPORT_AUTO_DESTINATIONS=garmin,intervals… dans sport.env ; aucune par défaut)."""
    utiliser_venv()
    conf = config()
    etapes = 0

    def etape(titre):
        nonlocal etapes
        etapes += 1
        print(f"\n=== {etapes}. {titre} ===", flush=True)

    if trouver_montre():
        etape("Montre Garmin")
        try:
            cmd_montre()
        except SystemExit as e:
            print(e)

    api = None
    try:
        api = connexion_garmin()
    except Exception as e:
        print(f"Garmin : pas de connexion ({e}), ses activités ne sont pas lues et les pesées pas envoyées")
    if api is not None:
        etape("Activités de la montre sur Garmin Connect")
        copier_activites_garmin(api)

    etape("Conversion précise de toutes les sources (activites-v2)")
    cmd_preparer()

    etape("Pas, poids et sommeil")
    faire_sante(api)

    choisies = [d.strip() for d in conf.get("SPORT_AUTO_DESTINATIONS", "").split(",") if d.strip() in DESTINATIONS]
    for nom in choisies:
        etape(f"Envoi vers {nom}")
        envoyer_v2(nom, int(conf.get("SPORT_AUTO_NOMBRE", AUTO_GARMIN)), reessayer=True)
    if not choisies:
        print("\nAucune destination automatique (SPORT_AUTO_DESTINATIONS dans sport.env) : rien n'est envoyé.")
    print("\nMode automatique terminé.")


def cmd_refaire():
    """relais.py refaire garmin : retire de Garmin Connect les activités envoyées par l'ancienne version
    (outils/historique-v1/envoyes-garmin.csv : départ et distance de chacune), pour les remplacer par activites-v2.

    Une activité n'est retirée que si Garmin en a exactement une qui démarre à moins d'une minute et dont la
    distance correspond (2 % ou 20 m) : les autres sont signalées et laissées. Journal : historique-v1/.retirees-garmin.txt"""
    if sys.argv[2:3] != ["garmin"]:
        sys.exit("Usage : relais.py refaire garmin")
    utiliser_venv()
    from datetime import datetime, timedelta, timezone

    liste = PROJET / "outils" / "historique-v1" / "envoyes-garmin.csv"
    journal = liste.parent / ".retirees-garmin.txt"
    deja = set(journal.read_text().split()) if journal.exists() else set()
    with liste.open() as f:
        a_retirer = [l for l in csv.DictReader(f) if l["empreinte"] not in deja and l["debut_utc"]]
    print(f"{len(a_retirer)} activité(s) de l'ancienne version à retirer de Garmin")
    if not a_retirer:
        return
    api = connexion_garmin()
    jours = sorted({datetime.fromisoformat(l["debut_utc"]).date() for l in a_retirer})
    sur_garmin, debut = [], jours[0] - timedelta(days=1)
    while debut <= jours[-1]:  # par tranches de 30 jours
        fin = debut + timedelta(days=30)
        sur_garmin += api.get_activities_by_date(debut.isoformat(), fin.isoformat()) or []
        debut = fin + timedelta(days=1)
    compte = Counter()
    with journal.open("a") as j:
        for l in a_retirer:
            depart = datetime.fromisoformat(l["debut_utc"])
            distance = float(l["distance_m"] or 0)
            candidates = [a for a in sur_garmin
                          if abs((datetime.fromisoformat(a["startTimeGMT"]).replace(tzinfo=timezone.utc) - depart).total_seconds()) <= 60
                          and abs((a.get("distance") or 0) - distance) <= max(20, 0.02 * distance)]
            if len(candidates) != 1:
                compte["introuvables ou ambiguës, laissées"] += 1
                continue
            api.delete_activity(candidates[0]["activityId"])
            sur_garmin.remove(candidates[0])
            j.write(l["empreinte"] + "\n")
            j.flush()
            compte["retirées de Garmin"] += 1
            time.sleep(1)
    print(", ".join(f"{n} {s}" for s, n in compte.items()))


def orphelines(nom):
    """Activités envoyées à un service qui ne sont plus dans activites-v2/ et dont aucune activité actuelle n'a besoin
    (ex. le moins bon exemplaire de deux activités au même départ, confondues après coup) : [ligne du journal]."""
    with (V2 / "index.csv").open() as f:
        actuelles = {a["fichier"] for a in csv.DictReader(f)}
    etat = etat_envois(nom)
    # une activité actuelle « envoyée sous » un ancien nom a besoin de cet envoi-là
    utiles = {s.split("envoyée sous ", 1)[1] for f, s in etat.items() if f in actuelles and "envoyée sous " in s}
    vues = set()
    lignes = []
    for l in lignes_envois(nom):
        if etat[l[0]] == "envoye" and l[0] not in actuelles and l[0] not in utiles and l[2] and l[0] not in vues:
            vues.add(l[0])
            lignes.append(l)
    return lignes


def sur_le_service(nom):
    """(liste(jour) → [(identifiant, départ UTC, durée s, distance m ou None)], supprimer(identifiant)) pour les
    services qui permettent de retirer une activité."""
    import destinations

    if nom == "garmin":
        api = connexion_garmin()

        def liste(jour):
            return [(a["activityId"], datetime.fromisoformat(a["startTimeGMT"]).replace(tzinfo=timezone.utc), a.get("duration") or 0,
                     a.get("distance")) for a in api.get_activities_by_date(jour.isoformat(), (jour + timedelta(days=1)).isoformat()) or []]
        return liste, api.delete_activity
    if nom == "smashrun":
        service = destinations.Smashrun(config())
        toutes = service.activites()
        return (lambda jour: [a for a in toutes if abs((a[1].date() - jour).days) <= 1]), service.supprimer
    raise SystemExit(f"{nom} ne permet pas de retirer une activité par son API : à faire sur son site")


def cmd_retirer():
    """relais.py retirer <garmin|smashrun> [--essai] : retire du service les activités que relais.py y a
    envoyées et qui ne sont plus dans activites-v2/ (voir orphelines). Sur le service, on cherche la même durée (2 % +
    30 s), la même distance (2 % + 20 m, ou aucune) et le même départ (1 min, ou un peu plus tard : le service prend
    parfois le premier point de la trace) : l'exemplaire gardé, parti à la même seconde mais plus long, n'est pas touché.
    S'il y en a plusieurs (copies d'une ancienne version ou d'une synchro), seule la dernière arrivée, celle de relais.py,
    est retirée. --essai montre sans rien retirer."""
    args = sys.argv[2:]
    if not args or args[0] not in DESTINATIONS:
        sys.exit(f"Usage : relais.py retirer <{'|'.join(DESTINATIONS)}> [--essai]")
    nom, essai = args[0], "--essai" in args
    utiliser_venv()
    a_retirer = orphelines(nom)
    print(f"{nom} : {len(a_retirer)} activité(s) envoyée(s) qui ne sont plus dans la bibliothèque")
    if not a_retirer:
        return
    if nom not in ("garmin", "smashrun"):
        for fichier, _, debut, duree in a_retirer:
            print(f"  {debut[:16].replace('T', ' ')} UTC, {duree} min : {fichier}")
        sys.exit(f"{nom} ne permet pas de retirer une activité par son API : à retirer sur son site")
    liste, supprimer = sur_le_service(nom)
    compte = Counter()
    with journal_envois(nom).open("a") as journal:
        for fichier, _, debut, duree in a_retirer:
            depart, secondes = datetime.fromisoformat(debut), float(duree) * 60
            distance = distance_envoyee(fichier)
            candidates = [a for a in liste(depart.astimezone(timezone.utc).date())
                          if -60 <= (a[1] - depart).total_seconds() <= max(60, secondes / 3)
                          and abs(a[2] - secondes) <= 0.02 * secondes + 30
                          and abs((a[3] or 0) - (distance or 0)) <= 0.02 * (distance or 0) + 20]
            if not candidates:
                print(f"  {fichier} : introuvable sur {nom}, laissée")
                compte["introuvables"] += 1
                continue
            identifiant = max(c[0] for c in candidates)  # numéros croissants : la dernière arrivée
            if essai:
                print(f"  {fichier} → {nom} n° {identifiant}" + (f" (sur {len(candidates)} copies)" if len(candidates) > 1 else ""))
                compte["à retirer"] += 1
                continue
            supprimer(identifiant)
            journal.write(f"{fichier}\tretiree\t{debut}\t{duree}\n")
            journal.flush()
            compte["retirées"] += 1
            time.sleep(1)
    print(", ".join(f"{n} {s}" for s, n in compte.items()))


def distance_envoyee(fichier):
    """Distance (m) d'une activité envoyée, d'après la version de activites-v2/ où elle figurait (a-supprimer/), ou None."""
    for index in sorted(CORBEILLE.glob(f"*/{V2.name}/index.csv"), reverse=True):
        with index.open() as f:
            for a in csv.DictReader(f):
                if a["fichier"] == fichier:
                    return float(a["distance_km"]) * 1000 if a["distance_km"] else None
    return None


def ecrire_config(**valeurs):
    """Ajoute ou remplace des clés dans sport.env (lisible par vous seul)."""
    lignes = CONFIG.read_text().splitlines() if CONFIG.exists() else []
    lignes = [l for l in lignes if l.split("=", 1)[0].strip() not in valeurs]
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text("\n".join(lignes + [f"{k}={v}" for k, v in valeurs.items()]) + "\n")
    CONFIG.chmod(0o600)


def copier_sommeil():
    """Le sommeil n'existe pas en FIT importable : activites-v2/sommeil/ en garde le CSV (une nuit par ligne)."""
    if (SANTE / "sommeil.csv").exists() and V2.exists():
        (V2 / "sommeil").mkdir(exist_ok=True)
        shutil.copyfile(SANTE / "sommeil.csv", V2 / "sommeil" / "sommeil.csv")
        print(f"  sommeil : {V2 / 'sommeil' / 'sommeil.csv'}")


def cmd_preparer():
    """relais.py preparer : conversion précise (v2) de toutes les sources vers activites-v2/, puis audit.

    Voir analyse/ANALYSE.md : un lecteur par source (heure, fuseau, durée, sport), regroupement de la même sortie
    vue par plusieurs applis, meilleur exemplaire, filtres, un FIT par activité. L'ancienne version de
    activites-v2/ est déplacée dans a-supprimer/ (jamais effacée)."""
    utiliser_venv()
    import statistics

    import audit
    import regrouper
    import seances

    print("Lecture de toutes les sources…", flush=True)
    racines = list(sources().values())
    seances.DOSSIER_MONTRE = ARCHIVE.name
    toutes = seances.lire_sources(racines, EXCLUS_PROJET)
    print("Regroupement, choix du meilleur exemplaire, filtres…", flush=True)
    gardees, ecartees = regrouper.preparer(toutes)
    if V2.exists():
        ancienne = CORBEILLE / time.strftime("%Y-%m-%d_%H%M") / V2.name
        ancienne.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(V2, ancienne)
        print(f"  ancienne version déplacée dans {ancienne}")
    print("Écriture des FIT…", flush=True)
    regrouper.ecrire(gardees, ecartees, V2)
    copier_sommeil()
    print("Contrôle des fichiers produits…", flush=True)
    index, problemes, vitesses = audit.auditer_v2(V2)
    print(f"  {len(index)} activités, {len(problemes)} problème(s)")
    for p in problemes[:10]:
        print(f"    {p[0]} : {p[1]} {p[2]}")
    for sport, v in sorted(vitesses.items()):
        print(f"  {sport:9} vitesse médiane {statistics.median(v):.1f} km/h ({len(v)} activités avec distance)")
    print(f"Détail : {V2 / 'index.csv'} (retenues) et {V2 / 'ecartees.csv'} (écartées, avec la raison)")


def main():
    commandes = {
        "etat": cmd_etat,
        "montre": cmd_montre,
        "vers-montre": cmd_vers_montre,
        "satellites": cmd_satellites,
        "envoyer": cmd_envoyer,
        "sante": cmd_sante,
        "auto": cmd_auto,
        "garmin": cmd_garmin,
        "collecte": cmd_collecte,
        "mot-de-passe": cmd_mot_de_passe,
        "certificat": cmd_certificat,
        "refaire": cmd_refaire,
        "retirer": cmd_retirer,
        "preparer": cmd_reconvertir,
        "lots": cmd_lots,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commandes:
        sys.exit(__doc__)
    if sys.argv[1] in COMMANDES_EXCLUSIVES:
        # Deux tris ou deux envois en même temps se marcheraient dessus (terminal, interface web, service montre)
        verrou = open(VERROU, "w")  # gardé ouvert jusqu'à la fin du processus
        try:
            fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("Une autre commande relais.py est déjà en cours (terminal ou interface web) : attendez qu'elle finisse.")
    commandes[sys.argv[1]]()


if __name__ == "__main__":
    main()
