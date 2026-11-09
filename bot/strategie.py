"""Décideurs : « stratégie à règles » (par défaut) et interface pour une IA interchangeable.

Contrat commun : decider(contexte) -> Decision(action, instrument, quantite, justification).
Le contexte ne contient QUE ce qui est connu à l'instant de décision (bougies
clôturées, cotation courante, portefeuille). Aucune donnée future.

Le programme VÉRIFIE ensuite chaque décision (valider_decision) avant exécution
simulée : espèces suffisantes, minimums, quantité détenue, marché négociable,
pas de levier, pas de vente à découvert.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .portefeuille import (Instrument, Portefeuille, cout_achat_minimum, quantite_max_achetable,
                           respecte_minimums, simuler_achat, simuler_vente)
from .outils import arrondi_inf

ACTIONS = ("acheter", "vendre", "attendre")


@dataclass
class Decision:
    action: str
    instrument: str | None = None
    quantite: float | None = None
    justification: str = ""

    def dict(self) -> dict:
        return {"action": self.action, "instrument": self.instrument, "quantite": self.quantite,
                "justification": self.justification}


def moyenne(xs):
    return sum(xs) / len(xs) if xs else None


def empreinte_strategie(cfg: dict) -> str:
    base = {"decideur": cfg["decideur"], "strategie": cfg["strategie"],
            "instruments": sorted(i["id"] for i in cfg["instruments"] if i.get("actif", True)),
            "frais": {i["id"]: (i["frais_pct"], i["frais_fixes_eur"], i["glissement_pct"]) for i in cfg["instruments"]}}
    return hashlib.sha256(json.dumps(base, sort_keys=True).encode()).hexdigest()[:12]


class StrategieRegles:
    """Croisement de moyennes mobiles sur bougies 5 min clôturées, portefeuille DIVERSIFIÉ.

    - Jusqu'à `positions_max` lignes détenues en même temps (cryptos, actions, ETF mélangés).
    - ACHETER un actif non détenu : moyenne courte > moyenne longue d'au moins `ecart_minimal_pct`
      et la bougie précédente ne remplissait PAS cette condition (vrai croisement). Si plusieurs
      actifs croisent, on prend l'écart le plus fort. Montant : 1/positions_max de la valeur totale
      du portefeuille, plafonné aux espèces disponibles (pas de levier).
    - VENDRE une ligne : moyenne courte < moyenne longue OU perte > stop_protection_pct par rapport
      au coût d'achat (frais inclus). Les stops passent en premier.
    - Une seule action par bougie (une décision unique par bougie, garde-fou anti-doublon) : les
      achats suivants se font aux bougies suivantes.
    - Repos de `bougies_attente_apres_transaction` bougies PAR ACTIF après une transaction sur cet actif.
    """
    nom = "stratégie à règles"

    def __init__(self, p: dict):
        self.c, self.l = int(p["moyenne_courte"]), int(p["moyenne_longue"])
        self.ecart = float(p["ecart_minimal_pct"])
        self.stop = float(p["stop_protection_pct"])
        self.attente = int(p["bougies_attente_apres_transaction"])
        self.positions_max = max(1, int(p.get("positions_max", 1)))
        self.crypto_max = p.get("positions_crypto_max")   # None = pas de limite
        # "croisement" : n'achète qu'au moment exact où la tendance devient haussière (version 1-2)
        # "tendance"   : achète tout titre en tendance haussière nette, les plus fortes d'abord (version 3.2)
        self.entree = p.get("entree", "croisement")
        self.ecart_sortie = float(p.get("ecart_sortie_pct", 0.0))   # marge avant de revendre (anti-allers-retours)
        self.version = p.get("version", "?")

    def _signal(self, closes: list[float]) -> tuple[float | None, float | None]:
        if len(closes) < self.l + 1:
            return None, None
        mc, ml = moyenne(closes[-self.c:]), moyenne(closes[-self.l:])
        mc_prec, ml_prec = moyenne(closes[-self.c - 1:-1]), moyenne(closes[-self.l - 1:-1])
        return (mc - ml) / ml * 100, (mc_prec - ml_prec) / ml_prec * 100

    def _au_repos(self, ctx: dict, iid: str) -> int | None:
        par_ins = ctx.get("bougies_depuis_transaction_par_instrument")
        if par_ins is None:  # compatibilité : ancien contexte global
            n = ctx.get("bougies_depuis_transaction")
        else:
            n = par_ins.get(iid)
        return n if (n is not None and n < self.attente) else None

    def decider(self, ctx: dict) -> Decision:
        port: Portefeuille = ctx["portefeuille"]
        cot, bg, ins = ctx["cotations"], ctx["bougies"], ctx["instruments"]

        # 1) Ventes (stops d'abord, puis retournements de tendance)
        stops, retournements, conservees = [], [], []
        for iid, pos in port.positions.items():
            if iid not in cot or not cot[iid].negociable:
                conservees.append(f"{iid} (marché non négociable)")
                continue
            v = simuler_vente(ins[iid], cot[iid].bid, pos["quantite"])
            perte = (v["net"] - pos["cout_total"]) / pos["cout_total"] * 100
            if perte <= -self.stop:
                stops.append((perte, iid, pos))
                continue
            if self._au_repos(ctx, iid) is not None:
                conservees.append(f"{iid} (repos après achat, P&L {perte:+.2f} %)")
                continue
            ecart, _ = self._signal([b[4] for b in bg.get(iid, [])])
            if ecart is not None and ecart < -self.ecart_sortie:
                retournements.append((ecart, perte, iid, pos))
            else:
                conservees.append(f"{iid} (tendance haussière, P&L {perte:+.2f} %)")
        if stops:
            perte, iid, pos = min(stops)
            return Decision("vendre", iid, pos["quantite"],
                            f"stop de protection : {perte:.2f} % sous le coût d'achat (seuil −{self.stop} %)")
        if retournements:
            ecart, perte, iid, pos = min(retournements)
            return Decision("vendre", iid, pos["quantite"],
                            f"MM{self.c} repassée sous MM{self.l} (écart {ecart:.2f} %) ; P&L latent {perte:+.2f} %")

        # 2) Achats si une place est libre
        nb = len(port.positions)
        if nb >= self.positions_max:
            return Decision("attendre", justification=f"{nb}/{self.positions_max} positions détenues, aucune "
                            "à vendre — " + " ; ".join(conservees))
        valeur = port.cash + sum(
            (simuler_vente(ins[i], cot[i].bid, p["quantite"])["net"] if i in cot else p["cout_total"])
            for i, p in port.positions.items())
        budget = min(port.cash, valeur / self.positions_max)
        meilleur, meilleur_ecart, detail = None, 0.0, []
        nb_crypto = sum(1 for i in port.positions if i in ins and ins[i].classe == "crypto")
        crypto_pleine = self.crypto_max is not None and nb_crypto >= int(self.crypto_max)
        for iid, c in cot.items():
            if iid in port.positions or not c.negociable or iid not in bg:
                continue
            if crypto_pleine and ins[iid].classe == "crypto":
                detail.append(f"{iid} : déjà {nb_crypto} ligne(s) crypto (maximum {self.crypto_max})")
                continue
            repos = self._au_repos(ctx, iid)
            if repos is not None:
                detail.append(f"{iid} : repos après vente ({repos}/{self.attente})")
                continue
            closes = [b[4] for b in bg[iid]]
            ecart, ecart_prec = self._signal(closes)
            if ecart is None:
                detail.append(f"{iid} : historique insuffisant ({len(closes)} bougies)")
                continue
            detail.append(f"{iid} : écart MM {ecart:+.2f} % (précédent {ecart_prec:+.2f} %)")
            signal_achat = ecart >= self.ecart and (self.entree == "tendance" or ecart_prec < self.ecart)
            if signal_achat and ecart > meilleur_ecart:
                if budget + 1e-12 >= cout_achat_minimum(ins[iid], c.ask):
                    meilleur, meilleur_ecart = iid, ecart
                else:
                    detail.append(f"{iid} : budget {budget:.2f} € sous le minimum d'achat")
        prefixe = (f"{nb}/{self.positions_max} positions" + (" (" + " ; ".join(conservees) + ")" if conservees else "")
                   + " — ")
        if meilleur is None:
            return Decision("attendre", justification=prefixe + (("aucune tendance haussière nette — " if self.entree == "tendance"
                                                                   else "aucun croisement haussier confirmé — ")
                            + " ; ".join(detail) if detail else "aucun marché négociable"))
        q = quantite_max_achetable(ins[meilleur], cot[meilleur].ask, budget)
        return Decision("acheter", meilleur, q,
                        f"{'tendance haussière' if self.entree == 'tendance' else 'croisement haussier'} "
                        f"MM{self.c}/MM{self.l} sur {meilleur} (écart {meilleur_ecart:+.2f} %) — "
                        f"ligne {nb + 1}/{self.positions_max}, budget {budget:.2f} € "
                        f"(1/{self.positions_max} de {valeur:.2f} €, plafonné aux espèces)")


class DecideurIA:
    """Branchement d'un modèle externe (OpenAI, Anthropic, modèle local…) : fournir une fonction
    `appeler(prompt: str) -> str` qui renvoie du JSON {"action", "instrument", "quantite", "justification"}.
    Le modèle ne reçoit que le contexte de l'instant ; sa réponse est validée comme toute décision."""
    nom = "IA externe"

    def __init__(self, appeler, nom_modele: str = "modèle"):
        self.appeler = appeler
        self.nom = f"IA : {nom_modele}"

    @staticmethod
    def construire_prompt(ctx: dict) -> str:
        port = ctx["portefeuille"]
        resume = {
            "espèces_eur": round(port.cash, 4),
            "positions": port.positions,
            "cotations": {k: {"acheteur": c.bid, "vendeur": c.ask, "négociable": c.negociable, "motif": c.motif}
                          for k, c in ctx["cotations"].items()},
            "dernières_clôtures": {k: [b[4] for b in v[-30:]] for k, v in ctx["bougies"].items()},
            "frais": {k: {"pct": i.frais_pct, "fixes": i.frais_fixes_eur, "glissement_pct": i.glissement_pct,
                          "min_qte": i.ordre_min_quantite, "min_eur": i.ordre_min_eur} for k, i in ctx["instruments"].items()},
        }
        return ("Tu gères un portefeuille FICTIF. Réponds UNIQUEMENT en JSON : "
                '{"action": "acheter|vendre|attendre", "instrument": "...", "quantite": nombre, "justification": "..."}. '
                "Pas de levier, pas de vente à découvert. Contexte :\n" + json.dumps(resume, ensure_ascii=False))

    def decider(self, ctx: dict) -> Decision:
        try:
            brut = self.appeler(self.construire_prompt(ctx))
            d = json.loads(brut[brut.index("{"): brut.rindex("}") + 1])
            return Decision(str(d.get("action", "")).lower(), d.get("instrument"),
                            float(d["quantite"]) if d.get("quantite") is not None else None,
                            str(d.get("justification", ""))[:500])
        except Exception as e:  # réponse illisible => on attend, et on garde la trace
            return Decision("attendre", justification=f"réponse IA inexploitable ({e}) — attente par sécurité")


