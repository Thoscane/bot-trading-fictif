"""Simulation réaliste : frais, glissement de prix, minimums, valeur de liquidation, statut.

Conventions (toutes configurables par instrument dans config.json) :
- Achat simulé au prix VENDEUR (ask) majoré du glissement.
- Vente simulée au prix ACHETEUR (bid) minoré du glissement.
- Frais = montant × frais_pct + frais_fixes_eur, à l'achat comme à la vente.
- Valeur de liquidation d'une position = produit net d'une vente immédiate
  (bid, glissement, frais déduits). Une position trop petite pour respecter les
  minimums de vente ne peut pas être vendue : sa valeur de liquidation est 0.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .outils import arrondi_inf, arrondi_sup

ACTIF, EN_ATTENTE, SUSPENDU, BLOQUE, MORT = "ACTIF", "EN ATTENTE", "SUSPENDU", "BLOQUÉ", "MORT"


@dataclass
class Instrument:
    id: str
    nom: str
    classe: str
    source: str
    symbole_source: str
    devise: str
    frais_pct: float
    frais_fixes_eur: float
    glissement_pct: float
    fourchette_estimee_pct: float
    ordre_min_quantite: float
    ordre_min_eur: float
    decimales_quantite: int
    actif: bool = True
    cle_source: str = ""

    @classmethod
    def depuis_config(cls, d: dict) -> "Instrument":
        champs = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**champs)


@dataclass
class Portefeuille:
    capital_initial: float
    cash: float
    frais_cumules: float = 0.0
    positions: dict = field(default_factory=dict)  # id -> {"quantite", "cout_total"}

    def copie(self) -> "Portefeuille":
        return copy.deepcopy(self)


# --- Calculs d'exécution simulée ---------------------------------------------

def prix_achat_effectif(ins: Instrument, ask: float) -> float:
    return ask * (1 + ins.glissement_pct / 100)


def prix_vente_effectif(ins: Instrument, bid: float) -> float:
    return bid * (1 - ins.glissement_pct / 100)


def simuler_achat(ins: Instrument, ask: float, qte: float) -> dict:
    pe = prix_achat_effectif(ins, ask)
    brut = qte * pe
    frais = brut * ins.frais_pct / 100 + ins.frais_fixes_eur
    return {"prix_marche": ask, "prix_effectif": pe, "brut": brut, "frais": frais, "total": brut + frais}


def simuler_vente(ins: Instrument, bid: float, qte: float) -> dict:
    pe = prix_vente_effectif(ins, bid)
    brut = qte * pe
    frais = brut * ins.frais_pct / 100 + ins.frais_fixes_eur
    return {"prix_marche": bid, "prix_effectif": pe, "brut": brut, "frais": frais, "net": brut - frais}


def respecte_minimums(ins: Instrument, qte: float, montant_brut: float) -> bool:
    return qte > 0 and qte >= ins.ordre_min_quantite - 1e-15 and montant_brut >= ins.ordre_min_eur - 1e-12


def quantite_max_achetable(ins: Instrument, ask: float, cash: float) -> float:
    """Plus grande quantité dont le coût TOTAL (prix effectif + frais) tient dans les espèces."""
    pe = prix_achat_effectif(ins, ask)
    disponible = cash - ins.frais_fixes_eur
    if disponible <= 0 or pe <= 0:
        return 0.0
    q = arrondi_inf(disponible / (pe * (1 + ins.frais_pct / 100)), ins.decimales_quantite)
    pas = 10 ** -ins.decimales_quantite
    while q > 0 and simuler_achat(ins, ask, q)["total"] > cash + 1e-12:
        q = arrondi_inf(q - pas, ins.decimales_quantite)
    return q


def cout_achat_minimum(ins: Instrument, ask: float | None) -> float:
    """Coût total du plus petit achat autorisé. Sans prix connu : borne basse (minimum en € + frais)."""
    if ask is None or ask <= 0:
        return ins.ordre_min_eur * (1 + ins.frais_pct / 100) + ins.frais_fixes_eur
    pe = prix_achat_effectif(ins, ask)
    q = max(ins.ordre_min_quantite, ins.ordre_min_eur / pe)
    q = arrondi_sup(q, ins.decimales_quantite)
    return simuler_achat(ins, ask, q)["total"]


def valeur_liquidation(ins: Instrument, qte: float, bid: float) -> float:
    v = simuler_vente(ins, bid, qte)
    if respecte_minimums(ins, qte, v["brut"]) and v["net"] > 0:
        return v["net"]
    return 0.0


# --- Évaluation du portefeuille et statut ------------------------------------

@dataclass
class Evaluation:
    valeur: float | None          # None = impossible à calculer sans inventer de cours
    cash: float
    valeur_positions: float | None
    details: dict
    statut: str
    motif: str


def evaluer(port: Portefeuille, cotations: dict, instruments: dict, mort: bool = False) -> Evaluation:
    details, vp, complet = {}, 0.0, True
    for iid, pos in port.positions.items():
        ins, c = instruments.get(iid), cotations.get(iid)
        if ins is None or c is None:
            complet = False
            details[iid] = {**pos, "valeur_liquidation": None, "bid": None, "latente": None}
            continue
        vl = valeur_liquidation(ins, pos["quantite"], c.bid)
        vp += vl
        details[iid] = {**pos, "valeur_liquidation": vl, "bid": c.bid, "latente": vl - pos["cout_total"]}
    valeur = port.cash + vp if complet else None
    vp_aff = vp if complet else None

    if mort:
        return Evaluation(valeur, port.cash, vp_aff, details, MORT, "portefeuille déclaré mort (définitif)")
    if valeur is not None and valeur <= 0:
        return Evaluation(valeur, port.cash, vp_aff, details, MORT, "valeur nette de liquidation ≤ 0 €")

    actifs = [i for i in instruments.values() if i.actif]
    if not actifs:
        return Evaluation(valeur, port.cash, vp_aff, details, SUSPENDU,
                          "aucun instrument actif (source de cours indisponible ou clé API absente)")
    # Une position est « revendable un jour » si, au dernier prix connu, elle passe les minimums.
    position_revendable = any(d["valeur_liquidation"] is None or d["valeur_liquidation"] > 0 for d in details.values())
    achat_possible = any(
        port.cash + 1e-12 >= cout_achat_minimum(i, cotations[i.id].ask if i.id in cotations else None)
        for i in actifs)
    if not position_revendable and not achat_possible:
        mini = min((cout_achat_minimum(i, cotations[i.id].ask if i.id in cotations else None) for i in actifs),
                   default=0.0)
        return Evaluation(valeur, port.cash, vp_aff, details, BLOQUE,
                          f"espèces ({port.cash:.4f} €) sous le plus petit achat possible (≈ {mini:.2f} € frais "
                          f"inclus) et aucune position revendable — ce n'est PAS une faillite")

    negociables = [c for c in cotations.values() if c.negociable and c.instrument in {i.id for i in actifs}]
    if negociables:
        return Evaluation(valeur, port.cash, vp_aff, details, ACTIF, "au moins un marché négociable")
    motifs = [c.motif for c in cotations.values() if c.motif]
    if motifs and all("fermé" in m for m in motifs) and len(cotations) == len(actifs):
        return Evaluation(valeur, port.cash, vp_aff, details, EN_ATTENTE, "marchés fermés")
    return Evaluation(valeur, port.cash, vp_aff, details, SUSPENDU,
                      "transactions suspendues : données manquantes, anciennes ou incohérentes")
