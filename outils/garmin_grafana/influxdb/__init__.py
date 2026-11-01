"""Remplace le client InfluxDB pour le code de garmin-grafana, gardé tel quel à côté de ce dossier : les points vont dans
une base SQLite locale (GARMIN_GRAFANA_BASE) au lieu d'un serveur. Comme dans InfluxDB, un point de même mesure, même heure
et mêmes étiquettes remplace le précédent. Seul ce dont garmin_fetch.py se sert est fourni."""
import json
import os
import sqlite3


class StockageLocal:
    def __init__(self, *_, **__):
        self.base = sqlite3.connect(os.environ["GARMIN_GRAFANA_BASE"])
        self.base.execute("""CREATE TABLE IF NOT EXISTS points (mesure TEXT NOT NULL, temps TEXT NOT NULL, etiquettes TEXT NOT NULL,
                             champs TEXT NOT NULL, PRIMARY KEY (mesure, temps, etiquettes))""")

    def switch_database(self, _nom):
        pass

    def write_points(self, points, **_):
        lignes = [(p["measurement"], str(p["time"]), json.dumps(p.get("tags") or {}, sort_keys=True), json.dumps(p.get("fields") or {}))
                  for p in points if p["measurement"] != "DemoPoint"]  # son essai de connexion n'a pas à rester dans la base
        with self.base:
            self.base.executemany("INSERT OR REPLACE INTO points VALUES (?, ?, ?, ?)", lignes)
        return True

    def delete_series(self, measurement=None, tags=None, **_):
        with self.base:
            for ligne in self.base.execute("SELECT rowid, etiquettes FROM points WHERE mesure = ?", (measurement,)).fetchall():
                if all(json.loads(ligne[1]).get(c) == v for c, v in (tags or {}).items()):
                    self.base.execute("DELETE FROM points WHERE rowid = ?", (ligne[0],))

    def query(self, *_a, **_k):
        return []


InfluxDBClient = StockageLocal
