"""L'écran « Conseils » : semaine type, manger, boire, avant et après la course, renforcement (utilisé par web.py).

Aucun repère n'est inventé ici : chacun vient d'une entrée de SOURCES, et le code ne fait que l'appliquer à vos mesures
(poids, durée des sorties, jour habituel) et à vos réponses (sante/carnet.json, section « profil »), d'où un calcul à chaque
demande : une réponse compte tout de suite. Une mesure absente retire la phrase qui s'en sert, jamais le repère général, et
une question sans réponse garde le conseil général.
Le renforcement reprend le programme de l'essai Run RCT : c'est le seul testé sur des coureurs de loisir qui soit public.
"""
import json
from collections import Counter
from datetime import date
from itertools import combinations
from statistics import median

from performance import COURSES, FC_FACILE, csv_jours, date_fr, fenetre, virgule

JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
DERNIERE_PESEE = 400  # jours : au-delà, le poids connu ne dit plus rien
PROTEINES = .83  # g par kg et par jour (EFSA)
PROTEINES_REPAS = (.25, .4)  # g par kg à un repas (ISSN)
PERTE_SUEUR = .02  # part du poids à ne pas perdre en sueur (ACSM)
HAUSSE_SORTIE = 1.1  # au-delà de la plus longue sortie du mois, le risque de blessure monte (Frandsen 2025)
AGES_ESSAI = (18, 55)  # ans, les adultes retenus dans l'essai Run RCT
EAU = {"femme": ("environ 2 litres", 1600, "1,6 litre"), "homme": ("environ 2,5 litres", 2000, "2 litres")}  # par jour, total puis à boire : 80 % du total (EFSA)
PAS = 7000  # par jour (Ding 2025)
ENDURANCE = 150  # minutes d'activité modérée par semaine, une minute soutenue en valant deux (ACSM, NHS)
ACTIVITES = {"marche": "marche", "velo": "vélo", "natation": "natation", "yoga": "yoga"}  # réponses possibles, et leur nom
FAMILLES = {"marche": "marche", "randonnee": "marche", "velo": "velo", "velo-interieur": "velo", "natation-piscine": "natation",
            "natation-eau-libre": "natation", "yoga": "yoga"}  # catégories de la bibliothèque
HABITUDE_AUTRE = 3  # séances en un an pour qu'un sport compte comme le vôtre
SOMMEIL = 7  # heures par nuit, au moins (Watson 2015)
JAMBES = {"SQUAT", "LUNGE", "DEADLIFT", "CALF_RAISE", "LEG_CURL", "HIP_RAISE", "HIP_STABILITY"}  # catégories d'exercices de Garmin

