#!/usr/bin/env python3
"""Interface web de relais.py (conteneur « relais »).

Chaque bouton lance une commande relais.py et affiche sa sortie en direct. La connexion à Garmin Connect
(mot de passe puis code MFA) se fait ici : les jetons sont enregistrés comme avec le terminal, et
« envoyer garmin » les réutilise. La page montre aussi la bibliothèque : chaque activité, sa carte et ses courbes.

Mode automatique : « relais.py auto » est lancé chaque jour à SPORT_AUTO_HEURE (« 04:00 » par défaut,
« non » pour le désactiver), si aucune autre commande ne tourne ; « relais.py garmin » l'est chaque heure. Port : SPORT_WEB_PORT (8090 par défaut).

Après relais.py certificat, le port suivant sert la même interface en https, et les appareils du réseau y sont renvoyés.
"""
import csv
import json
import urllib.parse
import os
import ssl
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import acces
import carnet
import relais

RELAIS_PY = Path(__file__).resolve().parent / "relais.py"
PAGE = Path(__file__).resolve().parent / "web.html"
CONNEXION = Path(__file__).resolve().parent / "connexion.html"
CERTIFICAT = "/relais-certificat.crt"  # certificat public à installer sur le téléphone
OUVERTS = ("/connexion", "/manifest.webmanifest", "/icone-192.png", "/icone-512.png", CERTIFICAT)  # accessibles sans être connecté
COOKIE = "relais"
LEAFLET = Path(__file__).resolve().parent / "leaflet"  # carte des activités, version 1.9.4 (licence dans ce dossier)
APPLI = Path(__file__).resolve().parent / "appli"  # icône et manifeste, pour l'installer sur un téléphone
TAILLE_ENVOI = 50 * 2 ** 20  # octets, pour une photo ou une activité envoyée depuis la page
PORT = int(os.environ.get("SPORT_WEB_PORT", "8090"))
PORT_HTTPS = PORT + 1
HTTPS = (acces.CERTIFICATS / "relais.crt").exists()  # lu une fois au démarrage, comme le serveur qui en dépend
HEURE_AUTO = os.environ.get("SPORT_AUTO_HEURE", "04:00")
PUBLIC = os.environ.get("SPORT_PUBLIC_URL", "http://localhost:8090")  # adresse de retour des autorisations
# Smashrun rend le jeton dans le fragment de l'adresse (#access_token=…), que seul le navigateur voit : cette page le lit
# et l'enregistre comme le formulaire (vérifié auprès de Smashrun). Le « + » du jeton est gardé tel quel.
RETOUR_SMASHRUN = """<!doctype html><meta charset="utf-8"><title>Smashrun</title><p>Connexion à Smashrun…</p>
<script>
const champs = Object.fromEntries(location.hash.slice(1).split("&").map((p) => p.split("=").map(decodeURIComponent)));
const fin = (r) => location.replace("/?smashrun=" + encodeURIComponent(r));
if (!champs.access_token) fin(champs.error || new URLSearchParams(location.search).get("error") || "autorisation refusée");
else fetch("/api/destination", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nom: "smashrun", champs: { SMASHRUN_TOKEN: champs.access_token } }) })
  .then((r) => r.json().then((d) => fin(r.ok ? "ok" : d.erreur || "refusé")))
  .catch((e) => fin(String(e)));
</script>"""


class Tache:
    """La commande en cours (une seule à la fois) et sa sortie."""

    def __init__(self):
        self.verrou = threading.Lock()
        self.proc = None
        self.commande = ""
        self.lignes = []
        self.code = None

    def en_cours(self):
        return self.proc is not None and self.proc.poll() is None

    def lancer(self, args):
        with self.verrou:
            if self.en_cours():
                return False
            self.commande, self.lignes, self.code = " ".join(args), [], None
            self.proc = subprocess.Popen([sys.executable, "-u", str(RELAIS_PY), *args], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._lire, args=(self.proc,), daemon=True).start()
        return True

    def _lire(self, proc):
        for ligne in proc.stdout:
            self.lignes.append(ligne.rstrip("\n"))
        self.code = proc.wait()

    def arreter(self):
        if self.en_cours():
            self.proc.terminate()


TACHE = Tache()
CONNEXION_GARMIN = {}  # session en attente du code MFA


