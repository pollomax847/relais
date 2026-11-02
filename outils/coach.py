"""Le mot du coach : ce que Relais conclut de vos dernières sorties, de votre sommeil et de vos pas (utilisé par relais.py et web.py).

Les chiffres et les conseils sont faits ici, avec des règles : ils se vérifient et ne dépendent d'aucun modèle. Le petit modèle
de langue du conteneur relais-ia ne fait que les rédiger, et rien ne sort de l'ordinateur. Une note qui cite un chiffre absent
des règles est écartée, et les règles s'affichent seules : mieux vaut un texte sec qu'un chiffre inventé.
Résultat : sante/coach.json.
"""
import json
import os
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import carnet
import performance
import programme
from performance import COURSE_MIN, COURSES, PAUSE_MAX, csv_jours, date_fr, fenetre, lire_points, virgule

ADRESSE_IA = os.environ.get("RELAIS_IA", "http://relais-ia:11434")
MODELE = os.environ.get("RELAIS_IA_MODELE", "gemma4:e4b")  # essayé contre qwen3.5:4b et mistral:7b : le seul à ne déformer aucune phrase
ATTENTE_IA = 600  # secondes : sur le processeur seul, une note prend environ une minute
ESSAIS_IA = 2  # un chiffre mal recopié arrive : un second essai suffit souvent
COMPARAISON = 8  # sorties regardées pour dire si la dernière sort du lot
DEPART_TROP_VITE = 45  # secondes par km entre le premier et le dernier kilomètre
PAS_BAS = 5000  # par jour : sous ce seuil, le gain pour la santé est le plus net (Ding 2025)
PAS_CREUX = 4000  # par jour
RENFORCEMENT_OUBLIE = 14  # jours
BAISSE_POIDS = 2  # kg en 6 mois, à partir desquels la phrase s'affiche ; le NHS ne donne pas de seuil
MOTS = (40, 130)  # longueur acceptée de la note rédigée
EN_LETTRES = re.compile(r"\b(trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze|treize|quatorze|quinze|seize|vingt|trente|quarante|cinquante|soixante|cent|mille)\w*", re.I)  # le filet ne lit que les chiffres
INTERDIT = re.compile(r"\b(médicament|complément|supplément|magnésium|vitamine|créatine|diagnostic|maladie)\w*", re.I)
CONSIGNE = """Tu es un coach de course à pied, direct et chaleureux. Tu écris en français simple à un coureur de loisir, en le vouvoyant.
Récris les phrases qui suivent en un seul petit paragraphe de 60 à 90 mots.
Règles strictes :
- garde le sens exact de chaque phrase ;
- écris tous les nombres en chiffres, exactement comme dans les phrases, par exemple 6 min 45 ou 10,5 km ;
- n'ajoute aucun nombre, aucun fait, aucun conseil ;
- pas de titre, pas de liste, pas de bonjour, ne parle pas de toi.

Exemple de phrases :
Vous n'avez marché que 2 minutes : votre sortie la plus continue.
Prochaine sortie, samedi : restez sous 9,5 km.
Repas du jour : des légumes d'abord.

Exemple de réponse :
Belle sortie : seulement 2 minutes de marche, la plus continue de vos dernières courses. Samedi, gardez cet élan et restez sous 9,5 km. Aujourd'hui, à table, mettez d'abord des légumes."""


def allure_fr(secondes):
    """« 6 min 45 » plutôt que « 6:45 », que seuls les coureurs lisent."""
    secondes = round(secondes)
    return f"{secondes // 60} min {secondes % 60:02d}"


def kmh_fr(secondes):
    """La même allure en km/h, plus parlante pour un débutant."""
    return virgule(3600 / secondes)


def energie_au_depart(garmin, seance):
    """La note d'énergie de la montre juste avant le départ, si le détail de cette journée est copié."""
    fichier = garmin / "minute" / f"garmin-connect-minute-{seance['jour']}.json"
    if not fichier.exists():
        return None
    debut = datetime.strptime(Path(seance["fichier"]).name[:17], "%Y-%m-%d_%H%M%S").replace(tzinfo=timezone.utc)  # le nom du fichier porte l'heure de départ UTC
    depart = debut.timestamp() * 1000
    avant = [v for t, v in json.loads(fichier.read_text()).get("batterie") or [] if t <= depart and v is not None]
    return avant[-1] if avant else None