SOURCES = {
    "anses": ("ANSES, repères de consommation alimentaire", "https://www.anses.fr/fr/content/lanses-actualise-les-reperes-de-consommations-alimentaires-pour-la-population-francaise"),
    "oms": ("OMS, alimentation saine", "https://www.who.int/fr/news-room/fact-sheets/detail/healthy-diet"),
    "efsa-proteines": ("EFSA, protéines, 2012", "https://doi.org/10.2903/j.efsa.2012.2557"),
    "issn": ("Société internationale de nutrition du sport, 2017", "https://doi.org/10.1186/s12970-017-0189-4"),
    "sportifs": ("Vitale et Getzin, 2019, qui reprend la position des diététiciens et de l'ACSM", "https://doi.org/10.3390/nu11061289"),
    "victoria": ("État de Victoria, manger avant et après l'effort", "https://www.getactive.vic.gov.au/wp-content/uploads/2025/12/What-To-Eat-Before-And-After-Exercise.pdf"),
    "dieteticiens": ("Diététiciens du sport d'Australie", "https://www.sportsdietitians.com.au/factsheets/fuelling-recovery/eating-drinking-exercise"),
    "position": ("Position commune des diététiciens et de l'ACSM, 2016", "https://doi.org/10.1249/MSS.0000000000000852"),
    "efsa-eau": ("EFSA, eau, 2010", "https://doi.org/10.2903/j.efsa.2010.1459"),
    "acsm-eau": ("ACSM, boire à l'effort, 2007", "https://doi.org/10.1249/mss.0b013e31802ca597"),
    "sodium": ("Consensus sur le manque de sodium à l'effort, 2015", "https://doi.org/10.1097/JSM.0000000000000221"),
    "run-rct": ("Essai Run RCT, Leppänen et al., 2024", "https://jyx.jyu.fi/handle/123456789/95333"),
    "exercices": ("Le programme complet de l'essai, avec photos", "https://ukkinstituutti.fi/wp-content/uploads/2024/06/TheRunRCTHipAndCoreProgram.pdf"),
    "lauersen": ("Lauersen et al., 25 essais, 2014", "https://doi.org/10.1136/bjsports-2013-092538"),
    "cochrane": ("Cochrane, étirements et courbatures", "https://doi.org/10.1002/14651858.CD004577.pub3"),
    "mythe": ("Alexander et al., 2020", "https://doi.org/10.1136/bjsports-2019-101169"),
    "nhs-etirements": ("NHS, étirements après l'effort", "https://www.nhs.uk/live-well/exercise/running-and-aerobic-exercises/how-to-stretch-after-exercising/"),
    "acsm-2011": ("ACSM, quantité d'exercice, 2011", "https://doi.org/10.1249/MSS.0b013e318213fefb"),
    "nhs-activite": ("NHS, repères d'activité des adultes", "https://www.nhs.uk/live-well/exercise/exercise-guidelines/physical-activity-guidelines-for-adults-aged-19-to-64/"),
    "frandsen": ("Frandsen et al., 5 200 coureurs, 2025", "https://doi.org/10.1136/bjsports-2024-109380"),
    "ding": ("Ding et al., 57 études, 2025", "https://doi.org/10.1016/S2468-2667(25)00164-1"),
    "sommeil": ("Consensus des sociétés américaines du sommeil, 2015", "https://doi.org/10.5664/jcsm.4758"),
    "boissons": ("Part des boissons selon l'EFSA, citée par une étude française de 2016", "https://pmc.ncbi.nlm.nih.gov/articles/PMC5133077"),
}

# le niveau le plus accessible de chaque famille du programme Run RCT
EXERCICES = (("Planche", "Sur les avant-bras et la pointe des pieds, le corps bien droit.", "20 à 40 s"),
             ("Planche sur le côté", "Sur un avant-bras, les jambes tendues, le bassin levé.", "20 à 40 s par côté"),
             ("Pont", "Sur le dos, genoux pliés, pieds au sol : montez le bassin et gardez-le en haut.", "20 à 40 s"),
             ("Squat avec mini-bande", "Une petite boucle élastique autour des cuisses, au-dessus des genoux : fléchissez en gardant les genoux écartés.", "10 à 15 fois"),
             ("Lever de jambe tendue", "Sur le dos, une jambe pliée, l'autre tendue : levez lentement la jambe tendue.", "8 à 12 fois par jambe"),
             ("Fente sur place", "Un grand pas en avant, puis descendez le genou arrière vers le sol et remontez.", "8 à 12 fois par jambe"),
             ("Talons qui glissent", "En pont, les talons sur une serviette, sur un sol lisse : éloignez les pieds en glissant, puis ramenez-les.", "8 à 12 fois"),
             ("Fente sur le côté", "Un grand pas de côté en fléchissant cette jambe, l'autre reste tendue. Alternez.", "6 à 10 fois par côté"))
SQUAT = 3  # sa place dans EXERCICES
SQUAT_SANS_ELASTIQUE = ("Squat", "Fléchissez les jambes en gardant les genoux écartés.", "10 à 15 fois")


def prudence(profil):
    """Une blessure ou un problème de santé signalés : rien n'ajoute d'effort sans l'avis d'un professionnel."""
    return "oui" in (profil.get("blessure"), profil.get("sante"))


def kg_fr(poids):
    return virgule(poids).removesuffix(",0")


def pas_fr(valeur):
    """À la centaine : ce sont des médianes."""
    return f"{100 * round(valeur / 100):,}".replace(",", "\u202f")