def prochain_auto(maintenant=None):
    if HEURE_AUTO.lower() in ("", "non", "off"):
        return None
    maintenant = maintenant or datetime.now()
    heure, minute = map(int, HEURE_AUTO.split(":"))
    prochain = maintenant.replace(hour=heure, minute=minute, second=0, microsecond=0)
    return prochain if prochain > maintenant else prochain + timedelta(days=1)


def planificateur():
    """Lance « auto » une fois par jour à l'heure prévue (si une commande tourne déjà, on attend le lendemain)."""
    while (prochain := prochain_auto()) is not None:
        time.sleep(max(1, (prochain - datetime.now()).total_seconds()))
        TACHE.lancer(["auto"])
        time.sleep(61)


def synchro_horaire():
    """Chaque heure, les nouvelles activités de la montre chez Garmin, comme le faisait Endurain. Si une commande tourne, à l'heure suivante."""
    while prochain_auto() is not None:
        time.sleep(3600)
        if relais.JETONS_GARMIN.is_dir() and any(relais.JETONS_GARMIN.iterdir()):
            TACHE.lancer(["garmin"])


def etat():
    montre = relais.trouver_montre()
    conf = relais.config()
    bibliotheque = []
    if relais.V2.exists() and (relais.V2 / "index.csv").exists():
        with (relais.V2 / "index.csv").open() as f:
            bibliotheque = list(csv.DictReader(f))
    destinations = {}
    for nom in relais.DESTINATIONS:
        envois = relais.etat_envois(nom)
        proposees = [l for l in bibliotheque if relais.proposee(nom, l)]  # Smashrun : courses avec distance
        # statuts des seules activités proposées (le journal garde aussi d'anciens noms et des activités non proposées)
        statuts = Counter(envois[l["fichier"]].split(" :")[0] for l in proposees if l["fichier"] in envois)
        # une activité venue de ce service y est déjà (la montre se synchronise avec Garmin) : a_envoyer ne l'envoie jamais
        origine = relais.ORIGINE_SERVICE.get(nom)
        deja_la = sum(l["fichier"] not in envois and l["enregistreur"] == origine for l in proposees)
        destinations[nom] = {"configuree": destination_configuree(nom, conf), "envoye": statuts.get("envoye", 0),
                             "deja": statuts.get("deja", 0) + deja_la, "refuse": statuts.get("refuse", 0), "total": len(proposees),
                             # activités de la bibliothèque actuelle jamais traitées (le journal garde aussi d'anciens noms)
                             "restantes": sum(l["fichier"] not in envois for l in proposees) - deja_la}
    index = relais.V2 / "index.csv"
    return {
        "bibliotheque": {"total": len(bibliotheque),
                         "categories": dict(Counter(l["categorie"] for l in bibliotheque).most_common()),
                         "gps": sum(l["gps"] == "True" for l in bibliotheque),
                         "annees": [min(l["debut_utc"][:4] for l in bibliotheque), max(l["debut_utc"][:4] for l in bibliotheque)] if bibliotheque else None,
                         "convertie_le": datetime.fromtimestamp(index.stat().st_mtime).strftime("%d/%m/%Y à %H:%M") if index.exists() else None},
        "destinations": destinations,
        "montre": str(montre) if montre else None,
        "prochain_auto": prochain.strftime("%d/%m à %H:%M") if (prochain := prochain_auto()) else None,
        "garmin_connecte": relais.JETONS_GARMIN.is_dir() and any(relais.JETONS_GARMIN.iterdir()),
        "mot_de_passe": acces.actif(),
        "commande": TACHE.commande if TACHE.en_cours() else None,
    }


def sante():
    """Valeurs retenues par « relais.py sante » (sante/*.csv), une ligne [date, valeur…] par jour, pour la section Santé."""
    def lire(nom, colonnes):
        fichier = relais.SANTE / nom
        if not fichier.exists():
            return []
        with fichier.open() as f:
            return [[l["date"], *(float(l[c]) if l.get(c) else None for c in colonnes)] for l in csv.DictReader(f)]

    return {"pas": lire("pas.csv", ["pas"]), "poids": lire("poids.csv", ["kg"]), "sommeil": lire("sommeil.csv", ["total_h"]),
            "montre": lire("montre.csv", ["pas", "fc_repos", "stress_moy"]),
            "garmin": lire("garmin.csv", ["fc_repos", "stress_moy", "batterie_max"]),
            "jours_minute": sorted({f.stem[-10:] for f in (relais.GARMIN_CONNECT / "minute").glob("*.json")})}


