"""Statistiques de performance et conseils d'entraînement (utilisé par relais.py et web.py).

Tout est calculé ici, à partir de activites-v2/ et de sante/ : rien ne sort de l'ordinateur. Résultat : sante/performance.json.

Méthodes publiques, choisies parce qu'elles marchent sans capteur de puissance :
- charge d'une séance : TRIMP d'Edwards, minutes dans chaque zone cardiaque × numéro de la zone (il ne dépend pas du sexe) ;
- condition et fatigue : moyennes exponentielles de la charge sur 42 et 7 jours (Banister), fraîcheur = condition - fatigue ;
- niveau : VDOT de Daniels et Gilbert, d'après le meilleur effort récent, et temps de course de Riegel ;
- VO2 max : Garmin n'en calcule pas pour l'Instinct E-Sports. Il est estimé séance par séance, depuis la vitesse de course
  (équation de l'ACSM : VO2 = 3,5 + 0,2 × m/min) et la part de réserve cardiaque utilisée (%FCR ≈ %VO2R, Swain).

Pour les records et le niveau, seules comptent les courses enregistrées par une montre GPS : les distances des applis
viennent souvent des pas ou de traces importées (3:25 au kilomètre en 2017 sur Smashrun, pour 5:33 au mieux sur 5 km). Une séance d'entraînement n'est pas une course à fond : VDOT et prédictions sont
des minimums, pas un test.
"""
import csv
import json
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median, pstdev

import fitconv

ZONES = (.5, .6, .7, .8, .9)  # début de chaque zone, en part de la FC maximale
COURSES = ("course", "course-tapis")
A_PIED = COURSES + ("course-trail", "marche", "randonnee")
DISTANCES = (("1 km", 1000), ("5 km", 5000), ("10 km", 10000), ("Semi-marathon", 21097.5))
VITESSE_MAX = 10  # m/s entre deux points ; au-delà, c'est un saut de GPS
PAUSE_MAX = 60  # s entre deux points ; au-delà, l'appareil était arrêté
RECENT = 182  # jours pris en compte pour le niveau actuel
MONTRES = ("Garmin", "Zepp", "TomTom")  # seuls ces enregistreurs donnent une distance fiable pour un record
FC_FACILE = .75  # part de la FC maximale sous laquelle une séance reste facile
VOLUME_MARATHON = 30  # km par semaine : en dessous, un temps de marathon prédit ne veut rien dire
COURSE_MIN, COURSE_MAX = 1.8, 6.5  # m/s : en deçà on marche, au-delà c'est un saut de GPS
VO2_DUREE = 900  # s de course au moins, pour une estimation
VO2_RESERVE = .65  # part de réserve cardiaque minimale : plus bas, la FC dit peu de chose de la vitesse
VO2_JOURS, VO2_SEANCES = 90, 10  # l'estimation actuelle : médiane des dernières séances
VO2_ECART = 2  # en deçà, une différence entre deux trimestres n'est que le bruit des séances (±5 d'une sortie à l'autre)


def lire_points(chemin):
    """Les (secondes depuis le début, FC, distance, altitude, cadence) de chaque point d'un FIT, et les calories de la séance."""
    from garmin_fit_sdk import Decoder, Stream

    messages, _ = Decoder(Stream.from_file(str(chemin))).read()
    points = [r for r in messages.get("record_mesgs", []) if r.get("timestamp")]
    return ([((r["timestamp"] - points[0]["timestamp"]).total_seconds(), r.get("heart_rate"), r.get("distance"), r.get("altitude"),
              r.get("cadence")) for r in points], (messages.get("session_mesgs") or [{}])[0].get("total_calories"))


def pic_cardiaque(points):
    """FC la plus haute tenue sur 5 points : un capteur optique a des pics isolés qui fausseraient toutes les zones."""
    fc = [p[1] for p in points if p[1]]
    return max((sorted(fc[i:i + 5])[2] for i in range(len(fc) - 4)), default=None)


