"""Tests automatiques (python -m unittest -v) avec des sources de données SIMULÉES :
aucun appel réseau, résultats reproductibles. Vérifie notamment :
 - le calcul des frais / glissement / minimums,
 - l'arrêt définitif à 0 € (y compris après redémarrage et même si la base est supprimée),
 - la distinction BLOQUÉ / MORT,
 - la reprise après interruption sans transaction en double,
 - le refus de traiter sur données périmées ou incohérentes,
 - l'absence de données futures dans le backtest.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import portefeuille as pf           # noqa: E402
from bot.backtest import executer_backtest   # noqa: E402
from bot.marche import verifier_achat_vente, verifier_bougies  # noqa: E402
from bot.moteur import Moteur                # noqa: E402
from bot.sources import ErreurSource         # noqa: E402
from bot.stockage import Stockage            # noqa: E402
from bot.strategie import Decision, StrategieRegles, valider_decision  # noqa: E402

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = 300


def config_test(dossier: str) -> dict:
    with open(os.path.join(RACINE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["instruments"] = [i for i in cfg["instruments"] if i["source"] == "kraken"]
    cfg["references_achat_conservation"] = ["BTC/EUR"]
    cfg["base_de_donnees"] = os.path.join(dossier, "bot.sqlite")
    cfg["delai_apres_cloture_secondes"] = 0
    return cfg


class Horloge:
    def __init__(self, t: float):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def avancer(self, s: float) -> None:
        self.t += s


class FauxKraken:
    """Source Kraken simulée : prix pilotés par le test, bougies 5 min générées à partir d'une série."""
    NOM = "Kraken (simulé pour les tests)"

    def __init__(self, horloge: Horloge, prix: dict[str, float], statut="online", decalage=0.0, ecart=0.0002):
        self.h, self.prix, self.statut, self.decalage, self.ecart = horloge, dict(prix), statut, decalage, ecart
        self.series: dict[str, list[float]] = {}   # symbole -> clôtures (une par bougie, la plus récente en dernier)
        self.bougies_forcees: dict[str, list] = {}

    def heure_serveur(self):
        return self.h() + self.decalage

    def statut_systeme(self):
        return self.statut

    def infos_paire(self, s):
        return {"cle": {"XBTEUR": "XXBTZEUR", "ETHEUR": "XETHZEUR"}[s], "nom_ws": s, "statut": "online",
                "ordre_min_quantite": 0.00005 if s == "XBTEUR" else 0.001, "ordre_min_eur": 0.45, "decimales_quantite": 8}

    def tickers(self, d):
        return {s: {"bid": p * (1 - self.ecart), "ask": p * (1 + self.ecart), "dernier": p}
                for s, p in self.prix.items() if s in d.values()}

    def bougies(self, symbole, cle, intervalle):
        if symbole in self.bougies_forcees:
            return self.bougies_forcees[symbole]
        serie = self.series.get(symbole) or [self.prix[symbole]] * 60
        fin = (int(self.heure_serveur()) // P) * P - P   # ouverture de la dernière bougie CLÔTURÉE
        out = []
        for k, c in enumerate(reversed(serie)):
            ts = fin - k * P
            out.append((ts, c, c * 1.001, c * 0.999, c, 1.0))
        out.append((fin + P, serie[-1], serie[-1], serie[-1], serie[-1], 0.5))  # bougie en cours (à ignorer)
        return sorted(out)


class Base(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.mkdtemp()
        self.cfg = config_test(self.dossier)
        self.h = Horloge(1_760_000_000 - 1_760_000_000 % P + 30)   # 30 s après une clôture

    def tearDown(self):
        shutil.rmtree(self.dossier, ignore_errors=True)

    def moteur(self, kraken):
        return Moteur(self.cfg, horloge=self.h, kraken=kraken, twelve=None)

    def ins(self, m, iid="BTC/EUR"):
        return m.instruments[iid]


class TestFrais(Base):
    def test_achat_prix_vendeur_plus_frais_et_glissement(self):
        ins = pf.Instrument.depuis_config(self.cfg["instruments"][0])  # BTC 0,40 % + glissement 0,05 %
        a = pf.simuler_achat(ins, ask=100_000.0, qte=0.0001)
        self.assertAlmostEqual(a["prix_effectif"], 100_050.0)
        self.assertAlmostEqual(a["brut"], 10.005)
        self.assertAlmostEqual(a["frais"], 10.005 * 0.004)
        self.assertAlmostEqual(a["total"], 10.005 * 1.004)

    def test_vente_prix_acheteur_moins_frais(self):
        ins = pf.Instrument.depuis_config(self.cfg["instruments"][0])
        v = pf.simuler_vente(ins, bid=100_000.0, qte=0.0001)
        self.assertAlmostEqual(v["prix_effectif"], 99_950.0)
        self.assertAlmostEqual(v["net"], 9.995 * (1 - 0.004))

    def test_quantite_max_tient_dans_les_especes_frais_inclus(self):
        ins = pf.Instrument.depuis_config(self.cfg["instruments"][0])
        q = pf.quantite_max_achetable(ins, ask=100_000.0, cash=10.0)
        self.assertLessEqual(pf.simuler_achat(ins, 100_000.0, q)["total"], 10.0)
        pas = 10 ** -ins.decimales_quantite
        self.assertGreater(pf.simuler_achat(ins, 100_000.0, q + pas)["total"], 10.0)

    def test_frais_cumules_apres_aller_retour(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0})
        k.series["XBTEUR"] = [100_000.0] * 59 + [102_000.0]
        k.prix["XBTEUR"] = 102_000.0   # tendance haussière => croisement
        k.prix["XBTEUR"] = 100_000.0
        m = self.moteur(k)
        r = m.cycle()
        self.assertIsNotNone(r.get("transaction"))
        self.assertEqual(r["transaction"]["sens"], "ACHAT")
        e = m.stock.etat()
        self.assertAlmostEqual(e["frais_cumules"], r["transaction"]["frais"])
        self.assertAlmostEqual(e["cash"], 10.0 - r["transaction"]["montant_brut"] - r["transaction"]["frais"], places=9)


class TestValidation(Base):
    def test_refus_levier_et_vente_a_decouvert(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0})
        m = self.moteur(k)
        m.verifier_disponibilite()
        cot, _ = m.collecteur.collecter(self.h())
        port = m.stock.charger_portefeuille()
        self.assertIn("espèces insuffisantes", valider_decision(Decision("acheter", "BTC/EUR", 0.001), port, cot, m.instruments))
        self.assertIn("découvert", valider_decision(Decision("vendre", "BTC/EUR", 0.0001), port, cot, m.instruments))
        self.assertIn("minimums", valider_decision(Decision("acheter", "BTC/EUR", 0.000001), port, cot, m.instruments))
        self.assertIn("inconnue", valider_decision(Decision("emprunter", "BTC/EUR", 1), port, cot, m.instruments))
        self.assertEqual("", valider_decision(Decision("attendre"), port, cot, m.instruments))