def valider_decision(d: Decision, port: Portefeuille, cot: dict, ins: dict, positions_max: int | None = None) -> str:
    """Renvoie '' si la décision est exécutable, sinon le motif de refus."""
    if d.action not in ACTIONS:
        return f"action inconnue « {d.action} »"
    if d.action == "attendre":
        return ""
    if d.instrument not in ins or not ins[d.instrument].actif:
        return f"instrument inconnu ou inactif « {d.instrument} »"
    c = cot.get(d.instrument)
    if c is None or not c.negociable:
        return f"{d.instrument} non négociable ({c.motif if c else 'aucune cotation'})"
    i: Instrument = ins[d.instrument]
    if d.quantite is None or d.quantite <= 0:
        return "quantité absente ou nulle"
    q = arrondi_inf(d.quantite, i.decimales_quantite)
    if d.action == "acheter":
        if positions_max is not None and d.instrument not in port.positions and len(port.positions) >= positions_max:
            return f"nombre maximal de positions atteint ({len(port.positions)}/{positions_max})"
        a = simuler_achat(i, c.ask, q)
        if not respecte_minimums(i, q, a["brut"]):
            return (f"sous les minimums ({q} < {i.ordre_min_quantite} ou {a['brut']:.2f} € < {i.ordre_min_eur} €) ; "
                    f"plus petit achat possible ≈ {cout_achat_minimum(i, c.ask):.2f} € frais inclus")
        if a["total"] > port.cash + 1e-9:
            return f"espèces insuffisantes : {a['total']:.4f} € nécessaires (frais inclus), {port.cash:.4f} € disponibles — pas de levier"
        return ""
    pos = port.positions.get(d.instrument)
    if pos is None:
        return "aucune position à vendre — vente à découvert interdite"
    if q > pos["quantite"] + 1e-12:
        return f"quantité {q} supérieure à la position {pos['quantite']} — vente à découvert interdite"
    v = simuler_vente(i, c.bid, q)
    if not respecte_minimums(i, q, v["brut"]):
        return f"vente sous les minimums ({v['brut']:.2f} € < {i.ordre_min_eur} €)"
    if v["net"] <= 0:
        return "produit net de la vente nul ou négatif"
    return ""