def meilleur_temps(points, metres):
    """Temps le plus court sur une distance, en secondes, sans passer par un saut de GPS."""
    mesures = [(p[0], p[2]) for p in points if p[2] is not None]
    meilleur, debut = None, 0
    for fin in range(1, len(mesures)):
        (t1, d1), (t2, d2) = mesures[fin - 1], mesures[fin]
        if d2 - d1 > VITESSE_MAX * max(t2 - t1, 1):
            debut = fin
            continue
        while debut + 1 < fin and d2 - mesures[debut + 1][1] >= metres:
            debut += 1
        if d2 - mesures[debut][1] >= metres:
            # à l'échelle de la distance exacte : le dernier point dépasse d'un peu
            temps = (t2 - mesures[debut][0]) * metres / (d2 - mesures[debut][1])
            meilleur = temps if meilleur is None else min(meilleur, temps)
    return meilleur


def portion_courue(points):
    """(mètres, secondes, FC moyenne) de la partie réellement courue : l'échauffement en marchant fausserait le lien
    entre vitesse et FC."""
    metres = secondes = battements = 0.0
    for (t1, fc, d1, *_), (t2, _, d2, *_) in zip(points, points[1:]):
        if fc and d1 is not None and d2 is not None and 0 < t2 - t1 <= PAUSE_MAX and COURSE_MIN <= (d2 - d1) / (t2 - t1) <= COURSE_MAX:
            metres += d2 - d1
            secondes += t2 - t1
            battements += fc * (t2 - t1)
    return (metres, secondes, battements / secondes) if secondes else None


def vo2_max(portion, fc_max, fc_repos):
    """VO2 max estimé d'une séance, ou None si elle ne permet pas de le dire."""
    if not portion or portion[1] < VO2_DUREE:
        return None
    reserve = (portion[2] - fc_repos) / (fc_max - fc_repos)
    if not VO2_RESERVE <= reserve <= 1:
        return None
    return 3.5 + .2 * (portion[0] / portion[1] * 60) / reserve


def vdot(metres, secondes):
    """VDOT de Daniels et Gilbert pour un effort."""
    minutes = secondes / 60
    vitesse = metres / minutes
    part = .8 + .1894393 * math.exp(-.012778 * minutes) + .2989558 * math.exp(-.1932605 * minutes)
    return (-4.6 + .182258 * vitesse + .000104 * vitesse ** 2) / part


def allure_vdot(niveau, part):
    """Secondes au km à une part de VDOT (inverse de la formule ci-dessus)."""
    cible = part * niveau
    vitesse = (-.182258 + math.sqrt(.182258 ** 2 + 4 * .000104 * (4.6 + cible))) / (2 * .000104)
    return 60000 / vitesse


def temps_riegel(metres, secondes, cible):
    return secondes * (cible / metres) ** 1.06


def csv_jours(fichier, colonne):
    """{jour: valeur} d'une colonne de sante/."""
    if not fichier.exists():
        return {}
    with fichier.open() as f:
        return {date.fromisoformat(l["date"]): float(l[colonne]) for l in csv.DictReader(f) if l.get(colonne)}


def virgule(valeur):
    return f"{valeur:.1f}".replace(".", ",")


def moyenne(valeurs):
    valeurs = list(valeurs)
    return sum(valeurs) / len(valeurs) if valeurs else None


def fenetre(jours, aujourdhui, debut, fin):
    """Valeurs d'un {jour: valeur} entre aujourdhui - debut (inclus) et aujourdhui - fin (exclu)."""
    return [v for j, v in jours.items() if fin < (aujourdhui - j).days <= debut]


