#!/usr/bin/env python3
"""Point d'entrée du bot fictif.

  python lancer.py verifier    vérifie les sources de données et la disponibilité des actifs
  python lancer.py cycle       exécute UN cycle (collecte, évaluation, décision éventuelle) puis s'arrête
  python lancer.py demarrer    boucle en continu (Ctrl+C pour arrêter ; reprise sûre au redémarrage)
  python lancer.py backtest    rejoue la stratégie sur l'historique disponible et l'enregistre
  python lancer.py tableau     régénère le tableau de bord HTML
  python lancer.py rapport     affiche le point quotidien (texte) et l'archive dans donnees/rapports/
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

RACINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RACINE)
os.chdir(RACINE)

from bot.backtest import executer_backtest          # noqa: E402
from bot.moteur import Moteur                        # noqa: E402
from bot.rapport import generer_tableau_de_bord, rapport_texte  # noqa: E402
from bot.sources import ErreurSource                 # noqa: E402
from bot.outils import heure_paris                   # noqa: E402


def charger_config() -> dict:
    with open(os.path.join(RACINE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def configurer_journal(cfg: dict) -> None:
    os.makedirs(os.path.dirname(cfg["journal"]), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(cfg["journal"], encoding="utf-8")])


def tableau(m: Moteur, cfg: dict) -> str:
    return generer_tableau_de_bord(m.stock, cfg, m.decideur.nom, cfg["tableau_de_bord"], m.instruments)


def cmd_verifier(m: Moteur, cfg: dict) -> None:
    print("Sources de données :")
    print(f"  - {m.kraken.NOM}")
    try:
        t = m.kraken.heure_serveur()
        print(f"    heure serveur Kraken : {heure_paris(t)} (écart local {time.time() - t:+.1f} s), statut : {m.kraken.statut_systeme()}")
    except ErreurSource as e:
        print(f"    INJOIGNABLE : {e}")
    if m.twelve:
        print(f"  - {m.twelve.NOM} (crédits utilisés aujourd'hui : {m.twelve.credits_utilises()}/{m.twelve.credits_max})")
    for a in m.avertissements_demarrage:
        print("  ! " + a)
    print("Actifs :")
    for ligne in m.verifier_disponibilite():
        print("  - " + ligne)
    print(f"Décideur : {m.decideur.nom} (empreinte de stratégie {m.empreinte})")
    e = m.stock.etat()
    print(f"Portefeuille : capital initial {e['capital_initial']} €, espèces {e['cash']:.4f} €, "
          f"{'MORT' if m.stock.est_mort() else 'vivant'}")


def cmd_cycle(m: Moteur, cfg: dict) -> dict:
    r = m.cycle()
    ev = r["evaluation"]
    logging.info("Statut %s — valeur %s € — espèces %.4f € — %s", r["statut"],
                 f"{ev.valeur:.4f}" if ev.valeur is not None else "inconnue", ev.cash, ev.motif)
    if r.get("decision"):
        d = r["decision"]
        logging.info("Décision bougie %s : %s %s %s — %s%s", heure_paris(d["cycle_ts"], "%H:%M"), d["action"],
                     d["instrument"] or "", d["quantite"] or "", d["justification"],
                     f" [REFUSÉE : {d['motif_refus']}]" if d["motif_refus"] else "")
    tableau(m, cfg)
    return r


def cmd_demarrer(m: Moteur, cfg: dict) -> None:
    logging.info("Démarrage de la boucle (interrogation toutes les %s s). Ctrl+C pour arrêter.",
                 cfg["frequence_interrogation_secondes"])
    while True:
        try:
            r = cmd_cycle(m, cfg)
            if r["statut"] == "MORT":
                logging.error("Portefeuille MORT : arrêt de la boucle. Tableau de bord consultable : %s", cfg["tableau_de_bord"])
                return
        except KeyboardInterrupt:
            logging.info("Arrêt demandé. L'état est sauvegardé ; relancez pour reprendre.")
            return
        except Exception:  # on journalise et on continue : jamais de cours inventé, jamais de crash silencieux
            logging.exception("Erreur inattendue pendant le cycle — transactions suspendues ce cycle")
        try:
            time.sleep(cfg["frequence_interrogation_secondes"])
        except KeyboardInterrupt:
            logging.info("Arrêt demandé.")
            return


def cmd_backtest(m: Moteur, cfg: dict) -> None:
    m.verifier_disponibilite()
    now = time.time()
    _, bougies = m.collecteur.collecter(now)
    bougies = {k: v for k, v in bougies.items() if v}
    if not bougies:
        print("Aucune bougie disponible pour le backtest.")
        return
    res = executer_backtest(cfg, {k: v for k, v in m.instruments.items() if k in bougies}, bougies)
    m.stock.enregistrer_backtest(m.empreinte, {k: v for k, v in res.items() if k not in ("courbe", "courbes_ref", "transactions")})
    print(f"Backtest sur {res['nb_bougies']} bougies ({heure_paris(res['debut'])} → {heure_paris(res['fin'])}) — "
          f"instruments : {', '.join(bougies)}")
    print(f"  Bot ({m.decideur.nom}) : {res['valeur_finale']:.4f} € ({res['performance_pct']:+.2f} %), "
          f"{res['nb_transactions']} transactions, frais {res['frais_cumules']:.4f} €, baisse max {res['baisse_max_pct']:.2f} %")
    for k, v in res["references"].items():
        print(f"  Acheter-conserver {k} : {v['valeur_finale']:.4f} € ({v['performance_pct']:+.2f} %)")
    for t in res["transactions"][:20]:
        print(f"    {heure_paris(t['ts'], '%d/%m %H:%M')} {t['sens']} {t['instrument']} {t['quantite']:.8g} @ {t['prix']:.4f} € "
              f"frais {t['frais']:.4f} €" + (f" P&L {t['pnl']:+.4f} €" if 'pnl' in t else ""))
    print("  Le passé ne préjuge pas de l'avenir : aucune rentabilité n'est promise.")
    tableau(m, cfg)


def cmd_rapport(m: Moteur, cfg: dict) -> None:
    txt = rapport_texte(m.stock, cfg, m.decideur.nom, m.instruments)
    os.makedirs(cfg["dossier_rapports"], exist_ok=True)
    chemin = os.path.join(cfg["dossier_rapports"], time.strftime("rapport_%Y-%m-%d_%H%M.txt"))
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    print(f"\n(archivé dans {chemin})")
    tableau(m, cfg)


def principal(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "cycle"
    cfg = charger_config()
    configurer_journal(cfg)
    m = Moteur(cfg)
    for a in m.avertissements_demarrage:
        logging.warning(a)
    commandes = {"verifier": cmd_verifier, "cycle": cmd_cycle, "demarrer": cmd_demarrer,
                 "backtest": cmd_backtest, "tableau": lambda m, c: print("Tableau de bord :", tableau(m, c)),
                 "rapport": cmd_rapport}
    if cmd not in commandes:
        print(__doc__)
        return 2
    commandes[cmd](m, cfg)
    m.stock.fermer()
    return 0


if __name__ == "__main__":
    sys.exit(principal(sys.argv))
