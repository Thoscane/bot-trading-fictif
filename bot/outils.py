"""Petits outils communs : heures (Paris / New York), arrondis, mise en forme française.

Aucune bibliothèque externe : les changements d'heure sont calculés à la main
(règles UE et États-Unis), pour fonctionner aussi sous Windows sans paquet "tzdata".
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, ROUND_DOWN, ROUND_UP

UTC = _dt.timezone.utc


def _dernier_dimanche(annee: int, mois: int) -> _dt.date:
    if mois == 12:
        d = _dt.date(annee, 12, 31)
    else:
        d = _dt.date(annee, mois + 1, 1) - _dt.timedelta(days=1)
    return d - _dt.timedelta(days=(d.weekday() + 1) % 7)


def _nieme_dimanche(annee: int, mois: int, n: int) -> _dt.date:
    d = _dt.date(annee, mois, 1)
    premier = d + _dt.timedelta(days=(6 - d.weekday()) % 7)
    return premier + _dt.timedelta(weeks=n - 1)


def decalage_paris(ts: float) -> int:
    """Décalage horaire de Paris par rapport à UTC (+1 l'hiver, +2 l'été)."""
    u = _dt.datetime.fromtimestamp(ts, UTC)
    debut = _dt.datetime.combine(_dernier_dimanche(u.year, 3), _dt.time(1, 0), UTC)
    fin = _dt.datetime.combine(_dernier_dimanche(u.year, 10), _dt.time(1, 0), UTC)
    return 2 if debut <= u < fin else 1


def decalage_new_york(ts: float) -> int:
    """Décalage de New York par rapport à UTC (-4 l'été, -5 l'hiver)."""
    u = _dt.datetime.fromtimestamp(ts, UTC)
    debut = _dt.datetime.combine(_nieme_dimanche(u.year, 3, 2), _dt.time(7, 0), UTC)
    fin = _dt.datetime.combine(_nieme_dimanche(u.year, 11, 1), _dt.time(6, 0), UTC)
    return -4 if debut <= u < fin else -5


def heure_paris(ts: float | None, fmt: str = "%d/%m/%Y %H:%M:%S") -> str:
    if ts is None:
        return "—"
    d = _dt.datetime.fromtimestamp(ts, UTC) + _dt.timedelta(hours=decalage_paris(ts))
    return d.strftime(fmt)


def nyse_ouvert(ts: float) -> bool:
    """Séance régulière NYSE/Nasdaq : lundi-vendredi, 9h30-16h00 heure de New York.
    Les jours fériés américains ne sont PAS gérés : ces jours-là les données
    n'arrivent pas, le contrôle de fraîcheur suspend alors les transactions."""
    local = _dt.datetime.fromtimestamp(ts, UTC) + _dt.timedelta(hours=decalage_new_york(ts))
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def secondes_avant_cloture_nyse(ts: float) -> float:
    """Secondes restantes avant 16h00 (heure de New York) le jour même ; 0 si déjà passé."""
    local = _dt.datetime.fromtimestamp(ts, UTC) + _dt.timedelta(hours=decalage_new_york(ts))
    return max(0.0, (16 * 3600) - (local.hour * 3600 + local.minute * 60 + local.second))


def arrondi_inf(q: float, decimales: int) -> float:
    if q <= 0:
        return 0.0
    pas = Decimal(1).scaleb(-decimales)
    return float(Decimal(repr(q)).quantize(pas, rounding=ROUND_DOWN))


def arrondi_sup(q: float, decimales: int) -> float:
    if q <= 0:
        return 0.0
    pas = Decimal(1).scaleb(-decimales)
    return float(Decimal(repr(q)).quantize(pas, rounding=ROUND_UP))


def eur(x: float | None, decimales: int = 2) -> str:
    if x is None:
        return "—"
    s = f"{x:,.{decimales}f}".replace(",", " ").replace(".", ",")
    return s + " €"


def nombre(x: float | None, decimales: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:,.{decimales}f}".replace(",", " ").replace(".", ",")


def pct(x: float | None, decimales: int = 2, signe: bool = True) -> str:
    if x is None:
        return "—"
    s = f"{x:+.{decimales}f}" if signe else f"{x:.{decimales}f}"
    return s.replace(".", ",") + " %"


def quantite(x: float | None) -> str:
    if x is None:
        return "—"
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")