def lire_seances(v2):
    """Une entrée par activité de la bibliothèque, avec ce que ses points permettent de mesurer."""
    with (v2 / "index.csv").open() as f:
        index = list(csv.DictReader(f))
    seances = []
    for l in index:
        s = {"fichier": l["fichier"], "jour": date.fromisoformat(l["debut_utc"][:10]), "categorie": l["categorie"],
             "minutes": float(l["duree_min"] or 0), "km": float(l["distance_km"] or 0), "gps": l["gps"] == "True",
             "cardio": l["cardio"] == "True", "montre": l["enregistreur"] in MONTRES and l["original"] == "True",
             "pic": None, "fc": Counter(), "efforts": {}}
        mesure_vitesse = s["gps"] and s["montre"] and s["categorie"] in COURSES
        s.update(mouvement=None, montee=None, altitude=None, cadence=None, calories=None)
        if s["cardio"] or s["gps"]:
            try:
                points, s["calories"] = lire_points(v2 / l["fichier"])
            except (OSError, ValueError, KeyError, IndexError):
                points = []  # fichier en cours de remplacement ou illisible : la séance compte sans ses points
            s["pic"] = pic_cardiaque(points)
            en_mouvement = fitconv.temps_en_mouvement([(p[0], p[2]) for p in points], s["km"] * 1000)
            s["mouvement"] = en_mouvement / 60 if en_mouvement else None
            altitudes = [p[3] for p in points if p[3] is not None]
            s["montee"] = fitconv.denivele(altitudes)[0] if altitudes else None
            s["altitude"] = round(max(altitudes)) if altitudes else None
            cadences = [p[4] for p in points if p[4] and p[4] <= fitconv.CADENCE_MAX]
            if cadences:  # à pied, le FIT compte les foulées d'un seul pied
                s["cadence"] = sum(cadences) / len(cadences) * (2 if s["categorie"] in A_PIED else 1)
            for (t1, fc, *_), (t2, *_) in zip(points, points[1:]):
                if fc and t2 - t1 <= PAUSE_MAX:
                    s["fc"][fc] += t2 - t1  # secondes passées à chaque FC : assez pour les zones, une fois la FC max connue
            if mesure_vitesse:
                s["efforts"] = {nom: t for nom, m in DISTANCES if (t := meilleur_temps(points, m))
                                and m / t <= VITESSE_MAX / 2}  # plus de 18 km/h soutenus : encore une erreur de distance
                s["courue"] = portion_courue(points)
        seances.append(s)
    return seances


def zones(seance, fc_max):
    """Secondes dans chaque zone cardiaque."""
    resultat = [0.0] * len(ZONES)
    for fc, secondes in seance["fc"].items():
        z = bisect_right(ZONES, fc / fc_max) - 1
        if z >= 0:
            resultat[z] += secondes
    return resultat


