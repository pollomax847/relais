"""Mot de passe de l'interface, comme sur Navidrome. Tant qu'aucun n'est choisi, relais reste ouvert.

Seule une empreinte scrypt est gardée, dans ~/.config/sport/acces.json : le SSD exFAT ne sait pas protéger un fichier. La
connexion donne un jeton signé, valable DUREE secondes ; changer de mot de passe change la clé de signature et déconnecte
donc tous les appareils. Mot de passe oublié : relais.py mot-de-passe, dans un terminal.

Sur le réseau de la maison, relais.py certificat chiffre l'accès. Il permet aussi à Chrome d'installer l'application.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

FICHIER = Path(os.environ.get("RELAIS_ACCES") or Path.home() / ".config" / "sport" / "acces.json")
CERTIFICATS = FICHIER.parent / "https"  # autorite.crt va sur le téléphone ; relais.crt et relais.key restent ici
VALIDITE = "3650"  # jours
DUREE = 30 * 86400
LONGUEUR_MIN = 8
ESSAIS, ATTENTE = 5, 60  # après 5 mots de passe faux, plus aucun essai pendant 60 s
VERROU = threading.Lock()
echecs, bloque_jusqua = 0, 0.0


def actif():
    return FICHIER.exists()


def _empreinte(mot, sel):
    return hashlib.scrypt(mot.encode(), salt=bytes.fromhex(sel), n=2 ** 15, r=8, p=1, maxmem=64 * 2 ** 20, dklen=32).hex()


def definir(mot):
    if len(mot) < LONGUEUR_MIN:
        raise ValueError(f"mot de passe trop court : {LONGUEUR_MIN} caractères au moins")
    sel = secrets.token_hex(16)
    contenu = {"sel": sel, "empreinte": _empreinte(mot, sel), "cle": secrets.token_hex(32)}
    FICHIER.parent.mkdir(parents=True, exist_ok=True)
    provisoire = FICHIER.with_suffix(".tmp")
    with os.fdopen(os.open(provisoire, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(contenu, f)
    provisoire.replace(FICHIER)


def retirer():
    FICHIER.unlink(missing_ok=True)


def verifier(mot):
    if not actif():
        return False
    d = json.loads(FICHIER.read_text())
    return hmac.compare_digest(_empreinte(mot, d["sel"]), d["empreinte"])


def essayer(mot):
    """Vérifie un mot de passe saisi à la connexion, en limitant les essais."""
    global echecs, bloque_jusqua
    with VERROU:
        if time.time() < bloque_jusqua:
            raise ValueError("trop d'essais : réessayez dans une minute")
        bon = verifier(mot)
        echecs = 0 if bon else echecs + 1
        if echecs >= ESSAIS:
            echecs, bloque_jusqua = 0, time.time() + ATTENTE
    return bon


def _signature(fin, cle):
    return hmac.new(bytes.fromhex(cle), str(fin).encode(), hashlib.sha256).hexdigest()


def jeton():
    fin = int(time.time()) + DUREE
    return f"{fin}.{_signature(fin, json.loads(FICHIER.read_text())['cle'])}"


def valide(texte):
    if not actif():
        return False
    fin, _, signature = (texte or "").partition(".")
    return fin.isdigit() and int(fin) > time.time() and hmac.compare_digest(signature, _signature(fin, json.loads(FICHIER.read_text())["cle"]))


def creer_certificat(adresse):
    """Le téléphone fait confiance à une autorité qui n'aura signé que ce certificat : sa clé disparaît avec le dossier temporaire."""
    ipaddress.ip_address(adresse)
    CERTIFICATS.mkdir(mode=0o700, parents=True, exist_ok=True)
    cle = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]

    def openssl(*arguments):
        subprocess.run(["openssl", *map(str, arguments)], check=True, capture_output=True)

    with tempfile.TemporaryDirectory() as temporaire:
        dossier = Path(temporaire)
        openssl("req", "-x509", *cle, "-days", VALIDITE, "-subj", "/CN=Relais", "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", dossier / "autorite.key", "-out", CERTIFICATS / "autorite.crt")
        openssl("req", *cle, "-subj", f"/CN={adresse}", "-keyout", CERTIFICATS / "relais.key", "-out", dossier / "demande.csr")
        (dossier / "extensions").write_text(f"subjectAltName=IP:{adresse}\nbasicConstraints=CA:FALSE\n"
                                            "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n")
        openssl("x509", "-req", "-in", dossier / "demande.csr", "-CA", CERTIFICATS / "autorite.crt", "-CAkey", dossier / "autorite.key",
                "-days", VALIDITE, "-set_serial", secrets.randbits(64), "-extfile", dossier / "extensions", "-out", CERTIFICATS / "relais.crt")