def duree_fr(minutes):
    """À 5 minutes près : c'est un ordre de grandeur."""
    heures, reste = divmod(5 * round(minutes / 5), 60)
    return f"{heures} h {reste:02d}" if heures and reste else f"{heures} h" if heures else f"{reste} min"


def series_jambes(garmin):
    """(séries pour les jambes, séries reconnues) des séances de musculation passées ; la montre ne reconnaît pas toutes les séries."""
    jambes = reconnues = 0
    for fichier in (garmin / "exercices").glob("*.json"):
        for serie in json.loads(fichier.read_text()).get("exerciseSets") or []:
            if serie.get("setType") == "ACTIVE" and serie.get("exercises"):
                categorie = max(serie["exercises"], key=lambda e: e.get("probability") or 0).get("category")
                if categorie not in ("UNKNOWN", "CARDIO"):
                    reconnues += 1
                    jambes += categorie in JAMBES
    return jambes, reconnues


def preferences(profil, seances, aujourdhui):
    """Les sports choisis dans vos réponses, sinon ceux de votre dernière année, sinon la marche."""
    if profil.get("activites"):
        return [a for a in ACTIVITES if a in profil["activites"]]
    vus = Counter(FAMILLES[s["categorie"]] for s in seances if s["categorie"] in FAMILLES and (aujourdhui - s["jour"]).days <= 365)
    return [a for a in ACTIVITES if vus[a] >= HABITUDE_AUTRE] or ["marche"]


def minutes_actives(sante, aujourdhui):
    """Minutes d'activité par semaine selon la montre, sur les 4 dernières semaines : une minute soutenue en vaut deux.
    Tous les sports y sont, marche rapide comprise."""
    moderees, intenses = csv_jours(sante / "garmin.csv", "minutes_moderees"), csv_jours(sante / "garmin.csv", "minutes_intenses")
    jours = [j for j in moderees if 0 < (max(moderees) - j).days <= 28] if moderees else []  # le dernier jour s'arrête à la dernière synchronisation
    return sum(moderees[j] + 2 * intenses.get(j, 0) for j in jours) / len(jours) * 7 if len(jours) >= 21 else None


def ecart(a, b):
    """Jours entre deux jours de la semaine, dans le sens le plus court."""
    return min((a - b) % 7, (b - a) % 7)


def autres_jours(sorties, principal, renforts, profil, prefs):
    """Les jours sans course : le renforcement qui manque, deux séances d'endurance sans chocs, et la veille de course au calme."""
    veille = (principal - 1) % 7
    libres = [j for j in (profil.get("jours_autres") or range(7)) if j not in sorties and j != veille]
    endurance = [ACTIVITES[a] for a in prefs if a != "yoga"] or ["marche"]
    seance = (" ou ".join([", ".join(endurance[:-1]), endurance[-1]]) if len(endurance) > 1 else endurance[0]) \
        + f", 30 min ou plus, puis {'yoga ou étirements' if 'yoga' in prefs else 'étirements'} si vous aimez"
    occupes, lignes = list(sorties), {}
    for tache in ["Renforcement à la maison, 20 à 35 min"] * (renforts < 2) + [seance[0].upper() + seance[1:]] * 2:
        if libres:
            jour = max(libres, key=lambda j: min(ecart(j, o) for o in occupes))
            lignes[jour] = tache
            occupes.append(jour)
            libres.remove(jour)
    if veille not in sorties:
        lignes[veille] = "Repos ou marche tranquille, la veille de courir"
    return lignes


def repartir(jour, nombre, possibles):
    """Le jour habituel s'il reste possible, et les autres le plus loin possible les uns des autres."""
    espacement = lambda jours: min(((b - a) % 7 for a in jours for b in jours if a != b), default=7)
    return max(combinations(possibles, min(nombre, len(possibles))), key=lambda jours: (jour in jours, espacement(jours)))