def calculer(v2, sante, aujourdhui=None):
    aujourdhui = aujourdhui or date.today()
    seances = lire_seances(v2)
    pics = sorted([s["pic"] for s in seances if s["pic"] and (aujourdhui - s["jour"]).days <= 730]
                  or [s["pic"] for s in seances if s["pic"]] or [190])
    fc_max = pics[-2] if len(pics) > 1 else pics[-1]  # la deuxième plus haute : une seule séance faussée décalerait toutes les zones
    fc_repos_jours = csv_jours(sante / "garmin.csv", "fc_repos")
    fc_repos = median(fenetre(fc_repos_jours, aujourdhui, 30, -1) or [60])

    # charge par jour, puis condition et fatigue
    charges = defaultdict(float)
    for s in seances:
        s["zones"] = zones(s, fc_max)
        s["charge"] = sum(secondes / 60 * (i + 1) for i, secondes in enumerate(s["zones"]))
        charges[s["jour"]] += s["charge"]
    premier = min((j for j, c in charges.items() if c), default=aujourdhui)
    condition = fatigue = 0.0
    courbe = []
    for n in range((aujourdhui - premier).days + 1):
        jour = premier + timedelta(days=n)
        condition += (charges.get(jour, 0) - condition) * (1 - math.exp(-1 / 42))
        fatigue += (charges.get(jour, 0) - fatigue) * (1 - math.exp(-1 / 7))
        courbe.append([jour.isoformat(), round(condition, 1), round(fatigue, 1), round(condition - fatigue, 1)])

    # les calculs à la Runalyze : équilibre, régularité et repos nécessaire pour revenir à l'équilibre
    sept = [charges.get(aujourdhui - timedelta(days=n), 0) for n in range(7)]
    ecart = pstdev(sept)
    repos, c, f = 0, condition, fatigue
    while c < f and repos < 30:
        c, f, repos = c * math.exp(-1 / 42), f * math.exp(-1 / 7), repos + 1

    # volume par semaine (lundi)
    semaines = defaultdict(lambda: {"km": 0.0, "minutes": 0.0, "seances": 0})
    for s in seances:
        lundi = s["jour"] - timedelta(days=s["jour"].weekday())
        semaines[lundi]["minutes"] += s["minutes"]
        semaines[lundi]["seances"] += 1
        if s["categorie"] in COURSES:
            semaines[lundi]["km"] += s["km"]

    # records et niveau
    courses = [s for s in seances if s["efforts"]]
    records = []
    for nom, metres in DISTANCES:
        avec = [s for s in courses if nom in s["efforts"]]
        recents = [s for s in avec if (aujourdhui - s["jour"]).days <= RECENT]
        meilleur = min(avec, key=lambda s: s["efforts"][nom], default=None)
        recent = min(recents, key=lambda s: s["efforts"][nom], default=None)
        records.append({"nom": nom, "metres": metres, "temps": meilleur and round(meilleur["efforts"][nom]),
                        "date": meilleur and meilleur["jour"].isoformat(), "fichier": meilleur and meilleur["fichier"],
                        "temps_recent": recent and round(recent["efforts"][nom]), "date_recent": recent and recent["jour"].isoformat(),
                        "fichier_recent": recent and recent["fichier"]})
    volume_recent = sum(s["km"] for s in seances if s["categorie"] in COURSES and (aujourdhui - s["jour"]).days < 28) / 4
    # le 5 km et le 10 km décrivent mieux l'endurance qu'un kilomètre isolé
    reference = max(((vdot(r["metres"], r["temps_recent"]), r) for r in records if r["temps_recent"] and r["metres"] >= 5000),
                    key=lambda x: x[0], default=None)
    niveau = None
    if reference:
        valeur, r = reference
        niveau = {"vdot": round(valeur, 1), "reference": r["nom"], "date": r["date_recent"],
                  "predictions": [{"nom": nom, "temps": round(temps_riegel(r["metres"], r["temps_recent"], m))}
                                  for nom, m in DISTANCES[1:] + (("Marathon", 42195),)
                                  if m < 42195 or volume_recent >= VOLUME_MARATHON],
                  "allures": [{"nom": "Endurance", "but": "la majorité de vos kilomètres", "de": round(allure_vdot(valeur, .74)), "a": round(allure_vdot(valeur, .65))},
                              {"nom": "Seuil", "but": "20 à 30 min en continu ou par blocs", "de": round(allure_vdot(valeur, .88)), "a": round(allure_vdot(valeur, .83))},
                              {"nom": "Fractionné", "but": "répétitions de 3 à 5 min", "de": round(allure_vdot(valeur, 1)), "a": round(allure_vdot(valeur, .95))}]}

    # économie de course : mètres parcourus par battement cardiaque sur la partie courue, mois par mois
    efficacite = defaultdict(list)
    for s in seances:
        courue = s.get("courue")
        if courue and courue[1] >= VO2_DUREE:
            s["metres_battement"] = courue[0] / (courue[2] * courue[1] / 60)
            efficacite[s["jour"].replace(day=1)].append(s["metres_battement"])

    # VO2 max estimé : médiane des dernières séances (une seule varie de plusieurs points : chaleur, relief, fatigue)
    vo2_seances = sorted((s["jour"], v) for s in seances if (v := vo2_max(s.get("courue"), fc_max, fc_repos)))
    vo2_mois = defaultdict(list)
    for jour, v in vo2_seances:
        vo2_mois[jour.replace(day=1)].append(v)
    vo2_recents = [v for j, v in vo2_seances if (aujourdhui - j).days <= VO2_JOURS][-VO2_SEANCES:]
    vo2_avant = [v for j, v in vo2_seances if VO2_JOURS < (aujourdhui - j).days <= 2 * VO2_JOURS]
    vo2 = {"valeur": round(median(vo2_recents), 1), "seances": len(vo2_recents), "avant": round(median(vo2_avant), 1) if len(vo2_avant) >= 3 else None,
           "bas": round(min(vo2_recents), 1), "haut": round(max(vo2_recents), 1)} if len(vo2_recents) >= 3 else None

    resultat = {"date": aujourdhui.isoformat(), "fc_max": fc_max, "fc_repos": round(fc_repos), "vo2": vo2,
                "calculs": {"ratio": round(fatigue / condition, 2) if condition else None, "repos": repos,
                            "monotonie": round(moyenne(sept) / ecart, 2) if ecart else None, "charge_7": round(sum(sept))},
                "seances": [{"fichier": s["fichier"], "jour": s["jour"].isoformat(), "categorie": s["categorie"], "minutes": round(s["minutes"], 1),
                             "mouvement": s["mouvement"] and round(s["mouvement"], 1), "km": round(s["km"], 3), "gps": s["gps"], "montre": s["montre"],
                             "fc": round(sum(f * v for f, v in s["fc"].items()) / sum(s["fc"].values())) if sum(s["fc"].values()) else None,
                             "charge": round(s["charge"]), "zones": [round(z / 60, 1) for z in s["zones"]], "montee": s["montee"],
                             "cadence": s["cadence"] and round(s["cadence"]), "calories": s["calories"], "altitude": s["altitude"]} for s in seances],
                "vo2_mois": [[j.isoformat(), round(median(v), 1)] for j, v in sorted(vo2_mois.items())],
                "courbe": courbe[-365:], "semaines": [[j.isoformat(), round(v["km"], 1), round(v["minutes"]), v["seances"]] for j, v in sorted(semaines.items()) if (aujourdhui - j).days <= 371],
                "efficacite": [[j.isoformat(), round(moyenne(v), 3)] for j, v in sorted(efficacite.items())], "records": records, "niveau": niveau}
    resultat.update(bilan(seances, resultat, sante, aujourdhui, fc_repos_jours))
    return resultat