class TestMort(Base):
    def _acheter_puis_crash(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0})
        k.series["XBTEUR"] = [100_000.0] * 59 + [102_000.0]
        k.prix["XBTEUR"] = 102_000.0
        m = self.moteur(k)
        self.assertEqual(m.cycle()["transaction"]["sens"], "ACHAT")
        # On simule un achat ayant consommé EXACTEMENT toutes les espèces (reste 0,0000 €)
        m.stock.cx.execute("UPDATE etat SET cash = 0 WHERE id = 1")
        m.stock.fermer()
        # Effondrement : la position vaut moins que le minimum de vente => liquidation = 0 ; valeur = 0 => MORT
        k.prix["XBTEUR"] = 1_000.0
        k.series["XBTEUR"] = [1_000.0] * 60
        self.h.avancer(P)
        return k

    def test_mort_definitive_et_bloquee_apres_redemarrage(self):
        k = self._acheter_puis_crash()
        m = self.moteur(k)
        r = m.cycle()
        self.assertEqual(r["statut"], pf.MORT)
        self.assertTrue(m.stock.est_mort())
        self.assertTrue(os.path.exists(os.path.join(self.dossier, "MORT.txt")))
        m.stock.fermer()
        # Redémarrage : toujours mort, aucune décision possible même si le prix remonte
        k.prix["XBTEUR"] = 200_000.0
        self.h.avancer(P)
        m2 = self.moteur(k)
        r2 = m2.cycle()
        self.assertEqual(r2["statut"], pf.MORT)
        self.assertIsNone(r2["decision"])
        self.assertEqual(m2.stock.cx.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"], 1)
        # Les garde-fous SQL refusent toute écriture
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            m2.stock.cx.execute("UPDATE etat SET cash = 10 WHERE id = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            m2.stock.cx.execute("DELETE FROM transactions")
        # L'historique reste consultable
        self.assertEqual(m2.stock.cx.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"], 1)
        m2.stock.fermer()

    def test_mort_survit_a_la_suppression_de_la_base(self):
        k = self._acheter_puis_crash()
        m = self.moteur(k)
        m.cycle()
        m.stock.fermer()
        for f in os.listdir(self.dossier):
            if f.startswith("bot.sqlite"):
                os.remove(os.path.join(self.dossier, f))
        m2 = self.moteur(k)
        self.assertTrue(m2.stock.est_mort())
        self.assertEqual(m2.cycle()["statut"], pf.MORT)

    def test_apport_interdit(self):
        import sqlite3
        s = Stockage(os.path.join(self.dossier, "x.sqlite"), 10.0, self.h)
        with self.assertRaises(sqlite3.IntegrityError):
            s.cx.execute("UPDATE etat SET capital_initial = 100 WHERE id = 1")
        s.fermer()


class TestBloque(Base):
    def test_bloque_nest_pas_mort(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0})
        m = self.moteur(k)
        # On force manuellement des espèces trop faibles pour le plus petit achat (0,45 €) sans position
        m.stock.cx.execute("UPDATE etat SET cash = 0.30 WHERE id = 1")
        r = m.cycle()
        self.assertEqual(r["statut"], pf.BLOQUE)
        self.assertFalse(m.stock.est_mort())
        self.assertIsNone(r["decision"])
        self.assertGreater(r["evaluation"].valeur, 0)


class TestReprise(Base):
    def test_pas_de_transaction_en_double_apres_redemarrage(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0})
        k.series["XBTEUR"] = [100_000.0] * 59 + [102_000.0]
        k.prix["XBTEUR"] = 102_000.0
        m = self.moteur(k)
        self.assertEqual(m.cycle()["transaction"]["sens"], "ACHAT")
        m.stock.fermer()
        # « Coupure » puis redémarrage dans la même bougie : la décision existe déjà => rien
        self.h.avancer(40)
        m2 = self.moteur(k)
        r = m2.cycle()
        self.assertIsNone(r["decision"])
        self.assertEqual(m2.stock.cx.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"], 1)
        port = m2.stock.charger_portefeuille()
        self.assertIn("BTC/EUR", port.positions)
        m2.stock.fermer()

    def test_ecriture_atomique_decision_unique(self):
        s = Stockage(os.path.join(self.dossier, "y.sqlite"), 10.0, self.h)
        d = {"cycle_ts": 1000, "horodatage": 1.0, "decideur": "t", "action": "attendre", "instrument": None,
             "quantite": None, "justification": "", "valide": 1, "motif_refus": ""}
        self.assertTrue(s.executer_cycle(d, None, None))
        self.assertFalse(s.executer_cycle(d, None, None))   # même bougie => refusé sans erreur
        s.fermer()


class TestDonnees(Base):
    def test_refus_donnees_perimees(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0})
        k.series["XBTEUR"] = [100_000.0] * 59 + [102_000.0]
        k.prix["XBTEUR"] = 102_000.0
        old = [(ts - 3 * 3600, o, hh, l, c, v) for ts, o, hh, l, c, v in k.bougies("XBTEUR", "XXBTZEUR", 5)]
        k.bougies_forcees["XBTEUR"] = old
        k.bougies_forcees["ETHEUR"] = [(ts, o, hh, l, c, v) for ts, o, hh, l, c, v in old]
        m = self.moteur(k)
        r = m.cycle()
        self.assertIsNone(r["decision"])
        self.assertEqual(r["statut"], pf.SUSPENDU)
        inc = [i["message"] for i in m.stock.cx.execute("SELECT message FROM incidents")]
        self.assertTrue(any("ancienne" in x for x in inc), inc)

    def test_refus_prix_incoherent(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0}, ecart=-0.01)  # bid > ask
        m = self.moteur(k)
        r = m.cycle()
        self.assertEqual(r["statut"], pf.SUSPENDU)
        self.assertEqual(len(m.stock.cx.execute("SELECT * FROM cotations").fetchall()), 0)  # prix rejeté, pas stocké

    def test_refus_horloge_decalee_et_statut_kraken(self):
        k = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0}, decalage=500)
        k.series["XBTEUR"] = [100_000.0] * 59 + [102_000.0]
        k.prix["XBTEUR"] = 102_000.0
        m = self.moteur(k)
        self.assertIsNone(m.cycle()["decision"])
        k2 = FauxKraken(self.h, {"XBTEUR": 100_000.0, "ETHEUR": 3_000.0}, statut="maintenance")
        k2.series["XBTEUR"] = k.series["XBTEUR"]
        shutil.rmtree(self.dossier); os.makedirs(self.dossier)
        self.assertIsNone(self.moteur(k2).cycle()["decision"])

    def test_source_injoignable_suspend_sans_crash(self):
        class Mort(FauxKraken):
            def heure_serveur(self):
                raise ErreurSource("réseau coupé")
        m = self.moteur(Mort(self.h, {"XBTEUR": 1.0, "ETHEUR": 1.0}))
        r = m.cycle()
        self.assertEqual(r["statut"], pf.SUSPENDU)
        self.assertEqual(r["evaluation"].valeur_positions, 0.0)  # pas de position ; rien n'est inventé
        self.assertAlmostEqual(r["evaluation"].cash, 10.0)

    def test_controles_unitaires(self):
        self.assertIn("supérieur", verifier_achat_vente(101, 100, 1.0))
        self.assertIn("anormal", verifier_achat_vente(100, 103, 1.0))
        self.assertEqual("", verifier_achat_vente(100, 100.1, 1.0))
        bougies = [(0, 1, 2, 0.5, 1.5, 1), (300, 1.5, 1.4, 1.3, 1.45, 1)]  # haut < clôture => incohérent
        self.assertIn("incohérente", verifier_bougies(bougies, 700, 300, 600))