def semaine(courses, recentes, resultat, profil, prefs, minutes, aujourdhui):
    fc_facile = round(FC_FACILE * resultat["fc_max"])
    habitude = Counter(s["jour"].weekday() for s in courses if (aujourdhui - s["jour"]).days < 182)
    jour = habitude.most_common(1)[0][0] if habitude else 6
    actuel = max(1, round(len(recentes) / 4))
    plus_longue = max((s["km"] for s in courses if (aujourdhui - s["jour"]).days < 30), default=0)
    points = []
    if plus_longue:
        points.append(f"Méfiez-vous de la sortie trop longue d'un coup : le risque de blessure monte de 64 % quand on dépasse de plus de 10 % sa plus longue sortie du mois. "
                      f"La vôtre fait {virgule(plus_longue)} km : restez sous {virgule(HAUSSE_SORTIE * plus_longue)} km.")
    if minutes:
        points.append(f"Tous vos sports comptent : le repère est d'au moins {ENDURANCE} minutes d'activité modérée par semaine, une minute soutenue en valant deux. "
                      f"Selon votre montre, vous en êtes à {minutes:.0f} par semaine sur les 4 dernières semaines" + (", c'est atteint." if minutes >= ENDURANCE else "."))
    section = {"titre": "Votre semaine", "points": points, "sources": ["frandsen", "run-rct", "acsm-2011", "nhs-activite"]}
    ajouter = resultat["peut_ajouter"] and not prudence(profil)
    garder = ("Vous avez signalé une blessure ou un problème de santé : gardez votre rythme, sans rien ajouter, et demandez l'avis d'un médecin ou d'un kiné." if prudence(profil)
              else "Un signal de fatigue est là (voir l'Accueil) : cette semaine, gardez votre rythme habituel, sans rien ajouter.")
    if actuel >= 3:
        section["intro"] = "Vous courez déjà 3 fois par semaine ou plus : gardez ce rythme, et ajoutez le renforcement avant deux de vos sorties." if ajouter else garder
        return section
    sorties = repartir(jour, actuel + 1 if ajouter else actuel, profil.get("jours") or range(7))
    principal = jour if jour in sorties else sorties[0]
    lignes = {}
    for n, j in enumerate(sorted(sorties, key=lambda j: j != principal)):
        sortie = "Votre sortie habituelle" if not n else f"Sortie tranquille de 20 à 30 min, sous {fc_facile} battements"
        lignes[j] = f"Renforcement, puis {sortie[0].lower()}{sortie[1:]}" if ajouter and n < 2 else sortie
    autres = autres_jours(sorties, principal, min(len(sorties), 2), profil, prefs) if ajouter else {}
    lignes.update(autres)
    section["autres"] = sorted([j, t] for j, t in autres.items() if not t.startswith("Repos"))
    maison = " et une seconde fois à la maison un autre jour" if any(t.startswith("Renforcement à la maison") for t in autres.values()) else " : cela évite un jour de plus"
    section["intro"] = (f"Construite autour de votre sortie du {JOURS[principal]}. Le renforcement se fait juste avant de courir, comme dans l'étude,{maison}. "
                        "Les autres jours, de l'endurance sans les chocs de la course, et la veille au calme.") if ajouter else garder
    section["tableau"] = {"titres": ["Jour", "Quoi"], "lignes": [[JOURS[j].capitalize(), lignes.get(j, "Marche ou repos")] for j in range(7)]}
    return section


