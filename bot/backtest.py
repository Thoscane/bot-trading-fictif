"""Backtest sur historique : rejoue la stratégie bougie par bougie, SANS données futures.

À chaque pas t, la stratégie ne voit que les bougies [0..t] (déjà closes) ; la
cotation utilisée est la clôture de la bougie t ± demi-fourchette estimée.
Mêmes frais, glissement et minimums que la simulation prospective. Le résultat
est comparé à la référence « acheter puis conserver » calculée au même coût.
"""
from __future__ import annotations

from . import portefeuille as pf
from .marche import Cotation
from .strategie import StrategieRegles, valider_decision


def executer_backtest(cfg: dict, instruments: dict, bougies: dict, decideur=None) -> dict:
    decideur = decideur or StrategieRegles(cfg["strategie"])
    P = cfg["intervalle_minutes"] * 60
    pas = sorted({b[0] for v in bougies.values() for b in v})
    index = {iid: {b[0]: k for k, b in enumerate(v)} for iid, v in bougies.items()}
    port = pf.Portefeuille(cfg["capital_initial_eur"], cfg["capital_initial_eur"])
    courbe, transactions, dernier_trade, sommet, dd_max = [], [], None, 0.0, 0.0
    derniers_par_ins: dict[str, int] = {}
    refs: dict[str, dict] = {}

    for t in pas:
        cot = {}
        for iid, ins in instruments.items():
            k = index.get(iid, {}).get(t)
            if k is None:
                continue
            px = bougies[iid][k][4]
            demi = ins.fourchette_estimee_pct / 200
            cot[iid] = Cotation(iid, px * (1 - demi), px * (1 + demi), "backtest", t + P, "estimée", True)
        if not cot:
            continue
        visibles = {iid: bougies[iid][: index[iid][t] + 1] for iid in cot}  # rien après t
        for iid in cfg.get("references_achat_conservation", []):
            if iid in cot and iid not in refs:
                ins = instruments[iid]
                q = pf.quantite_max_achetable(ins, cot[iid].ask, cfg["capital_initial_eur"])
                if q > 0:
                    a = pf.simuler_achat(ins, cot[iid].ask, q)
                    refs[iid] = {"quantite": q, "reste": cfg["capital_initial_eur"] - a["total"], "courbe": []}
        for iid, r in refs.items():
            if iid in cot:
                r["courbe"].append((t, r["reste"] + pf.valeur_liquidation(instruments[iid], r["quantite"], cot[iid].bid)))

        ev = pf.evaluer(port, cot, instruments)
        if ev.valeur is not None:
            sommet = max(sommet, ev.valeur)
            dd_max = max(dd_max, (sommet - ev.valeur) / sommet * 100 if sommet else 0)
            courbe.append((t, ev.valeur))
        if ev.statut in (pf.MORT, pf.BLOQUE):
            if ev.statut == pf.MORT:
                break
            continue
        ctx = {"portefeuille": port.copie(), "cotations": cot, "bougies": visibles, "instruments": instruments,
               "horodatage": t + P,
               "bougies_depuis_transaction": None if dernier_trade is None else (t - dernier_trade) // P,
               "bougies_depuis_transaction_par_instrument": {i: (t - ts) // P for i, ts in derniers_par_ins.items()}}
        d = decideur.decider(ctx)
        if d.action == "attendre" or valider_decision(d, port, cot, instruments,
                                                      cfg["strategie"].get("positions_max")):
            continue
        ins, c = instruments[d.instrument], cot[d.instrument]
        q = pf.arrondi_inf(d.quantite, ins.decimales_quantite)
        if d.action == "acheter":
            a = pf.simuler_achat(ins, c.ask, q)
            port.cash -= a["total"]
            port.frais_cumules += a["frais"]
            p = port.positions.setdefault(d.instrument, {"quantite": 0.0, "cout_total": 0.0})
            p["quantite"] += q
            p["cout_total"] += a["total"]
            transactions.append({"ts": t, "sens": "ACHAT", "instrument": d.instrument, "quantite": q,
                                 "prix": a["prix_effectif"], "frais": a["frais"], "justification": d.justification})
        else:
            v = pf.simuler_vente(ins, c.bid, q)
            p = port.positions[d.instrument]
            part = p["cout_total"] * q / p["quantite"]
            p["quantite"] -= q
            p["cout_total"] -= part
            if p["quantite"] <= 10 ** -ins.decimales_quantite / 2:
                del port.positions[d.instrument]
            port.cash += v["net"]
            port.frais_cumules += v["frais"]
            transactions.append({"ts": t, "sens": "VENTE", "instrument": d.instrument, "quantite": q,
                                 "prix": v["prix_effectif"], "frais": v["frais"], "pnl": v["net"] - part,
                                 "justification": d.justification})
        dernier_trade = t
        derniers_par_ins[d.instrument] = t

    finale = courbe[-1][1] if courbe else cfg["capital_initial_eur"]
    ventes = [x for x in transactions if x["sens"] == "VENTE"]
    return {
        "capital_initial": cfg["capital_initial_eur"],
        "valeur_finale": finale,
        "performance_pct": (finale / cfg["capital_initial_eur"] - 1) * 100,
        "frais_cumules": port.frais_cumules,
        "nb_transactions": len(transactions),
        "nb_ventes_gagnantes": sum(1 for v in ventes if v.get("pnl", 0) > 0),
        "nb_ventes_perdantes": sum(1 for v in ventes if v.get("pnl", 0) <= 0),
        "baisse_max_pct": dd_max,
        "debut": pas[0] if pas else None, "fin": pas[-1] if pas else None, "nb_bougies": len(pas),
        "references": {iid: {"valeur_finale": r["courbe"][-1][1] if r["courbe"] else None,
                             "performance_pct": (r["courbe"][-1][1] / cfg["capital_initial_eur"] - 1) * 100
                             if r["courbe"] else None} for iid, r in refs.items()},
        "courbe": courbe, "courbes_ref": {k: v["courbe"] for k, v in refs.items()},
        "transactions": transactions,
    }