def performance():
    """Statistiques et conseils calculés par relais.py sante, avec la copie de Smashrun."""
    fichier, smashrun = relais.SANTE / "performance.json", relais.SANTE / "smashrun.json"
    resultat = json.loads(fichier.read_text()) if fichier.exists() else {}
    if resultat and smashrun.exists():
        copie = json.loads(smashrun.read_text())
        resultat["smashrun"] = {c: copie[c] for c in ("stats", "annee", "badges")}
    return resultat


def journee(jour):
    """Détail d'une journée copiée de Garmin Connect ; rien si la journée n'est pas copiée."""
    try:
        fichiers = [relais.GARMIN_CONNECT / "minute" / f"garmin-connect-{nom}-{date.fromisoformat(jour)}.json" for nom in ("minute", "nuit")]
    except ValueError:
        return {}
    return {cle: valeur for f in fichiers if f.exists() for cle, valeur in json.loads(f.read_text()).items()}


def activites():
    """La bibliothèque (activites-v2/index.csv) pour la section Activités, la plus récente d'abord."""
    index = relais.V2 / "index.csv"
    if not index.exists():
        return []
    colonnes = ("fichier", "categorie", "debut_utc", "duree_min", "distance_km", "vitesse_kmh", "gps", "source", "remarques")
    noms = activites_montre()
    with index.open() as f:
        lignes = [{**{c: l[c] for c in colonnes}, "nom_garmin": noms.get(l["debut_utc"][:19], {}).get("activityName"),
                   "type_garmin": noms.get(l["debut_utc"][:19], {}).get("typeKey")} for l in csv.DictReader(f)]
    return sorted(lignes, key=lambda l: l["debut_utc"], reverse=True)


def activites_montre():
    """Les activités de la montre chez Garmin, par heure de départ UTC (AAAA-MM-JJTHH:MM:SS)."""
    resultat = {}
    for fichier in relais.GARMIN_CONNECT.glob("garmin-connect-liste-*.json"):
        for a in json.loads(fichier.read_text())["valeurs"]:
            if a.get("startTimeGMT") and a.get("manufacturer") == "GARMIN":
                resultat[a["startTimeGMT"].replace(" ", "T")[:19]] = a
    return resultat


def compte_garmin():
    """La copie du compte, sans les records tirés d'imports : pour Garmin, un total journalier de 2014 est une course de 106 km.
    Un record lié à une activité ne compte que si la montre l'a mesurée ; ceux des pas n'ont pas d'activité."""
    fichier = relais.GARMIN_CONNECT / "compte.json"
    if not fichier.exists():
        return {}
    compte = json.loads(fichier.read_text())
    montre = {a["activityId"] for a in activites_montre().values()}
    compte["records"] = [r for r in compte.get("records") or [] if not r.get("activityId") or r["activityId"] in montre]
    return compte


NOMS_EXERCICES = {"UNKNOWN": "Non reconnu par la montre", "SIT_UP": "Abdominaux", "CRUNCH": "Abdominaux", "PUSH_UP": "Pompes", "CARDIO": "Cardio", "LATERAL_RAISE": "Élévations latérales",
                  "TRICEPS_EXTENSION": "Triceps", "ROW": "Tirage", "SQUAT": "Squats", "DEADLIFT": "Soulevé de terre", "BENCH_PRESS": "Développé couché", "WARM_UP": "Échauffement",
                  "HYPEREXTENSION": "Lombaires", "PLANK": "Gainage", "LUNGE": "Fentes", "CURL": "Curl", "PULL_UP": "Tractions", "SHOULDER_PRESS": "Développé épaules"}


