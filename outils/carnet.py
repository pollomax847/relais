"""Ce que vous saisissez vous-même, repris d'Endurain et de ses champs : titre, description, notes, ressenti et matériel des
activités, matériel et composants, objectifs, cibles de santé, eau, jeûne, transit. Le tout dans sante/carnet.json, en dehors
de activites-v2/, que chaque conversion refait : une activité y est repérée par son heure de départ, qui survit à un
changement de nom de fichier. Les photos d'une activité vont dans sante/medias/.
"""
import json
import re
import threading
import uuid
from pathlib import Path

SECTIONS_DICT = ("activites", "cibles", "eau", "materiel_defaut")  # une valeur par clé
SECTIONS_LISTE = ("materiel", "objectifs", "jeune", "transit")  # des fiches avec un identifiant
CHAMPS_ACTIVITE = ("titre", "description", "notes", "ressenti", "effort", "materiel", "masquee")
TYPES_MEDIA = {".jpg", ".jpeg", ".png", ".webp"}
TYPES_IMPORT = {".fit", ".gpx", ".tcx"}
VERROU = threading.Lock()


def lire(dossier):
    fichier = Path(dossier) / "carnet.json"
    carnet = json.loads(fichier.read_text()) if fichier.exists() else {}
    return {**{s: {} for s in SECTIONS_DICT}, **{s: [] for s in SECTIONS_LISTE}, **carnet}


def _ecrire(dossier, carnet):
    """Écrit à côté puis remplace : une coupure ne laisse jamais un carnet à moitié écrit."""
    fichier = Path(dossier) / "carnet.json"
    provisoire = fichier.with_suffix(".tmp")
    provisoire.write_text(json.dumps(carnet, ensure_ascii=False, indent=1))
    provisoire.replace(fichier)


def modifier(dossier, section, cle, valeur):
    """Enregistre une valeur (None l'efface). Section à fiches : cle est l'identifiant de la fiche, None pour en créer une."""
    with VERROU:
        carnet = lire(dossier)
        if section in SECTIONS_DICT:
            if not isinstance(cle, str) or not cle:
                raise ValueError("clé manquante")
            if section == "activites" and valeur is not None:
                valeur = {c: v for c, v in valeur.items() if c in CHAMPS_ACTIVITE and v not in (None, "", False)}
            if valeur in (None, {}, ""):
                carnet[section].pop(cle, None)
            else:
                carnet[section][cle] = valeur
        elif section in SECTIONS_LISTE:
            fiches = [f for f in carnet[section] if f["id"] != cle]
            if valeur is not None:
                fiches.append({**valeur, "id": cle or uuid.uuid4().hex[:12]})
            carnet[section] = fiches
        else:
            raise ValueError(f"section inconnue : {section}")
        _ecrire(dossier, carnet)
    return carnet


def nom_sur(nom, types):
    """Le nom d'un fichier reçu, sans son chemin et limité aux extensions attendues : rien ne s'écrit hors du dossier prévu."""
    propre = re.sub(r"[^\w.\- ]", "_", Path(nom or "").name).strip(" .")
    if not propre or Path(propre).suffix.lower() not in types:
        raise ValueError(f"fichier refusé : {', '.join(sorted(types))} seulement")
    return propre


def dossier_medias(dossier, debut_utc):
    return Path(dossier) / "medias" / re.sub(r"\W", "-", debut_utc)