def bilan(seances, resultat, sante, aujourdhui, fc_repos_jours):
    """Les chiffres des 4 dernières semaines, et les conseils qu'ils appellent. Chaque conseil cite ses mesures : il doit
    pouvoir être vérifié, et il ne s'affiche que si la mesure existe."""
    depuis = lambda s, jours, avant=0: avant <= (aujourdhui - s["jour"]).days < jours
    s28 = [s for s in seances if depuis(s, 28)]
    courses = [s for s in s28 if s["categorie"] in COURSES]
    courses_avant = [s for s in seances if s["categorie"] in COURSES and depuis(s, 56, 28)]
    km, km_avant = sum(s["km"] for s in courses), sum(s["km"] for s in courses_avant)
    muscu = sum(s["categorie"] == "musculation" for s in s28)
    secondes = [sum(s["zones"][i] for s in courses) for i in range(len(ZONES))]
    total = sum(secondes)
    parts = [round(100 * v / total) for v in secondes] if total else None
    cardio = sum(s["cardio"] for s in s28)
    longue = max(courses, key=lambda s: s["minutes"], default=None)
    derniere = max((s["jour"] for s in seances if s["categorie"] in COURSES), default=None)
    charge_7 = sum(s["charge"] for s in seances if depuis(s, 7))
    charge_3s = sum(s["charge"] for s in seances if depuis(s, 28, 7)) / 3
    actives = {s["jour"] - timedelta(days=s["jour"].weekday()) for s in seances if depuis(s, 84)}
    jour = resultat["courbe"][-1] if resultat["courbe"] else None
    condition, fatigue, fraicheur = jour[1:] if jour else (None, None, None)
    sommeil = csv_jours(sante / "sommeil.csv", "total_h")
    batterie = csv_jours(sante / "garmin.csv", "batterie_max")
    rhr_7, rhr_60 = moyenne(fenetre(fc_repos_jours, aujourdhui, 7, -1)), moyenne(fenetre(fc_repos_jours, aujourdhui, 67, 7))
    sommeil_14 = moyenne(fenetre(sommeil, aujourdhui, 14, -1))
    batterie_14 = moyenne(fenetre(batterie, aujourdhui, 14, -1))
    tendance = [s["metres_battement"] for s in seances if "metres_battement" in s and depuis(s, 90)]
    tendance_avant = [s["metres_battement"] for s in seances if "metres_battement" in s and depuis(s, 180, 90)]
    fc_facile = round(FC_FACILE * resultat["fc_max"])
    conseils = []

    def conseil(niveau, titre, detail):
        conseils.append({"niveau": niveau, "titre": titre, "detail": detail})

    if parts and sum(parts[3:]) >= 35 and len(courses) >= 2:
        conseil("action", "Courez plus lentement la plupart du temps",
                f"{sum(parts[3:])} % de votre temps de course des 4 dernières semaines est à haute intensité, et seulement {100 - sum(parts[3:])} % en dessous. "
                f"Les coureurs qui progressent font environ 80 % de leur volume en facile : c'est lui qui développe l'endurance et permet de récupérer des séances dures. "
                f"Essayez de rester sous {fc_facile} battements par minute sur deux sorties sur trois, quitte à marcher dans les côtes : c'est le pouls qui compte, pas l'allure.")
    if fraicheur is not None and condition and fraicheur < -.3 * condition and fraicheur < -20:
        conseil("action", "Levez le pied quelques jours",
                f"Votre effort récent ({fatigue:.0f}) dépasse nettement votre effort habituel ({condition:.0f}). Prévoyez 2 ou 3 jours légers avant la prochaine séance dure.")
    elif charge_3s >= 30 and charge_7 > 1.5 * charge_3s:
        conseil("attention", "Votre effort monte vite",
                f"Effort des 7 derniers jours : {charge_7:.0f} points, contre {charge_3s:.0f} par semaine en moyenne sur les 3 précédentes. Gardez une hausse progressive pour limiter le risque de blessure.")
    elif fraicheur is not None and condition and fraicheur > .25 * condition and fraicheur > 5:
        conseil("bien", "Vous êtes reposé", "C'est un bon moment pour une séance plus dure, ou pour tester votre niveau sur 5 km.")
    if len(courses) < 8:
        conseil("action" if len(courses) < 4 else "attention", "Courez plus régulièrement",
                f"{len(courses)} course{'s' if len(courses) > 1 else ''} en 4 semaines. La régularité compte plus que chaque séance : visez 3 sorties par semaine, même courtes.")
    elif km_avant and km > 1.3 * km_avant:
        conseil("attention", "Volume en forte hausse", f"{km:.0f} km sur 4 semaines contre {km_avant:.0f} km sur les 4 précédentes (+{100 * (km / km_avant - 1):.0f} %). Stabilisez une semaine avant d'augmenter encore.")
    if longue and len(courses) >= 4 and longue["minutes"] < 60:
        conseil("attention", "Ajoutez une sortie longue", f"Votre sortie la plus longue des 4 dernières semaines dure {longue['minutes']:.0f} min. Allongez-en une de 10 min par semaine, en facile, jusqu'à 60 à 75 min.")
    if muscu < 4:
        conseil("attention" if muscu else "action", "Renforcez-vous", f"{muscu} séance{'s' if muscu > 1 else ''} de musculation en 4 semaines. Deux séances courtes par semaine (jambes, tronc) soutiennent la course et limitent les blessures.")
    else:
        conseil("bien", "Musculation régulière", f"{muscu} séances en 4 semaines : continuez.")
    if rhr_7 and rhr_60 and rhr_7 - rhr_60 >= 5:
        conseil("attention", "Pouls au repos en hausse", f"{rhr_7:.0f} bpm sur 7 jours contre {rhr_60:.0f} habituellement. Cela signale souvent de la fatigue, du stress ou un début de maladie : allégez. Si cela dure ou s'accompagne de symptômes, parlez-en à un médecin.")
    if sommeil_14 and sommeil_14 < 7:
        conseil("attention", "Dormez davantage", f"{virgule(sommeil_14)} h par nuit sur 14 jours. Viser 7 à 9 h est l'un des meilleurs leviers de récupération.")
    elif sommeil_14:
        conseil("bien", "Bon sommeil", f"{virgule(sommeil_14)} h par nuit sur 14 jours.")
    if batterie_14 and batterie_14 < 50:
        conseil("attention", "Récupération incomplète", f"Votre énergie plafonne en moyenne à {batterie_14:.0f} sur 100 depuis 14 jours : vous ne repartez pas chargé. Regardez d'abord le sommeil et le stress avant d'ajouter de la charge.")
    if len(tendance) >= 4 and len(tendance_avant) >= 4:
        evolution = 100 * (moyenne(tendance) / moyenne(tendance_avant) - 1)
        if evolution >= 2:
            conseil("bien", "Votre efficacité de course progresse", f"+{evolution:.0f} % de distance par battement cardiaque sur 3 mois : à effort égal, vous allez plus loin.")
        elif evolution <= -2:
            conseil("attention", "Votre efficacité de course recule", f"{evolution:.0f} % de distance par battement cardiaque sur 3 mois. La fatigue, la chaleur ou un manque de volume facile peuvent l'expliquer.")
    vo2 = resultat["vo2"]
    if vo2 and vo2["avant"]:
        if vo2["valeur"] - vo2["avant"] >= VO2_ECART:
            conseil("bien", "Votre capacité cardio progresse", f"{virgule(vo2['valeur'])} contre {virgule(vo2['avant'])} les 3 mois précédents.")
        elif vo2["avant"] - vo2["valeur"] >= VO2_ECART:
            conseil("attention", "Votre capacité cardio recule", f"{virgule(vo2['valeur'])} contre {virgule(vo2['avant'])} les 3 mois précédents. La fatigue ou la chaleur peuvent jouer : regardez la tendance sur plusieurs semaines.")
    conseils.sort(key=lambda c: ("action", "attention", "bien").index(c["niveau"]))
    return {"forme": {"condition": condition, "fatigue": fatigue, "fraicheur": fraicheur},
            "periode": {"seances": len(s28), "courses": len(courses), "km": round(km, 1), "km_avant": round(km_avant, 1), "minutes": round(sum(s["minutes"] for s in s28)),
                        "musculation": muscu, "semaines_actives": len(actives), "derniere_course": derniere and derniere.isoformat(),
                        "avec_cardio": cardio, "charge_7": round(charge_7), "sortie_longue": longue and round(longue["minutes"])},
            "zones": parts, "conseils": conseils}


def ecrire(v2, sante):
    resultat = calculer(v2, sante)
    (sante / "performance.json").write_text(json.dumps(resultat, ensure_ascii=False))
    print(f"Performance : {len(resultat['conseils'])} conseil(s), dans {sante / 'performance.json'}")
    return resultat