def manger(recentes, longue, poids, date_pesee, aujourdhui):
    points = []
    if recentes:
        points.append(f"Pas besoin d'alimentation de sportif : les quantités pour sportifs visent au moins 1 h d'entraînement par jour, et vous courez environ {duree_fr(sum(s['minutes'] for s in recentes) / 4)} par semaine.")
    points += ["Plus de légumes d'abord, des fruits, et souvent des légumineuses : lentilles, pois chiches, haricots secs.",
               "Pain, pâtes et riz complets ou à moitié complets plutôt que blancs.",
               "Du poisson deux fois par semaine, dont un poisson gras. De l'huile de colza ou de noix.",
               "Au plus 500 g de viande par semaine hors volaille, 25 g de charcuterie par jour, moins d'un verre de boisson sucrée par jour (jus de fruits compris), moins de 5 g de sel par jour."]
    if poids:
        points.append(f"Protéines : le repère courant est de 0,83 g par kg et par jour, soit environ {5 * round(PROTEINES * poids / 5)} g pour vos {kg_fr(poids)} kg (pesée du {date_fr(date_pesee, aujourdhui)}). "
                      f"Les jours de renforcement, mettez-en à chaque repas, environ {5 * round(PROTEINES_REPAS[0] * poids / 5)} à {5 * round(PROTEINES_REPAS[1] * poids / 5)} g : viande, poisson, œufs, laitage ou légumineuses.")
    points += ["Avant de courir : un repas 3 à 4 h avant, ou une collation 1 à 2 h avant, par exemple un fruit, du pain ou des céréales."]
    pendant = "Pendant : rien à manger sous 1 h. Au-delà de 1 h à 1 h 30, prenez une petite collation pauvre en fibres et en graisses, facile à digérer."
    if longue and longue["minutes"] >= 60:
        pendant += f" Votre plus longue sortie du mois a duré {duree_fr(longue['minutes'])}."
    points += [pendant, "Après : un repas équilibré avec des féculents et des protéines, par exemple un yaourt et un fruit, ou un œuf sur du pain. Pas besoin de boisson protéinée."]
    return {"titre": "Manger", "points": points, "sources": ["anses", "oms", "efsa-proteines", "issn", "sportifs", "victoria"]}


def boire(poids, sexe):
    repere = (f"{EAU[sexe][0]} en comptant l'eau des aliments, soit {EAU[sexe][2]} à boire" if sexe in EAU
              else "environ 2 litres pour une femme et 2,5 litres pour un homme en comptant l'eau des aliments, soit 1,6 et 2 litres à boire")
    points = [f"Chaque jour : {repere}. Ce repère vaut pour un temps tempéré et une activité modérée : buvez plus quand il fait chaud.",
              "Pendant la course : de l'eau, quand vous avez soif. Inutile de vous forcer : c'est en buvant trop qu'on provoque une baisse dangereuse du sodium dans le sang.",
              "Les boissons pour sportifs ne sont pas nécessaires : pour la plupart des gens, l'eau fait aussi bien, sans le sucre."]
    if poids:
        points.append(f"Pour connaître vos besoins, pesez-vous avant et après une longue sortie : 1 kg perdu, c'est environ 1 litre de sueur. Au-delà de 2 % de votre poids, soit {virgule(PERTE_SUEUR * poids)} kg, buvez davantage la fois suivante. "
                      "Après la course, buvez environ 1,5 fois ce que vous avez perdu.")
    points.append("Vous pouvez noter ce que vous buvez dans Santé, à la rubrique Eau.")
    return {"titre": "Boire", "points": points, "sources": ["efsa-eau", "boissons", "sodium", "acsm-eau", "sportifs", "victoria", "dieteticiens"]}