def details_sports():
    """Ce qui n'existe que pour certains sports, lu dans les données d'origine de la montre : les exercices de musculation
    (garmin-connect/exercices/) et les longueurs de natation (leurs FIT d'origine ; la bibliothèque n'en garde pas le détail)."""
    from garmin_fit_sdk import Decoder, Stream

    par_exercice, seances_muscu = {}, []
    for fichier in (relais.GARMIN_CONNECT / "exercices").glob("*.json"):
        actives = [s for s in json.loads(fichier.read_text()).get("exerciseSets") or [] if s.get("setType") == "ACTIVE"]
        for s in actives:
            categorie = ((s.get("exercises") or [{}])[0].get("category")) or "UNKNOWN"
            e = par_exercice.setdefault(categorie, {"nom": NOMS_EXERCICES.get(categorie, categorie.replace("_", " ").capitalize()), "series": 0, "repetitions": 0, "poids_max": None})
            e["series"] += 1
            e["repetitions"] += s.get("repetitionCount") or 0
            if s.get("weight"):
                e["poids_max"] = max(e["poids_max"] or 0, s["weight"] / 1000)
        if actives:
            seances_muscu.append({"jour": min(s["startTime"] for s in actives if s.get("startTime"))[:10], "series": len(actives),
                                  "repetitions": sum(s.get("repetitionCount") or 0 for s in actives),
                                  "volume": round(sum((s.get("weight") or 0) / 1000 * (s.get("repetitionCount") or 0) for s in actives))})
    natation = []
    for a in activites_montre().values():
        fit = relais.ARCHIVE / "Activity" / "garmin-connect" / f"{a['activityId']}.fit"
        if a.get("typeKey") in ("lap_swimming", "open_water_swimming") and fit.exists():
            messages, _ = Decoder(Stream.from_file(str(fit))).read()
            s = (messages.get("session_mesgs") or [{}])[0]
            natation.append({"jour": a["startTimeGMT"][:10], "metres": s.get("total_distance"), "secondes": s.get("total_timer_time"), "longueurs": s.get("num_active_lengths"),
                             "bassin": s.get("pool_length"), "mouvements": s.get("total_strokes"), "swolf": s.get("avg_swolf"), "eau_libre": a["typeKey"] == "open_water_swimming"})
    return {"exercices": sorted(par_exercice.values(), key=lambda e: -e["series"]), "musculation": sorted(seances_muscu, key=lambda s: s["jour"]),
            "natation": sorted(natation, key=lambda n: n["jour"], reverse=True)}


def medias(debut_utc):
    dossier = carnet.dossier_medias(relais.SANTE, debut_utc)
    return sorted(f.name for f in dossier.iterdir()) if dossier.is_dir() else []


def recevoir(dossier, nom, types, donnees):
    """Enregistre un fichier envoyé par la page, sans jamais en écraser un autre."""
    if len(donnees) > TAILLE_ENVOI:
        raise ValueError("fichier trop gros (50 Mo au plus)")
    dossier.mkdir(parents=True, exist_ok=True)
    cible = dossier / carnet.nom_sur(nom, types)
    while cible.exists():
        cible = cible.with_stem(cible.stem + "-2")
    cible.write_bytes(donnees)
    return {"ok": True, "nom": cible.name}


def fit_bibliotheque(fichier):
    """Chemin d'un FIT de la bibliothèque, ou None : aucun autre fichier n'est lu."""
    chemin = (relais.V2 / urllib.parse.unquote(fichier)).resolve()
    return chemin if chemin.suffix == ".fit" and chemin.is_relative_to(relais.V2.resolve()) and chemin.is_file() else None


def meteo_smashrun(depart):
    """La météo que Smashrun associe à une course partie à cette heure, à 2 min près."""
    fichier = relais.SANTE / "smashrun.json"
    if not fichier.exists():
        return None
    moment = datetime.fromisoformat(depart)
    return next((c for c in json.loads(fichier.read_text())["courses"] if c.get("temperature") is not None
                 and abs((datetime.fromisoformat(c["startDateTimeLocal"]) - moment).total_seconds()) <= 120), None)


FENETRES = ((36500, "de tout votre historique"), (365, "en un an"), (90, "en 3 mois"), (30, "en un mois"))


def vitesse(s):
    """km/h en mouvement : c'est la vraie allure, les arrêts n'y comptent pas."""
    return 60 * s["km"] / (s["mouvement"] or s["minutes"] or 1)


