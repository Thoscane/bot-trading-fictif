"""Moteur : un « cycle » = une bougie clôturée => au plus une décision.

Enchaînement d'un cycle :
 1. collecte + contrôle des données (marche.py)
 2. évaluation du portefeuille, règle de mort / BLOQUÉ (portefeuille.py)
 3. si une nouvelle bougie est close et non encore traitée : décision (strategie.py)
 4. vérification de la décision, exécution simulée, écriture atomique (stockage.py)
 5. suivi de la référence « acheter puis conserver »
"""
from __future__ import annotations

import logging
import time

from . import portefeuille as pf
from .marche import Collecteur
from .portefeuille import Instrument, MORT, BLOQUE
from .sources import ErreurSource, SourceKraken, SourceTwelveData
from .stockage import PortefeuilleMort, Stockage
from .strategie import Decision, DecideurIA, StrategieRegles, empreinte_strategie, valider_decision

log = logging.getLogger("bot")


class Moteur:
    def __init__(self, cfg: dict, horloge=time.time, kraken=None, twelve=None, decideur_ia=None,
                 chemin_base: str | None = None):
        self.cfg = cfg
        self.horloge = horloge
        self.P = cfg["intervalle_minutes"] * 60
        self.instruments: dict[str, Instrument] = {
            i["id"]: Instrument.depuis_config(i) for i in cfg["instruments"] if i.get("actif", True)}
        self.stock = Stockage(chemin_base or cfg["base_de_donnees"], cfg["capital_initial_eur"], horloge)
        for a in self.stock.avertissements:
            log.warning(a)
        self.kraken = kraken if kraken is not None else SourceKraken()
        self.twelve = twelve
        self.avertissements_demarrage: list[str] = []
        if self.twelve is None and any(i.source == "twelvedata" for i in self.instruments.values()):
            cle = _lire_cle(cfg["twelvedata"]["fichier_cle"])
            if cle:
                self.twelve = SourceTwelveData(cle, cfg["twelvedata"]["credits_max_par_jour"], compteur=self.stock,
                                               horloge=horloge)
            else:
                msg = ("actions/ETF désactivés : aucune clé Twelve Data (fichier "
                       f"{cfg['twelvedata']['fichier_cle']} absent). Seules les cryptos sont suivies.")
                self.avertissements_demarrage.append(msg)
                self.stock.journaliser_incident("configuration", msg)
                for i in list(self.instruments.values()):
                    if i.source == "twelvedata":
                        i.actif = False
        self.collecteur = Collecteur(cfg, self.instruments, self.kraken, self.twelve, self._incident,
                                     persistance=self.stock)
        if decideur_ia is not None:
            self.decideur = DecideurIA(decideur_ia["appeler"], decideur_ia.get("nom", "modèle"))
        else:
            self.decideur = StrategieRegles(cfg["strategie"])
        self.empreinte = empreinte_strategie(cfg)
        if self.stock.enregistrer_strategie(self.empreinte, f"{self.decideur.nom} — {cfg['strategie']}"):
            log.info("Changement de stratégie enregistré (%s)", self.empreinte)
        self._verifie_paires = False

    # ------------------------------------------------------------- utilitaires
    def _incident(self, type_: str, message: str) -> None:
        if self.stock.journaliser_incident(type_, message):
            log.warning("[%s] %s", type_, message)

    def verifier_disponibilite(self) -> list[str]:
        """Vérifie auprès de Kraken que les paires existent et sont en ligne ; complète les minimums."""
        rapport = []
        for ins in self.instruments.values():
            if ins.source != "kraken" or not ins.actif:
                continue
            try:
                info = self.kraken.infos_paire(ins.symbole_source)
                ins.cle_source = info["cle"]
                if info["statut"] != "online":
                    ins.actif = False
                    rapport.append(f"{ins.id} : statut Kraken « {info['statut']} » — désactivé")
                    continue
                ins.ordre_min_quantite = max(ins.ordre_min_quantite, info["ordre_min_quantite"])
                ins.ordre_min_eur = max(ins.ordre_min_eur, info["ordre_min_eur"])
                ins.decimales_quantite = min(ins.decimales_quantite, info["decimales_quantite"])
                rapport.append(f"{ins.id} : disponible sur Kraken ({info['nom_ws']}), minimum "
                               f"{info['ordre_min_quantite']} / {info['ordre_min_eur']} €")
            except ErreurSource as e:
                ins.actif = False
                rapport.append(f"{ins.id} : vérification impossible ({e}) — désactivé")
        for ins in self.instruments.values():
            if ins.source == "twelvedata" and ins.actif:
                rapport.append(f"{ins.id} : {ins.classe} en {ins.devise} via Twelve Data (converti en EUR, "
                               f"fourchette estimée ±{ins.fourchette_estimee_pct / 2:.3f} %)")
        self._verifie_paires = True
        return rapport

    # ---------------------------------------------------------------- cycle
    def cycle(self) -> dict:
        now = self.horloge()
        if not self._verifie_paires:
            for ligne in self.verifier_disponibilite():
                log.info(ligne)
        port = self.stock.charger_portefeuille()
        mort = self.stock.est_mort()
        cot, bg = self.collecteur.collecter(now)
        self.stock.enregistrer_cotations(cot.values(), now)
        ev = pf.evaluer(port, cot, self.instruments, mort)
        self.stock.enregistrer_valorisation(now, ev)
        self._suivre_references(now, cot)

        if ev.statut == MORT:
            if not mort:
                self.stock.declarer_mort(ev.motif, ev.valeur)
                log.error("PORTEFEUILLE MORT : %s", ev.motif)
            return {"statut": MORT, "evaluation": ev, "decision": None}
        if ev.statut == BLOQUE:
            self._incident("bloque", ev.motif)
        cycle_ts = (int(now) // self.P) * self.P - self.P      # ouverture de la dernière bougie close
        if now - (cycle_ts + self.P) < self.cfg["delai_apres_cloture_secondes"]:
            # La bougie vient de se clôturer : on la laisse se stabiliser et on décide sur la PRÉCÉDENTE
            # (en ignorant la toute dernière), au lieu de ne rien décider. Aucune donnée future n'est utilisée.
            cycle_ts -= self.P
            bg = {k: [b for b in v if b[0] <= cycle_ts] for k, v in bg.items()}
        if self.stock.decision_existe(cycle_ts):
            return {"statut": ev.statut, "evaluation": ev, "decision": None}
        negociables = {k: v for k, v in cot.items() if v.negociable and bg.get(k) and bg[k][-1][0] >= cycle_ts - self.P}
        if not negociables or ev.statut == BLOQUE:
            return {"statut": ev.statut, "evaluation": ev, "decision": None}

        dernier = self.stock.dernier_trade_cycle()
        par_ins = {iid: (cycle_ts - ts) // self.P for iid, ts in self.stock.derniers_trades_par_instrument().items()}
        ctx = {"portefeuille": port.copie(), "cotations": cot, "bougies": {k: v for k, v in bg.items()},
               "instruments": self.instruments, "horodatage": now,
               "bougies_depuis_transaction": None if dernier is None else (cycle_ts - dernier) // self.P,
               "bougies_depuis_transaction_par_instrument": par_ins}
        d = self.decideur.decider(ctx)
        motif = valider_decision(d, port, cot, self.instruments, self.cfg["strategie"].get("positions_max"))
        decision = {"cycle_ts": cycle_ts, "horodatage": now, "decideur": self.decideur.nom, "action": d.action,
                    "instrument": d.instrument, "quantite": d.quantite, "justification": d.justification,
                    "valide": int(not motif), "motif_refus": motif}
        tx, apres = None, None
        if not motif and d.action != "attendre":
            tx, apres = self._executer(d, port, cot[d.instrument], cycle_ts, now)
        try:
            ok = self.stock.executer_cycle(decision, tx, apres)
        except PortefeuilleMort:
            return {"statut": MORT, "evaluation": ev, "decision": None}
        if not ok:
            log.info("bougie %s déjà traitée (doublon évité)", cycle_ts)
            return {"statut": ev.statut, "evaluation": ev, "decision": None}
        if motif:
            self._incident("decision_refusee", f"{d.action} {d.instrument} {d.quantite} refusé : {motif}")
        if tx:
            log.info("TRANSACTION SIMULÉE %s %s %.8g @ %.4f € (frais %.4f €) — %s", tx["sens"], tx["instrument"],
                     tx["quantite"], tx["prix_effectif"], tx["frais"], tx["justification"])
            ev = pf.evaluer(apres, cot, self.instruments)
            if ev.statut == MORT:
                self.stock.declarer_mort(ev.motif, ev.valeur)
        return {"statut": ev.statut, "evaluation": ev, "decision": decision, "transaction": tx}

    def _executer(self, d: Decision, port, c, cycle_ts: int, now: float):
        ins = self.instruments[d.instrument]
        q = pf.arrondi_inf(d.quantite, ins.decimales_quantite)
        apres = port.copie()
        if d.action == "acheter":
            a = pf.simuler_achat(ins, c.ask, q)
            apres.cash -= a["total"]
            apres.frais_cumules += a["frais"]
            p = apres.positions.setdefault(d.instrument, {"quantite": 0.0, "cout_total": 0.0})
            p["quantite"] += q
            p["cout_total"] += a["total"]
            tx = {"sens": "ACHAT", "quantite": q, "prix_marche": a["prix_marche"], "prix_effectif": a["prix_effectif"],
                  "montant_brut": a["brut"], "frais": a["frais"], "montant_net": -a["total"], "pnl_realise": None}
        else:
            v = pf.simuler_vente(ins, c.bid, q)
            p = apres.positions[d.instrument]
            part_cout = p["cout_total"] * q / p["quantite"]
            p["quantite"] -= q
            p["cout_total"] -= part_cout
            if p["quantite"] <= 10 ** -ins.decimales_quantite / 2:
                del apres.positions[d.instrument]
            apres.cash += v["net"]
            apres.frais_cumules += v["frais"]
            tx = {"sens": "VENTE", "quantite": q, "prix_marche": v["prix_marche"], "prix_effectif": v["prix_effectif"],
                  "montant_brut": v["brut"], "frais": v["frais"], "montant_net": v["net"],
                  "pnl_realise": v["net"] - part_cout}
        tx.update({"cycle_ts": cycle_ts, "horodatage": now, "instrument": d.instrument, "cash_apres": apres.cash,
                   "justification": d.justification, "decideur": self.decideur.nom, "source": c.source,
                   "heure_donnee": c.heure_donnee})
        return tx, apres

    # ------------------------------------------------------------ référence
    def _suivre_references(self, now: float, cot: dict) -> None:
        """Référence « acheter puis conserver » : même capital, mêmes frais, achat au premier prix
        négociable vu par le bot, puis valorisation en liquidation à chaque cycle."""
        for iid in self.cfg.get("references_achat_conservation", []):
            ins, c = self.instruments.get(iid), cot.get(iid)
            if ins is None or c is None:
                continue
            ref = self.stock.reference(iid)
            if ref is None:
                if not c.negociable:
                    continue
                q = pf.quantite_max_achetable(ins, c.ask, self.cfg["capital_initial_eur"])
                if q <= 0:
                    continue
                a = pf.simuler_achat(ins, c.ask, q)
                self.stock.creer_reference(iid, q, a["prix_effectif"], a["frais"],
                                           self.cfg["capital_initial_eur"] - a["total"])
                ref = self.stock.reference(iid)
            valeur = ref["reste_cash"] + pf.valeur_liquidation(ins, ref["quantite"], c.bid)
            self.stock.enregistrer_valeur_ref(now, iid, valeur)


def _lire_cle(chemin: str) -> str:
    import os
    for candidat in (chemin, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), chemin)):
        if os.path.exists(candidat):
            with open(candidat, encoding="utf-8") as f:
                return f.read().strip()
    return os.environ.get("TWELVEDATA_API_KEY", "")