class TestBacktest(Base):
    def test_pas_de_donnees_futures(self):
        """Le résultat jusqu'à l'instant T ne change pas si l'on ajoute des bougies après T."""
        ins = {"BTC/EUR": pf.Instrument.depuis_config(self.cfg["instruments"][0])}
        serie = [100 + (i % 17) - (i % 5) * 2 + i * 0.2 for i in range(150)]
        base = [(1_000_000 + k * P, c, c * 1.001, c * 0.999, c, 1.0) for k, c in enumerate(serie)]
        r1 = executer_backtest(self.cfg, ins, {"BTC/EUR": base[:100]})
        r2 = executer_backtest(self.cfg, ins, {"BTC/EUR": base})
        self.assertEqual(r1["transactions"], [t for t in r2["transactions"] if t["ts"] <= base[99][0]])
        self.assertEqual(r1["courbe"], r2["courbe"][:len(r1["courbe"])])

    def test_reference_memes_frais(self):
        ins = {"BTC/EUR": pf.Instrument.depuis_config(self.cfg["instruments"][0])}
        base = [(1_000_000 + k * P, 100.0, 100.1, 99.9, 100.0, 1.0) for k in range(40)]
        r = executer_backtest(self.cfg, ins, {"BTC/EUR": base})
        ref = r["references"]["BTC/EUR"]["valeur_finale"]
        self.assertLess(ref, 10.0)          # prix stable => la référence perd exactement les frais + glissement + fourchette
        self.assertGreater(ref, 9.8)
        self.assertEqual(r["nb_transactions"], 0)  # pas de croisement sur un prix plat


class TestStrategie(Base):
    def test_signal_croisement_pas_simple_etat(self):
        s = StrategieRegles(self.cfg["strategie"])
        ins = {"BTC/EUR": pf.Instrument.depuis_config(self.cfg["instruments"][0])}
        from bot.marche import Cotation
        cot = {"BTC/EUR": Cotation("BTC/EUR", 99.9, 100.1, "t", 0, "e", True)}
        hausse = [100.0] * 59 + [102.0]
        bg = {"BTC/EUR": [(k * P, c, c, c, c, 1) for k, c in enumerate(hausse)]}
        port = pf.Portefeuille(10, 10)
        ctx = {"portefeuille": port, "cotations": cot, "bougies": bg, "instruments": ins, "bougies_depuis_transaction": None}
        self.assertEqual(s.decider(ctx).action, "acheter")
        # Tendance déjà installée depuis longtemps (écart constant) : pas de nouveau croisement => attendre
        plat_haut = [100.0 + i for i in range(60)]
        ctx["bougies"] = {"BTC/EUR": [(k * P, c, c, c, c, 1) for k, c in enumerate(plat_haut)]}
        self.assertEqual(s.decider(ctx).action, "attendre")


if __name__ == "__main__":
    unittest.main(verbosity=2)