def reperes(fichier):
    """Où se place une activité parmi celles de sa catégorie : rangs, ce qu'elle a de remarquable à sa date, et les plus
    rapides sur une distance voisine (±10 %). La vitesse ne compare que des séances de montre : voir performance.py."""
    fichier_perf = relais.SANTE / "performance.json"
    if not fichier_perf.exists():
        return {}
    perf = json.loads(fichier_perf.read_text())
    s = next((x for x in perf.get("seances", []) if x["fichier"] == fichier), None)
    if not s:
        return {}
    resultat = {"zones": s["zones"], "fc_max": perf["fc_max"], "charge": s["charge"], "notables": [], "rangs": [], "semblables": []}
    groupe = [x for x in perf["seances"] if x["categorie"] == s["categorie"]]
    avec_km = [x for x in groupe if x["km"] and x["gps"]]
    if not s["km"] or not s["gps"]:
        return resultat
    semblables = sorted((x for x in avec_km if x["montre"] and .9 * s["km"] <= x["km"] <= 1.1 * s["km"]), key=vitesse, reverse=True)
    resultat["rangs"].append({"nom": "Par la distance", "rang": 1 + sum(x["km"] > s["km"] for x in avec_km), "sur": len(avec_km)})
    if s in semblables:
        resultat["rangs"].append({"nom": f"Par la vitesse, sur {round(s['km'])} km environ", "rang": semblables.index(s) + 1, "sur": len(semblables)})
    resultat["semblables"] = [{"fichier": x["fichier"], "jour": x["jour"], "km": x["km"], "kmh": round(vitesse(x), 2)} for x in semblables[:10]]

    def remarquable(valeur, texte, candidates):
        """La plus longue période sur laquelle cette séance bat toutes les précédentes (à sa date, pas à aujourd'hui)."""
        jour = date.fromisoformat(s["jour"])
        for jours, periode in FENETRES:
            avant = [x for x in candidates if x is not s and valeur(x) is not None and 0 <= (jour - date.fromisoformat(x["jour"])).days <= jours]
            if len(avant) >= 3 and valeur(s) is not None and all(valeur(s) > valeur(x) for x in avant):
                resultat["notables"].append(f"{texte} {periode}")
                return

    remarquable(lambda x: x["km"], "Sortie la plus longue", avec_km)
    remarquable(vitesse, f"{round(s['km'])} km le plus rapide", [x for x in semblables if x["jour"] <= s["jour"]] if s["montre"] else [])
    remarquable(lambda x: x["fc"], "FC moyenne la plus haute", avec_km)
    remarquable(lambda x: x["montee"], "Le plus de dénivelé", avec_km)
    remarquable(lambda x: x["charge"], "Séance la plus chargée", groupe)
    return resultat


def activite(fichier):
    import fitconv

    chemin = fit_bibliotheque(fichier)
    if not chemin:
        return {}
    detail = fitconv.lire_detail(chemin)
    if detail:
        detail["meteo"] = meteo_smashrun(detail["depart"])
        detail["reperes"] = reperes(urllib.parse.unquote(fichier))
        exercices = relais.GARMIN_CONNECT / "exercices" / f"{activites_montre().get(detail['depart'][:19], {}).get('activityId')}.json"
        detail["exercices"] = json.loads(exercices.read_text()) if exercices.exists() else None
    return detail


def destination_configuree(nom, conf):
    if nom == "garmin":
        return relais.JETONS_GARMIN.is_dir() and any(relais.JETONS_GARMIN.iterdir())
    if nom == "strava":
        return bool(conf.get("STRAVA_CLIENT_ID")) and relais.JETONS_STRAVA.exists()
    return bool(conf.get({"intervals": "INTERVALS_API_KEY", "runalyze": "RUNALYZE_TOKEN",
                          "smashrun": "SMASHRUN_TOKEN"}[nom]))


