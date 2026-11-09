"""Connexion aux API publiques de marché (lecture seule, AUCUN ordre réel).

- Kraken (cryptomonnaies en euros) : https://docs.kraken.com/api/
  Points d'accès publics utilisés : Time, SystemStatus, AssetPairs, Ticker, OHLC.
  Pas de clé, gratuit. Le Ticker donne le vrai prix acheteur (bid) et vendeur (ask).

- Twelve Data (actions et ETF) : https://twelvedata.com/docs
  Point d'accès utilisé : time_series (bougies 5 min) + taux EUR/USD.
  Clé gratuite obligatoire. Offre gratuite : 800 crédits/jour, 8/minute,
  1 crédit par appel. Pas de bid/ask : la fourchette est ESTIMÉE (voir config).

Ce module ne contient aucun appel « privé » : il est techniquement impossible
d'envoyer un ordre avec ce code.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = _dt.timezone.utc
AGENT = "bot-trading-fictif/1.0 (simulation pedagogique, aucun ordre reel)"


class ErreurSource(Exception):
    """Problème de données : réseau, réponse invalide, quota, actif inconnu…"""


def _get_json(url: str, params: dict | None = None, timeout: int = 15) -> dict:
    complet = url + ("?" + urllib.parse.urlencode(params) if params else "")
    lisible = url  # on n'écrit jamais la clé API dans les messages
    req = urllib.request.Request(complet, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ErreurSource(f"erreur HTTP {e.code} sur {lisible}") from None
    except json.JSONDecodeError:
        raise ErreurSource(f"réponse illisible (JSON invalide) sur {lisible}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErreurSource(f"connexion impossible à {lisible} : {e}") from None


class SourceKraken:
    NOM = "Kraken — API publique REST (api.kraken.com)"
    BASE = "https://api.kraken.com/0/public/"

    def _appel(self, methode: str, params: dict | None = None) -> dict:
        d = _get_json(self.BASE + methode, params)
        if d.get("error"):
            raise ErreurSource("Kraken : " + "; ".join(d["error"]))
        if "result" not in d:
            raise ErreurSource("Kraken : réponse sans résultat")
        return d["result"]

    def heure_serveur(self) -> float:
        return float(self._appel("Time")["unixtime"])

    def statut_systeme(self) -> str:
        return str(self._appel("SystemStatus").get("status", "inconnu"))

    def infos_paire(self, symbole: str) -> dict:
        res = self._appel("AssetPairs", {"pair": symbole})
        for cle, v in res.items():
            return {
                "cle": cle,
                "nom_ws": v.get("wsname", symbole),
                "statut": v.get("status", "online"),
                "ordre_min_quantite": float(v.get("ordermin") or 0),
                "ordre_min_eur": float(v.get("costmin") or 0),
                "decimales_quantite": int(v.get("lot_decimals", 8)),
            }
        raise ErreurSource(f"Kraken : paire {symbole} introuvable")

    def tickers(self, symboles_par_cle: dict[str, str]) -> dict[str, dict]:
        """symboles_par_cle : {clé Kraken (ex. XXBTZEUR) -> symbole (ex. XBTEUR)}"""
        res = self._appel("Ticker", {"pair": ",".join(symboles_par_cle.values())})
        sortie = {}
        for cle, v in res.items():
            sym = symboles_par_cle.get(cle, cle)
            sortie[sym] = {"bid": float(v["b"][0]), "ask": float(v["a"][0]), "dernier": float(v["c"][0])}
        return sortie

    def bougies(self, symbole: str, cle: str, intervalle_min: int) -> list[tuple]:
        """Bougies (ouverture_ts, ouverture, haut, bas, clôture, volume), ordre chronologique.
        ATTENTION : la dernière bougie renvoyée par Kraken est en cours de formation ;
        le filtrage des seules bougies clôturées est fait dans marche.py."""
        res = self._appel("OHLC", {"pair": symbole, "interval": intervalle_min})
        lignes = res.get(cle)
        if lignes is None:
            lignes = next((v for k, v in res.items() if k != "last"), [])
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])) for r in lignes]


class SourceTwelveData:
    NOM = "Twelve Data — API REST (api.twelvedata.com)"
    URL = "https://api.twelvedata.com/time_series"

    def __init__(self, cle: str, credits_max_par_jour: int = 700, compteur=None, horloge=time.time):
        self.cle = cle.strip()
        self.credits_max = credits_max_par_jour
        self.compteur = compteur  # objet avec lire(jour) / ecrire(jour, n) — persistance des crédits
        self.horloge = horloge
        self._memoire: dict[str, int] = {}

    def _jour(self) -> str:
        return _dt.datetime.fromtimestamp(self.horloge(), UTC).strftime("%Y-%m-%d")

    def credits_utilises(self) -> int:
        j = self._jour()
        return self.compteur.lire(j) if self.compteur else self._memoire.get(j, 0)

    def _consommer(self, n: int = 1) -> None:
        j = self._jour()
        deja = self.credits_utilises()
        if deja + n > self.credits_max:
            raise ErreurSource(
                f"Twelve Data : plafond de sécurité atteint ({deja}/{self.credits_max} crédits aujourd'hui, UTC)")
        if self.compteur:
            self.compteur.ecrire(j, deja + n)
        else:
            self._memoire[j] = deja + n

    def bougies(self, symbole: str, intervalle_min: int, nombre: int = 300) -> list[tuple]:
        self._consommer(1)
        d = _get_json(self.URL, {"symbol": symbole, "interval": f"{intervalle_min}min",
                                 "outputsize": nombre, "timezone": "UTC", "apikey": self.cle})
        if d.get("status") == "error":
            raise ErreurSource(f"Twelve Data ({d.get('code')}) : {d.get('message')}")
        sortie = []
        for v in d.get("values") or []:
            try:
                ts = int(_dt.datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp())
                sortie.append((ts, float(v["open"]), float(v["high"]), float(v["low"]),
                               float(v["close"]), float(v.get("volume") or 0)))
            except (KeyError, ValueError):
                raise ErreurSource(f"Twelve Data : ligne illisible pour {symbole}") from None
        sortie.sort()
        return sortie