def renforcement(seances, garmin, profil, maison, aujourdhui):
    derniere = max((s["jour"] for s in seances if s["categorie"] == "musculation"), default=None)
    jambes, reconnues = series_jambes(garmin)
    lignes = [list(e) for e in EXERCICES]
    points = ["C'est ce qui protège le mieux : sur 25 essais, il a divisé les blessures de sport par plus de 3, alors que les étirements n'ont rien changé.",
              "Chez des coureurs débutants, ce programme des hanches et du tronc, encadré par des kinés, a réduit de 39 % les blessures dues à la répétition, par rapport à de simples étirements.",
              "Les coureurs de l'essai le faisaient 2 fois par semaine en moyenne, ce qui correspond aussi au repère de 2 jours de renforcement par semaine."]
    if maison:
        points.append("La seconde séance se fait à la maison, un jour sans course (voir Votre semaine) : l'essai la plaçait avant de courir, mais le repère reste de 2 jours par semaine.")
    elif len(profil.get("jours") or ()) == 1:
        points.append("Avec un seul jour possible, vous en resterez à une séance par semaine, soit la moitié de celles de l'essai.")
    points.append("Le yoga et le pilates comptent aussi comme renforcement pour le NHS, mais ils ne remplacent pas ce programme, le seul testé sur des coureurs.")
    if profil.get("naissance") and not AGES_ESSAI[0] <= (age := aujourdhui.year - int(profil["naissance"])) <= AGES_ESSAI[1]:
        points.append(f"L'essai portait sur des adultes de {AGES_ESSAI[0]} à {AGES_ESSAI[1]} ans : son effet n'a pas été mesuré à {age} ans.")
    if derniere:
        points.append(f"Votre dernière séance date du {date_fr(derniere, aujourdhui)}." + (
            f" Vos séances passées visaient peu les jambes : {jambes} séries sur {reconnues} reconnues par la montre. Ce programme rééquilibre." if reconnues >= 20 and jambes < reconnues / 3 else ""))
    if profil.get("elastique") == "non":
        lignes[SQUAT] = list(SQUAT_SANS_ELASTIQUE)
        points.append("Sans mini-bande, ce squat n'est plus tout à fait l'exercice de l'étude.")
    elif not profil.get("elastique"):
        points.append("Sans mini-bande, faites un squat simple : ce n'est plus tout à fait l'exercice de l'étude.")
    points.append("Le programme d'origine propose 4 niveaux par exercice, voici les plus accessibles.")
    intro = "Comptez 20 à 35 minutes, comme dans l'essai, après 5 minutes d'échauffement. Faites chaque exercice 2 fois, 3 fois quand cela devient facile. Arrêtez un exercice s'il fait mal."
    if prudence(profil):
        intro = "À valider d'abord avec un médecin ou un kiné : l'essai n'a pas porté sur des personnes récemment blessées ou ayant une maladie générale. " + intro
    return {"titre": "Renforcement", "intro": intro,
            "tableau": {"titres": ["Exercice", "Comment", "Combien"], "lignes": lignes}, "points": points,
            "sources": ["exercices", "run-rct", "lauersen", "nhs-activite", "acsm-2011"]}


def situer(valeurs, cible, unite):
    """Votre habitude par rapport à une cible, en une phrase ; rien sans mesure."""
    if not valeurs:
        return ""
    habitude = median(valeurs)
    return f" Sur 3 mois, votre médiane est de {unite(habitude)}{' : vous y êtes' if habitude >= cible else ''}."


def suggestions(profil, sante, aujourdhui):
    """Les cibles de santé que vous n'avez pas choisies : un repère publié, situé par rapport à vos 3 derniers mois. Rien pour le poids ni le jeûne : aucun repère ne vaut pour tous."""
    pas = fenetre(csv_jours(sante / "pas.csv", "pas"), aujourdhui, 90, -1)
    nuits = fenetre(csv_jours(sante / "sommeil.csv", "total_h"), aujourdhui, 90, -1)
    cibles = {"pas": (PAS, "7\u202f000 pas par jour : un repère réaliste, déjà associé à un net gain pour la santé par rapport à 2\u202f000 pas."
                      + situer(pas, PAS, lambda v: f"{pas_fr(v)} pas"), "ding"),
              "sommeil": (SOMMEIL, "Au moins 7 h par nuit, le repère des médecins du sommeil américains." + situer(nuits, SOMMEIL, lambda v: f"{virgule(v)} h"), "sommeil")}
    if profil.get("sexe") in EAU:
        total, boissons, texte = EAU[profil["sexe"]]
        cibles["eau_ml"] = (boissons, f"{texte.capitalize()} à boire par jour : le reste de l'eau vient des aliments.", "boissons")
    return {cle: {"valeur": valeur, "texte": texte, "source": {"nom": SOURCES[source][0], "url": SOURCES[source][1]}} for cle, (valeur, texte, source) in cibles.items()}


def semaine_de(resultat, profil, sante, aujourdhui):
    """La semaine type, à partir des résultats de performance.py : aussi pour le mot du coach, qui dit la même chose."""
    seances = [{**s, "jour": date.fromisoformat(s["jour"])} for s in resultat["seances"]]
    courses = [s for s in seances if s["categorie"] in COURSES]
    recentes = [s for s in courses if (aujourdhui - s["jour"]).days < 28]
    return semaine(courses, recentes, resultat, profil, preferences(profil, seances, aujourdhui), minutes_actives(sante, aujourdhui), aujourdhui)