def lire_sortie(seance, v2, garmin):
    """Ce qu'un coach regarde dans une sortie : la marche, le rythme kilomètre par kilomètre et le pouls au milieu de l'effort."""
    points, _ = lire_points(v2 / seance["fichier"])
    marche = course = 0.0
    kms, debut_km, arrets, km = [], points[0][0] if points else 0, 0.0, 1
    for (t1, _, d1, *_), (t2, _, d2, *_) in zip(points, points[1:]):
        duree = t2 - t1
        if d1 is None or d2 is None or duree <= 0:
            continue
        vitesse = (d2 - d1) / duree
        if duree > PAUSE_MAX or vitesse < .3:
            arrets += duree
        elif vitesse >= COURSE_MIN:
            course += duree
        else:
            marche += duree
        if d2 >= 1000 * km > d1:  # le temps de chaque kilomètre, tel que la montre l'affiche, et ses arrêts
            passage = t1 + (1000 * km - d1) / (d2 - d1) * duree  # entre deux points, à la seconde près
            kms.append((passage - debut_km, arrets))
            debut_km, arrets, km = passage, 0.0, km + 1
    pouls = [p[1] for p in points[len(points) // 3:2 * len(points) // 3] if p[1]]
    courue = performance.portion_courue(points)
    return {"jour": date.fromisoformat(seance["jour"]), "km": seance["km"], "marche": marche / 60, "part_marche": marche / (marche + course) if marche + course else 0,
            "allure": courue[1] / courue[0] * 1000 if courue else None, "kms": kms, "pouls": sum(pouls) / len(pouls) if pouls else None,
            "energie": energie_au_depart(garmin, seance)}


def recentes(resultat, v2, garmin, aujourdhui):
    """Les dernières sorties, de la plus ancienne à la plus récente."""
    courses = sorted((s for s in resultat["seances"] if s["categorie"] in COURSES and s["gps"] and s["montre"] and s["km"]), key=lambda s: s["jour"])
    sorties = []
    for s in courses[-COMPARAISON:]:
        try:
            sorties.append(lire_sortie(s, v2, garmin))
        except (OSError, ValueError, KeyError, IndexError):
            pass  # fichier en cours de remplacement : elle compte à la prochaine note
    return sorties


def prochain_jour(jours, aujourdhui):
    """Le prochain jour de sortie possible, aujourd'hui compris."""
    return next(aujourdhui + timedelta(days=n) for n in range(7) if (aujourdhui + timedelta(days=n)).weekday() in (jours or (6,)))


def jour_fr(jour, aujourdhui):
    ecart = (jour - aujourdhui).days
    return "aujourd'hui" if not ecart else "demain" if ecart == 1 else programme.JOURS[jour.weekday()]


def parler_sorties(sorties, aujourdhui):
    if not sorties:
        return []
    derniere = sorties[-1]
    points = [f"Dernière sortie, le {date_fr(derniere['jour'], aujourdhui)} : {virgule(derniere['km'])} km" + (f", à {allure_fr(derniere['allure'])} par kilomètre en courant, soit {kmh_fr(derniere['allure'])} km/h." if derniere["allure"] else ".")]
    autres = sorties[:-1]
    if len(autres) >= 3 and derniere["part_marche"] < min(s["part_marche"] for s in autres):
        points.append(f"Vous n'avez marché que {round(derniere['marche'])} minutes : c'est votre sortie la plus continue parmi les {len(sorties)} dernières, et le vrai progrès à retenir.")
    (premier, arret_debut), (dernier, arret_fin) = (derniere["kms"][0], derniere["kms"][-1]) if len(derniere["kms"]) >= 3 else ((0, 1e9), (0, 1e9))
    if arret_debut + arret_fin < PAUSE_MAX and dernier - premier >= DEPART_TROP_VITE:  # un arrêt fausserait la comparaison
        points.append(f"Premier kilomètre en {allure_fr(premier)}, dernier en {allure_fr(dernier)} : le départ était trop rapide, vous avez fini plus lentement.")
    if len(sorties) >= 2:
        avant = sorties[-2]
        if avant["allure"] and derniere["allure"] and avant["pouls"] and derniere["pouls"] and derniere["energie"] is not None and avant["energie"] is not None \
                and abs(avant["allure"] - derniere["allure"]) <= 15 and avant["pouls"] - derniere["pouls"] >= 5 and derniere["energie"] > avant["energie"]:
            points.append(f"À la même allure qu'à la sortie d'avant, votre pouls a baissé de {avant['pouls'] - derniere['pouls']:.0f} battements, avec une énergie de {derniere['energie']} au départ contre {avant['energie']} : "
                          "c'est sans doute surtout de la fraîcheur.")
    return points


def parler_performance(sorties, resultat, profil, aujourdhui):
    if programme.prudence(profil):
        return ["Vous avez signalé une blessure ou un problème de santé : demandez l'avis d'un médecin ou d'un kiné avant d'en faire plus."]
    prochain = prochain_jour(profil.get("jours"), aujourdhui)
    points = []
    plus_longue = max((s["km"] for s in resultat["seances"] if s["categorie"] in COURSES and (aujourdhui - date.fromisoformat(s["jour"])).days < 30), default=0)
    limite = f" Restez sous {virgule(programme.HAUSSE_SORTIE * plus_longue)} km." if plus_longue else ""
    dures = [s for s in sorties[-2:] if s["pouls"] and s["pouls"] >= performance.FC_DUR * resultat["fc_max"]]
    allures = (resultat.get("niveau") or {}).get("allures") or []
    if len(dures) == 2 and allures:
        points.append(f"Vos deux dernières sorties étaient dures presque de bout en bout. Prochaine sortie, {jour_fr(prochain, aujourdhui)} : courez assez lentement pour parler, entre {allure_fr(allures[0]['de'])} et {allure_fr(allures[0]['a'])} par kilomètre, soit {kmh_fr(allures[0]['a'])} à {kmh_fr(allures[0]['de'])} km/h. "
                      f"Si votre montre affiche le temps de chaque kilomètre, ralentissez quand il passe sous {allure_fr(allures[0]['de'])}.{limite}")
    else:
        depart = f", en partant vers {allure_fr(10 * -(-sorties[-1]['allure'] // 10))} par kilomètre" if sorties and sorties[-1]["allure"] else ""
        points.append(f"Prochaine sortie, {jour_fr(prochain, aujourdhui)} : gardez votre rythme habituel{depart}.{limite}")
    derniere = max((date.fromisoformat(s["jour"]) for s in resultat["seances"] if s["categorie"] == "musculation"), default=None)
    if not derniere:
        points.append("Le renforcement est votre priorité : 20 à 35 minutes avant de courir, avec les exercices de l'écran Conseils.")
    elif (aujourdhui - derniere).days > RENFORCEMENT_OUBLIE:
        points.append(f"Le renforcement est votre priorité : aucune séance depuis {(aujourdhui - derniere).days // 7} semaines. Faites-en 20 à 35 minutes avant de courir, avec les exercices de l'écran Conseils.")
    return points


def parler_semaine(resultat, profil, sante, aujourdhui):
    """Les jours sans course, tirés de la semaine type de l'écran Conseils, et l'activité de tous les sports."""
    points, jours = [], {}
    for jour, texte in programme.semaine_de(resultat, profil, sante, aujourdhui).get("autres", []):
        jours.setdefault(texte, []).append(programme.JOURS[jour])
    if jours:
        points.append("En plus de la course : " + " ; ".join(f"{' et '.join(j)}, {t[0].lower()}{t[1:]}" for t, j in jours.items()) + ".")
    minutes = programme.minutes_actives(sante, aujourdhui)
    if minutes:
        points.append(f"Activité, tous sports compris : {minutes:.0f} minutes par semaine selon la montre, pour un repère d'au moins {programme.ENDURANCE}"
                      + (" : c'est atteint." if minutes >= programme.ENDURANCE else "."))
    return points


def parler_recuperation(resultat, sante, aujourdhui):
    points = []
    derniere = max((date.fromisoformat(s["jour"]) for s in resultat["seances"] if s["categorie"] in COURSES), default=None)
    if derniere and (aujourdhui - derniere).days <= 1:
        points.append("Votre sortie est toute récente : marche ou repos aujourd'hui.")
    repos = csv_jours(sante / "garmin.csv", "fc_repos")
    derniers, habitude = fenetre(repos, aujourdhui, 3, -1), fenetre(repos, aujourdhui, 63, 3)
    if derniers and habitude:
        ecart = sum(derniers) / len(derniers) - median(habitude)
        points.append(f"Pouls au repos à {sum(derniers) / len(derniers):.0f}, contre {median(habitude):.0f} d'habitude : " + ("levez le pied quelques jours." if ecart >= 5 else "rien d'inquiétant."))
    return points


def parler_sommeil(sante, aujourdhui):
    nuits = csv_jours(sante / "sommeil.csv", "total_h")
    recentes_nuits = fenetre(nuits, aujourdhui, 14, -1)
    if not recentes_nuits:
        return []
    moyenne = sum(recentes_nuits) / len(recentes_nuits)
    if moyenne >= programme.SOMMEIL:
        return [f"Sommeil : {virgule(moyenne)} h par nuit sur 14 jours, rien à changer."]
    return [f"Sommeil : {virgule(moyenne)} h par nuit sur 14 jours, sous le repère de {programme.SOMMEIL} h. C'est le premier point à améliorer."]


def parler_pas(sante, aujourdhui):
    pas = csv_jours(sante / "pas.csv", "pas")
    if not pas:
        return []
    complets = {j: v for j, v in pas.items() if j < max(pas) and (aujourdhui - j).days <= 15}  # le dernier jour s'arrête à la dernière synchronisation
    semaine = [v for j, v in complets.items() if j.weekday() < 5]
    if not semaine:
        return []
    creux = sum(v < PAS_CREUX for v in semaine)
    texte = f"Pas : {programme.pas_fr(median(semaine))} en semaine"
    fin = [v for j, v in complets.items() if j.weekday() >= 5]
    if fin:
        texte += f" et {programme.pas_fr(median(fin))} le week-end"
    texte += f", avec {creux} jours sous {programme.pas_fr(PAS_CREUX)} pas." if creux > 1 else "."
    if median(semaine) < programme.PAS or creux:
        texte += f" Les jours sous {programme.pas_fr(PAS_BAS)} pas, ajoutez une marche de 20 minutes."
    return [texte]


def parler_repas(resultat, profil, sante, aujourdhui):
    prochain = prochain_jour(profil.get("jours"), aujourdhui)
    derniere = max((date.fromisoformat(s["jour"]) for s in resultat["seances"] if s["categorie"] in COURSES), default=None)
    if (prochain - aujourdhui).days == 0:
        points = ["Repas du jour : une collation 1 à 2 h avant de courir (fruit, pain ou céréales), puis un repas avec des féculents et des protéines."]
    elif (prochain - aujourdhui).days == 1:
        points = ["Ce soir, veille de sortie : un repas normal avec des féculents, rien de particulier à ajouter."]
    elif derniere and (aujourdhui - derniere).days <= 1:
        points = ["Repas d'après sortie : des féculents et des protéines, par exemple un œuf sur du pain, ou un yaourt et un fruit."]
    else:
        points = ["Repas du jour : des légumes d'abord, et des légumineuses comme les lentilles ou les pois chiches. Pas besoin de manger plus un jour sans sortie."]
    pesees = csv_jours(sante / "poids.csv", "kg")
    if profil.get("poids_kg"):
        pesees[date.fromisoformat(profil["poids_date"])] = float(profil["poids_kg"])
    if pesees:
        dernier = max(pesees)
        anciennes = [v for j, v in pesees.items() if 120 <= (dernier - j).days <= 240]
        if anciennes and max(anciennes) - pesees[dernier] >= BAISSE_POIDS:
            points.append(f"Poids : {programme.kg_fr(pesees[dernier])} kg, contre {programme.kg_fr(max(anciennes))} kg il y a quelques mois. Si vous n'avez pas cherché à maigrir et qu'il continue de baisser, parlez-en à votre médecin.")
    return points


def regles(resultat, profil, v2, sante, garmin, aujourdhui):
    """Les constats et les conseils du jour, par rubrique."""
    sorties = recentes(resultat, v2, garmin, aujourdhui)
    return [{"titre": titre, "points": points} for titre, points in (
        ("Vos sorties", parler_sorties(sorties, aujourdhui)),
        ("Performance", parler_performance(sorties, resultat, profil, aujourdhui)),
        ("Votre semaine", parler_semaine(resultat, profil, sante, aujourdhui)),
        ("Récupération", parler_recuperation(resultat, sante, aujourdhui) + parler_sommeil(sante, aujourdhui) + parler_pas(sante, aujourdhui)),
        ("Repas", parler_repas(resultat, profil, sante, aujourdhui))) if points]


def essentiel(sections):
    """Les phrases données au modèle : peu nombreuses, car un petit modèle déforme ce qu'on lui donne en trop grand nombre."""
    rubriques = {s["titre"]: s["points"] for s in sections}
    return "\n".join(rubriques.get("Vos sorties", [])[1:3] + rubriques.get("Performance", []) + rubriques.get("Votre semaine", [])[:1] + rubriques.get("Récupération", [])[:1] + rubriques.get("Repas", [])[:1])


def nombres(texte):
    """Les chiffres d'un texte, écrits d'une seule façon : 13,5 et 13.5, 7 000 et 7000, 6:30 et 6 h 30 restent comparables."""
    texte = re.sub(r"(?<=\d)[\s  ](?=\d{3}\b)", "", texte)
    return {n.replace(".", ",") for n in re.findall(r"\d+(?:[.,:]\d+)?", texte)}


def note_valable(note, faits):
    """Le filet de sécurité : la bonne longueur, aucun nombre ajouté ou écrit en lettres, aucun sujet médical."""
    return MOTS[0] <= len(note.split()) <= MOTS[1] and nombres(note) <= nombres(faits) and not EN_LETTRES.search(note) and not INTERDIT.search(note)


def rediger(faits, modele=MODELE, adresse=ADRESSE_IA):
    """Le texte du modèle, ou None s'il ne répond pas. Il ne reçoit que les phrases des règles."""
    corps = json.dumps({"model": modele, "stream": False, "think": False, "options": {"temperature": .2, "num_predict": 400},
                        "messages": [{"role": "system", "content": CONSIGNE}, {"role": "user", "content": faits}]}).encode()
    requete = urllib.request.Request(f"{adresse}/api/chat", corps, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=ATTENTE_IA) as reponse:
            return json.load(reponse)["message"]["content"].strip()
    except (OSError, KeyError, ValueError) as e:  # conteneur arrêté ou modèle absent : les règles suffisent
        print(f"Coach : pas de rédaction ({e})")
        return None


def ecrire(resultat, v2, sante, garmin):
    aujourdhui = date.today()
    sections = regles(resultat, carnet.lire(sante)["profil"], v2, sante, garmin, aujourdhui)
    faits = essentiel(sections)
    debut = time.time()
    note = None
    for _ in range(ESSAIS_IA if faits else 0):
        texte = rediger(faits)
        if not texte:
            break
        if note_valable(texte, faits):
            note = texte
            break
        print(f"Coach : note écartée (nombre ajouté, sujet médical ou longueur) : {texte[:200]}")
    (sante / "coach.json").write_text(json.dumps({"date": aujourdhui.isoformat(), "note": note, "sections": sections}, ensure_ascii=False))
    print(f"Coach : {'note rédigée en ' + str(round(time.time() - debut)) + ' s' if note else 'règles seules'}, dans {sante / 'coach.json'}")