def configurer_destination(nom, champs):
    """Enregistre les identifiants d'une destination après les avoir vérifiés auprès du service."""
    import destinations

    cles = {"intervals": ["INTERVALS_API_KEY"], "runalyze": ["RUNALYZE_TOKEN"], "smashrun": ["SMASHRUN_TOKEN"],
            "strava": ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"]}.get(nom)
    if not cles:
        raise ValueError(f"rien à configurer pour {nom}")
    valeurs = {c: (champs.get(c) or "").strip() for c in cles}
    if not all(valeurs.values()):
        raise ValueError("tous les champs sont nécessaires")
    if nom == "strava":  # vérifié à l'autorisation (bouton suivant)
        relais.ecrire_config(**valeurs)
        return {"ok": True, "autoriser": "/strava/autoriser"}
    conf = relais.config()
    conf.update(valeurs)
    cible = {"intervals": destinations.Intervals, "runalyze": destinations.Runalyze,
             "smashrun": destinations.Smashrun}[nom](conf)
    qui = cible.verifier()  # lève une erreur si le service refuse
    relais.ecrire_config(**valeurs)
    return {"ok": True, "compte": str(qui)}


def connexion_garmin(email, mot_de_passe):
    from garminconnect import Garmin

    relais.JETONS_GARMIN.mkdir(parents=True, exist_ok=True)
    relais.JETONS_GARMIN.chmod(0o700)
    api = Garmin(email, mot_de_passe, return_on_mfa=True)
    statut, etat_client = api.login(str(relais.JETONS_GARMIN))
    if statut == "needs_mfa":
        CONNEXION_GARMIN.update(api=api, etat=etat_client)
        return {"mfa": True}
    return {"ok": True}


def code_mfa(code):
    if not CONNEXION_GARMIN:
        raise ValueError("aucune connexion en attente : recommencez avec l'e-mail et le mot de passe")
    api = CONNEXION_GARMIN.pop("api")
    api.resume_login(CONNEXION_GARMIN.pop("etat"), code)
    api.client.dump(str(relais.JETONS_GARMIN))
    return {"ok": True}


def arguments(demande):
    """Traduit un bouton de l'interface en arguments de relais.py (rien d'autre n'est accepté)."""
    commande = demande.get("commande")
    if commande in ("montre", "etat", "sante", "auto", "preparer", "satellites", "garmin", "collecte"):
        return [commande]
    if commande == "convertir":
        chemin = (demande.get("chemin") or "").strip()
        if not chemin:
            raise ValueError("indiquez le dossier à convertir")
        return ["convertir", chemin]
    if commande == "envoyer":
        cible = demande.get("destination") or "garmin"
        if cible not in relais.DESTINATIONS:
            raise ValueError(f"destination inconnue : {cible}")
        args = ["envoyer", cible] + ([str(int(demande["nombre"]))] if demande.get("nombre") else [])  # tout par défaut
        return args + (["--reessayer"] if demande.get("reessayer") else [])
    raise ValueError(f"commande inconnue : {commande}")


class Gestionnaire(BaseHTTPRequestHandler):
    def _repondre(self, code, contenu, type_="application/json", cookie=None):
        corps = contenu if isinstance(contenu, bytes) else json.dumps(contenu).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{type_}; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        if cookie is not None:  # chaîne vide : déconnexion
            securise = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" or self._chiffre() else ""
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Max-Age={acces.DUREE if cookie else 0}; Path=/; HttpOnly; SameSite=Lax{securise}")
        self.end_headers()
        self.wfile.write(corps)

    def _rediriger(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _chiffre(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def _hote(self):
        return urllib.parse.urlsplit("//" + self.headers.get("Host", "")).hostname

    def _en_clair_sur_le_reseau(self):
        """Ni chiffré ni sur cet ordinateur : Tailscale chiffre de son côté et annonce X-Forwarded-Proto."""
        return HTTPS and not self._chiffre() and self._hote() not in ("localhost", "127.0.0.1") and not self.headers.get("X-Forwarded-Proto")

    def _connecte(self):
        if not acces.actif():
            return True
        try:
            morceau = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except CookieError:
            return False
        return bool(morceau) and acces.valide(morceau.value)

    def do_GET(self):
        chemin = self.path.partition("?")[0]
        if chemin != CERTIFICAT and self._en_clair_sur_le_reseau():  # le certificat reste accessible avant d'être installé
            return self._rediriger(f"https://{self._hote()}:{PORT_HTTPS}{self.path}")
        if chemin not in OUVERTS and not self._connecte():
            return self._rediriger("/connexion") if chemin == "/" else self._repondre(401, {"erreur": "connexion nécessaire"})
        if chemin == CERTIFICAT and HTTPS:
            self._repondre(200, (acces.CERTIFICATS / "autorite.crt").read_bytes(), "application/octet-stream")
        elif chemin == "/connexion":
            self._repondre(200, CONNEXION.read_bytes(), "text/html")
        elif self.path == "/" or self.path.startswith("/?"):
            self._repondre(200, PAGE.read_bytes(), "text/html")
        elif self.path == "/api/etat":
            self._repondre(200, etat())
        elif self.path == "/api/sante":
            self._repondre(200, sante())
        elif self.path == "/manifest.webmanifest":
            self._repondre(200, (APPLI / "manifest.webmanifest").read_bytes(), "application/manifest+json")
        elif self.path in ("/icone-192.png", "/icone-512.png"):
            self._repondre(200, (APPLI / self.path[1:]).read_bytes(), "image/png")
        elif self.path in ("/leaflet.js", "/leaflet.css"):
            self._repondre(200, (LEAFLET / self.path[1:]).read_bytes(), "text/css" if self.path.endswith("css") else "text/javascript")
        elif self.path.startswith("/api/fit?"):
            chemin = fit_bibliotheque(self.path.partition("fichier=")[2])
            self._repondre(200, chemin.read_bytes(), "application/octet-stream") if chemin else self._repondre(404, {"erreur": "introuvable"})
        elif self.path == "/api/performance":
            self._repondre(200, performance())
        elif self.path == "/api/details-sports":
            self._repondre(200, details_sports())
        elif self.path == "/api/carnet":
            self._repondre(200, carnet.lire(relais.SANTE))
        elif self.path == "/api/compte":
            self._repondre(200, compte_garmin())
        elif self.path.startswith("/api/medias?"):
            self._repondre(200, medias(urllib.parse.unquote(self.path.partition("debut=")[2])))
        elif self.path.startswith("/media?"):
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                fichier = carnet.dossier_medias(relais.SANTE, q["debut"][0]) / carnet.nom_sur(q["nom"][0], carnet.TYPES_MEDIA)
                self._repondre(200, fichier.read_bytes(), "image/" + fichier.suffix[1:].replace("jpg", "jpeg").lower())
            except (KeyError, ValueError, OSError):
                self._repondre(404, {"erreur": "introuvable"})
        elif self.path == "/api/activites":
            self._repondre(200, activites())
        elif self.path.startswith("/api/activite?"):
            self._repondre(200, activite(self.path.partition("fichier=")[2]))
        elif self.path.startswith("/api/journee"):
            self._repondre(200, journee(self.path.partition("date=")[2]))
        elif self.path == "/strava/autoriser":
            import destinations

            client = relais.config().get("STRAVA_CLIENT_ID")
            if not client:
                return self._repondre(400, {"erreur": "Client ID Strava absent"})
            self._rediriger(destinations.Strava.lien_autorisation(client, f"{PUBLIC}/strava/retour"))
        elif self.path.startswith("/strava/retour"):
            import destinations

            requete = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            conf = relais.config()
            try:
                if "code" not in requete:
                    raise ValueError(requete.get("error", ["autorisation refusée"])[0])
                destinations.Strava.echanger_code(conf["STRAVA_CLIENT_ID"], conf["STRAVA_CLIENT_SECRET"],
                                                  requete["code"][0], relais.JETONS_STRAVA)
                self._rediriger("/?strava=ok")
            except Exception as e:
                self._rediriger("/?strava=" + urllib.parse.quote(str(e)[:200]))
        elif self.path == "/smashrun/autoriser":
            import destinations

            self._rediriger(destinations.Smashrun.lien_autorisation(f"{PUBLIC}/smashrun/retour"))
        elif self.path.startswith("/smashrun/retour"):
            self._repondre(200, RETOUR_SMASHRUN.encode(), "text/html")
        elif self.path.startswith("/api/journal"):
            depuis = int(self.path.partition("depuis=")[2] or 0)
            self._repondre(200, {"lignes": TACHE.lignes[depuis:], "total": len(TACHE.lignes),
                                 "en_cours": TACHE.en_cours(), "commande": TACHE.commande, "code": TACHE.code})
        else:
            self._repondre(404, {"erreur": "introuvable"})

    def do_POST(self):
        # une page d'un autre site ouverte dans le même navigateur ne doit pas pouvoir lancer de commande
        origine = self.headers.get("Origin")
        # derrière Tailscale, l'adresse d'origine peut arriver dans X-Forwarded-Host, qu'une page tierce ne peut pas poser sans autorisation
        if origine and urllib.parse.urlsplit(origine).netloc not in (self.headers.get("Host"), self.headers.get("X-Forwarded-Host")):
            return self._repondre(403, {"erreur": "origine refusée"})
        if self._en_clair_sur_le_reseau():
            return self._repondre(403, {"erreur": "utilisez l'adresse en https"})
        if self.path != "/api/connexion" and not self._connecte():
            return self._repondre(401, {"erreur": "connexion nécessaire"})
        try:
            taille = int(self.headers.get("Content-Length", 0))
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            if self.path.startswith("/api/envoi-activite?") or self.path.startswith("/api/envoi-media?"):  # fichier brut, dans le corps
                if taille > TAILLE_ENVOI:
                    return self._repondre(400, {"erreur": "fichier trop gros (50 Mo au plus)"})
                donnees = self.rfile.read(taille)
                if self.path.startswith("/api/envoi-activite?"):
                    return self._repondre(200, recevoir(relais.chemin("sources-brutes") / "imports", q["nom"][0], carnet.TYPES_IMPORT, donnees))
                return self._repondre(200, recevoir(carnet.dossier_medias(relais.SANTE, q["debut"][0]), q["nom"][0], carnet.TYPES_MEDIA, donnees))
            demande = json.loads(self.rfile.read(taille) or "{}")
            if self.path == "/api/connexion":
                if acces.essayer(demande.get("mot_de_passe") or ""):
                    self._repondre(200, {"ok": True}, cookie=acces.jeton())
                else:
                    self._repondre(401, {"erreur": "mot de passe incorrect"})
            elif self.path == "/api/deconnexion":
                self._repondre(200, {"ok": True}, cookie="")
            elif self.path == "/api/mot-de-passe":
                if acces.actif() and not acces.verifier(demande.get("actuel") or ""):
                    raise ValueError("mot de passe actuel incorrect")
                acces.definir(demande.get("nouveau") or "")
                self._repondre(200, {"ok": True}, cookie=acces.jeton())  # les autres appareils devront se reconnecter
            elif self.path == "/api/lancer":
                if not TACHE.lancer(arguments(demande)):
                    return self._repondre(409, {"erreur": "une commande est déjà en cours"})
                self._repondre(200, {"ok": True})
            elif self.path == "/api/arreter":
                TACHE.arreter()
                self._repondre(200, {"ok": True})
            elif self.path == "/api/garmin/connexion":
                self._repondre(200, connexion_garmin(demande["email"], demande["mot_de_passe"]))
            elif self.path == "/api/garmin/mfa":
                self._repondre(200, code_mfa(demande["code"]))
            elif self.path == "/api/carnet":
                self._repondre(200, carnet.modifier(relais.SANTE, demande["section"], demande.get("cle"), demande.get("valeur")))
            elif self.path == "/api/retirer-media":
                (carnet.dossier_medias(relais.SANTE, demande["debut"]) / carnet.nom_sur(demande["nom"], carnet.TYPES_MEDIA)).unlink(missing_ok=True)
                self._repondre(200, {"ok": True})
            elif self.path == "/api/destination":
                self._repondre(200, configurer_destination(demande["nom"], demande.get("champs") or {}))
            else:
                self._repondre(404, {"erreur": "introuvable"})
        except Exception as e:  # message affiché dans l'interface
            self._repondre(400, {"erreur": str(e)})

    def log_message(self, *_):
        pass  # pas de journal d'accès : l'interface interroge le serveur chaque seconde


class ServeurChiffre(ThreadingHTTPServer):
    def handle_error(self, requete, adresse):  # un appareil sans le certificat coupe la connexion : rien d'anormal
        if not isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            super().handle_error(requete, adresse)


def servir_https():
    contexte = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    contexte.load_cert_chain(acces.CERTIFICATS / "relais.crt", acces.CERTIFICATS / "relais.key")
    serveur = ServeurChiffre(("0.0.0.0", PORT_HTTPS), Gestionnaire)
    # la négociation se fait dans le fil de chaque connexion : un appareil lent ne bloque pas les autres
    serveur.socket = contexte.wrap_socket(serveur.socket, server_side=True, do_handshake_on_connect=False)
    serveur.serve_forever()


if __name__ == "__main__":
    print(f"Interface relais.py sur le port {PORT} ; mode automatique : {prochain_auto() or 'désactivé'}")
    threading.Thread(target=planificateur, daemon=True).start()
    threading.Thread(target=synchro_horaire, daemon=True).start()
    if HTTPS:
        threading.Thread(target=servir_https, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Gestionnaire).serve_forever()