def construire(resultat, profil, sante, garmin, aujourdhui):
    """Les sections de l'écran, avec leurs sources : le texte est fait ici, la page ne fait que l'afficher."""
    seances = [{**s, "jour": date.fromisoformat(s["jour"])} for s in resultat["seances"]]
    courses = [s for s in seances if s["categorie"] in COURSES]
    recentes = [s for s in courses if (aujourdhui - s["jour"]).days < 28]
    longue = max(recentes, key=lambda s: s["minutes"], default=None)
    pesees = {j: kg for j, kg in csv_jours(sante / "poids.csv", "kg").items() if 0 <= (aujourdhui - j).days <= DERNIERE_PESEE}
    if profil.get("poids_kg"):  # le plus récent gagne : une pesée suivante remplace votre réponse
        pesees[date.fromisoformat(profil["poids_date"])] = float(profil["poids_kg"])
    date_pesee = max(pesees, default=None)
    poids = pesees.get(date_pesee)
    plan = semaine_de(resultat, profil, sante, aujourdhui)
    maison = any(t.startswith("Renforcement à la maison") for _, t in plan.get("autres", []))
    sections = [plan, manger(recentes, longue, poids, date_pesee, aujourdhui), boire(poids, profil.get("sexe")),
                {"titre": "Avant et après la course",
                 "points": ["Échauffez-vous 5 minutes : petit footing, montées de genoux, talons aux fesses, pas chassés. Cela améliore la performance ; l'effet sur les blessures n'est pas établi.",
                            "Les étirements sont facultatifs : ils ne réduisent pas les blessures et presque pas les courbatures (1 point sur 100 après l'effort). Ce temps est mieux placé dans le renforcement.",
                            "Si vous aimez vous étirer, gardez-le, cela ne nuit pas. Après la course, 15 à 20 secondes chacun : fessiers, arrière de la cuisse, intérieur de la cuisse, mollets, avant de la cuisse. Comptez 5 minutes.",
                            "Pour garder de la souplesse, l'ACSM conseille deux fois par semaine, 60 secondes au total par étirement."],
                 "sources": ["exercices", "mythe", "lauersen", "cochrane", "nhs-etirements", "acsm-2011"]},
                renforcement(seances, garmin, profil, maison, aujourdhui),
                {"titre": "Quand demander à un professionnel",
                 "points": ["Douleur dans la poitrine, malaise, essoufflement inhabituel : arrêtez-vous et appelez le 15 ou le 112.",
                            "Une douleur qui change votre façon de courir, ou qui revient à chaque sortie : levez le pied et voyez un médecin ou un kiné.",
                            "Diabète, maladie du cœur ou des reins, grossesse, blessure, régime particulier : ces repères peuvent changer pour vous, demandez à votre médecin.",
                            "Pour un plan alimentaire fait pour vous, voyez un diététicien : c'est aussi ce que conseillent les sociétés savantes."],
                 "sources": ["position", "nhs-etirements"]}]
    if prudence(profil):
        sections.insert(0, sections.pop())
    for section in sections:
        section["sources"] = [{"nom": SOURCES[cle][0], "url": SOURCES[cle][1]} for cle in section["sources"]]
    return {"sections": sections, "pesee": poids and f"{kg_fr(poids)} kg, le {date_fr(date_pesee, aujourdhui)}", "suggestions": suggestions(profil, sante, aujourdhui)}


def appliquer(resultat, profil, sante, garmin, aujourdhui):
    """Ajoute l'écran Conseils aux résultats, et accorde les conseils de l'Accueil sur vos réponses : les deux écrans disent la même chose."""
    retirer = {"sortie", "effort"} if prudence(profil) else {"sortie"} if len(profil.get("jours") or ()) == 1 else set()
    resultat["conseils"] = [c for c in resultat["conseils"] if c.get("ajoute") not in retirer]
    if prudence(profil):
        resultat["conseils"].insert(0, {"niveau": "attention", "titre": "Avant d'ajouter de l'effort",
                                        "detail": "Vous avez signalé une blessure ou un problème de santé dans Conseils : demandez d'abord l'avis d'un médecin ou d'un kiné."})
    resultat["programme"] = construire(resultat, profil, sante, garmin, aujourdhui)
