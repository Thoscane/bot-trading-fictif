"""Sauvegarde durable (SQLite, un seul fichier) + garde-fous inscrits DANS la base.

Garde-fous :
- MORT est irréversible : un déclencheur SQL refuse toute modification de l'état
  une fois mort, et refuse toute nouvelle décision, transaction ou position.
- Un fichier témoin « MORT.txt » est aussi écrit à côté de la base : même si la
  base était supprimée, le bot redémarrerait directement à l'état MORT.
- L'historique des transactions ne peut être ni modifié ni effacé.
- Le capital initial ne peut pas être modifié (aucun apport).
- Une seule décision par bougie (contrainte UNIQUE) : pas de transaction en
  double après une coupure ou si deux copies du bot tournent.
- Décision + transaction + mise à jour du portefeuille sont écrites en une seule
  opération atomique (tout ou rien).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from .portefeuille import Portefeuille

SCHEMA = """
CREATE TABLE IF NOT EXISTS etat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    capital_initial REAL NOT NULL,
    cash REAL NOT NULL CHECK (cash >= 0),
    frais_cumules REAL NOT NULL DEFAULT 0,
    mort INTEGER NOT NULL DEFAULT 0,
    mort_le REAL, mort_raison TEXT,
    cree_le REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    instrument TEXT PRIMARY KEY,
    quantite REAL NOT NULL CHECK (quantite > 0),
    cout_total REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts INTEGER NOT NULL UNIQUE,
    horodatage REAL NOT NULL, decideur TEXT, action TEXT, instrument TEXT,
    quantite REAL, justification TEXT, valide INTEGER, motif_refus TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts INTEGER NOT NULL, horodatage REAL NOT NULL,
    instrument TEXT NOT NULL, sens TEXT NOT NULL, quantite REAL NOT NULL,
    prix_marche REAL, prix_effectif REAL, montant_brut REAL, frais REAL,
    montant_net REAL, cash_apres REAL, pnl_realise REAL,
    justification TEXT, decideur TEXT, source TEXT, heure_donnee REAL,
    UNIQUE (cycle_ts, instrument, sens)
);
CREATE TABLE IF NOT EXISTS valorisations (
    horodatage REAL NOT NULL, valeur REAL, cash REAL, valeur_positions REAL, statut TEXT
);
CREATE TABLE IF NOT EXISTS references_bh (
    instrument TEXT PRIMARY KEY, quantite REAL, prix_effectif REAL, frais REAL,
    reste_cash REAL, cree_le REAL
);
CREATE TABLE IF NOT EXISTS valorisations_ref (horodatage REAL, instrument TEXT, valeur REAL);
CREATE TABLE IF NOT EXISTS cotations (
    instrument TEXT PRIMARY KEY, bid REAL, ask REAL, source TEXT, heure_donnee REAL,
    fourchette TEXT, negociable INTEGER, motif TEXT, recu_le REAL
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, horodatage REAL, type TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS changements_strategie (
    id INTEGER PRIMARY KEY AUTOINCREMENT, horodatage REAL, empreinte TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, horodatage REAL, empreinte TEXT, resultats TEXT
);
CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT);
CREATE INDEX IF NOT EXISTS idx_valo ON valorisations(horodatage);
CREATE INDEX IF NOT EXISTS idx_valo_ref ON valorisations_ref(horodatage);

CREATE TRIGGER IF NOT EXISTS mort_irreversible BEFORE UPDATE ON etat
WHEN OLD.mort = 1 BEGIN SELECT RAISE(ABORT, 'MORT : portefeuille figé définitivement'); END;
CREATE TRIGGER IF NOT EXISTS capital_fige BEFORE UPDATE ON etat
WHEN NEW.capital_initial <> OLD.capital_initial BEGIN SELECT RAISE(ABORT, 'apport ou modification du capital interdit'); END;
CREATE TRIGGER IF NOT EXISTS etat_non_supprimable BEFORE DELETE ON etat
BEGIN SELECT RAISE(ABORT, 'état non supprimable'); END;
CREATE TRIGGER IF NOT EXISTS pas_de_decision_si_mort BEFORE INSERT ON decisions
WHEN (SELECT mort FROM etat WHERE id = 1) = 1 BEGIN SELECT RAISE(ABORT, 'MORT : décision refusée'); END;
CREATE TRIGGER IF NOT EXISTS pas_de_transaction_si_mort BEFORE INSERT ON transactions
WHEN (SELECT mort FROM etat WHERE id = 1) = 1 BEGIN SELECT RAISE(ABORT, 'MORT : transaction refusée'); END;
CREATE TRIGGER IF NOT EXISTS positions_figees_ins BEFORE INSERT ON positions
WHEN (SELECT mort FROM etat WHERE id = 1) = 1 BEGIN SELECT RAISE(ABORT, 'MORT : position figée'); END;
CREATE TRIGGER IF NOT EXISTS positions_figees_maj BEFORE UPDATE ON positions
WHEN (SELECT mort FROM etat WHERE id = 1) = 1 BEGIN SELECT RAISE(ABORT, 'MORT : position figée'); END;
CREATE TRIGGER IF NOT EXISTS positions_figees_sup BEFORE DELETE ON positions
WHEN (SELECT mort FROM etat WHERE id = 1) = 1 BEGIN SELECT RAISE(ABORT, 'MORT : position figée'); END;
CREATE TRIGGER IF NOT EXISTS historique_protege_maj BEFORE UPDATE ON transactions
BEGIN SELECT RAISE(ABORT, 'historique non modifiable'); END;
CREATE TRIGGER IF NOT EXISTS historique_protege_sup BEFORE DELETE ON transactions
BEGIN SELECT RAISE(ABORT, 'historique non supprimable'); END;
CREATE TRIGGER IF NOT EXISTS decisions_protegees_sup BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'historique non supprimable'); END;
"""


class PortefeuilleMort(Exception):
    pass


class Stockage:
    def __init__(self, chemin: str, capital_initial: float, horloge=time.time):
        self.chemin = chemin
        self.horloge = horloge
        dossier = os.path.dirname(os.path.abspath(chemin))
        os.makedirs(dossier, exist_ok=True)
        self.fichier_mort = os.path.join(dossier, "MORT.txt")
        self.cx = sqlite3.connect(chemin, isolation_level=None, timeout=30)
        self.cx.row_factory = sqlite3.Row
        self.cx.execute("PRAGMA journal_mode=WAL")
        self.cx.execute("PRAGMA synchronous=FULL")
        self.cx.executescript(SCHEMA)
        self._derniers_incidents: dict[tuple, float] = {}
        self.avertissements: list[str] = []
        row = self.cx.execute("SELECT * FROM etat WHERE id = 1").fetchone()
        if row is None:
            mort = 1 if os.path.exists(self.fichier_mort) else 0
            self.cx.execute(
                "INSERT INTO etat (id, capital_initial, cash, frais_cumules, mort, mort_le, mort_raison, cree_le)"
                " VALUES (1, ?, ?, 0, ?, ?, ?, ?)",
                (capital_initial, capital_initial, mort,
                 self.horloge() if mort else None,
                 "fichier MORT.txt présent : base recréée mais le bot reste mort" if mort else None,
                 self.horloge()))
            self.journaliser_incident("demarrage", f"Portefeuille créé avec {capital_initial:.2f} € fictifs")
        elif abs(row["capital_initial"] - capital_initial) > 1e-9:
            self.avertissements.append(
                f"config.json indique {capital_initial} € mais le portefeuille existant a démarré avec "
                f"{row['capital_initial']} € : la base fait foi (aucune réinitialisation automatique).")

    # ------------------------------------------------------------------ état
    def est_mort(self) -> bool:
        row = self.cx.execute("SELECT mort FROM etat WHERE id = 1").fetchone()
        return bool(row["mort"]) or os.path.exists(self.fichier_mort)

    def etat(self) -> sqlite3.Row:
        return self.cx.execute("SELECT * FROM etat WHERE id = 1").fetchone()

    def declarer_mort(self, raison: str, valeur: float | None) -> None:
        now = self.horloge()
        self.cx.execute("BEGIN IMMEDIATE")
        try:
            self.cx.execute("UPDATE etat SET mort = 1, mort_le = ?, mort_raison = ? WHERE id = 1 AND mort = 0",
                            (now, raison))
            self.cx.execute("INSERT INTO incidents (horodatage, type, message) VALUES (?, 'MORT', ?)",
                            (now, f"PORTEFEUILLE MORT — {raison} (valeur : {valeur})"))
            self.cx.execute("COMMIT")
        except sqlite3.DatabaseError:
            self.cx.execute("ROLLBACK")
        with open(self.fichier_mort, "w", encoding="utf-8") as f:
            f.write(f"Portefeuille déclaré MORT le {now} ({raison}).\n"
                    "Ce fichier empêche toute reprise d'activité, même si la base est supprimée.\n")

    def charger_portefeuille(self) -> Portefeuille:
        e = self.etat()
        pos = {r["instrument"]: {"quantite": r["quantite"], "cout_total": r["cout_total"]}
               for r in self.cx.execute("SELECT * FROM positions")}
        return Portefeuille(e["capital_initial"], e["cash"], e["frais_cumules"], pos)

    # -------------------------------------------------------------- décisions
    def decision_existe(self, cycle_ts: int) -> bool:
        return self.cx.execute("SELECT 1 FROM decisions WHERE cycle_ts = ?", (cycle_ts,)).fetchone() is not None

    def dernier_trade_cycle(self) -> int | None:
        r = self.cx.execute("SELECT MAX(cycle_ts) m FROM transactions").fetchone()
        return r["m"]

    def derniers_trades_par_instrument(self) -> dict:
        return {r["instrument"]: r["m"] for r in
                self.cx.execute("SELECT instrument, MAX(cycle_ts) m FROM transactions GROUP BY instrument")}

    def executer_cycle(self, decision: dict, tx: dict | None, port_apres: Portefeuille | None) -> bool:
        """Écrit décision + transaction + portefeuille d'un bloc. Renvoie False si la bougie
        avait déjà été traitée (aucune écriture)."""
        try:
            self.cx.execute("BEGIN IMMEDIATE")
            self.cx.execute(
                "INSERT INTO decisions (cycle_ts, horodatage, decideur, action, instrument, quantite,"
                " justification, valide, motif_refus) VALUES (:cycle_ts, :horodatage, :decideur, :action,"
                " :instrument, :quantite, :justification, :valide, :motif_refus)", decision)
            if tx is not None and port_apres is not None:
                self.cx.execute(
                    "INSERT INTO transactions (cycle_ts, horodatage, instrument, sens, quantite, prix_marche,"
                    " prix_effectif, montant_brut, frais, montant_net, cash_apres, pnl_realise, justification,"
                    " decideur, source, heure_donnee) VALUES (:cycle_ts, :horodatage, :instrument, :sens,"
                    " :quantite, :prix_marche, :prix_effectif, :montant_brut, :frais, :montant_net,"
                    " :cash_apres, :pnl_realise, :justification, :decideur, :source, :heure_donnee)", tx)
                self.cx.execute("UPDATE etat SET cash = ?, frais_cumules = ? WHERE id = 1",
                                (round(port_apres.cash, 10), port_apres.frais_cumules))
                existantes = {r["instrument"] for r in self.cx.execute("SELECT instrument FROM positions")}
                for iid in existantes - set(port_apres.positions):
                    self.cx.execute("DELETE FROM positions WHERE instrument = ?", (iid,))
                for iid, p in port_apres.positions.items():
                    self.cx.execute(
                        "INSERT INTO positions (instrument, quantite, cout_total) VALUES (?, ?, ?)"
                        " ON CONFLICT(instrument) DO UPDATE SET quantite = excluded.quantite,"
                        " cout_total = excluded.cout_total", (iid, p["quantite"], p["cout_total"]))
            self.cx.execute("COMMIT")
            return True
        except sqlite3.IntegrityError as e:
            self.cx.execute("ROLLBACK")
            if "UNIQUE" in str(e):
                return False
            if "MORT" in str(e):
                raise PortefeuilleMort(str(e)) from None
            raise
        except Exception:
            self.cx.execute("ROLLBACK")
            raise

    # --------------------------------------------------------------- journaux
    def journaliser_incident(self, type_: str, message: str, anti_doublon_s: int = 900) -> bool:
        now = self.horloge()
        cle = (type_, message)
        if now - self._derniers_incidents.get(cle, -1e18) < anti_doublon_s:
            return False
        self._derniers_incidents[cle] = now
        self.cx.execute("INSERT INTO incidents (horodatage, type, message) VALUES (?, ?, ?)", (now, type_, message))
        return True

    def enregistrer_valorisation(self, ts: float, ev) -> None:
        self.cx.execute("INSERT INTO valorisations VALUES (?, ?, ?, ?, ?)",
                        (ts, ev.valeur, ev.cash, ev.valeur_positions, ev.statut))

    def enregistrer_cotations(self, cotations, recu_le: float) -> None:
        for c in cotations:
            self.cx.execute(
                "INSERT INTO cotations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(instrument) DO UPDATE SET"
                " bid=excluded.bid, ask=excluded.ask, source=excluded.source, heure_donnee=excluded.heure_donnee,"
                " fourchette=excluded.fourchette, negociable=excluded.negociable, motif=excluded.motif,"
                " recu_le=excluded.recu_le",
                (c.instrument, c.bid, c.ask, c.source, c.heure_donnee, c.fourchette, int(c.negociable), c.motif,
                 recu_le))

    def reference(self, iid: str):
        return self.cx.execute("SELECT * FROM references_bh WHERE instrument = ?", (iid,)).fetchone()

    def creer_reference(self, iid: str, qte: float, prix_eff: float, frais: float, reste: float) -> None:
        self.cx.execute("INSERT OR IGNORE INTO references_bh VALUES (?, ?, ?, ?, ?, ?)",
                        (iid, qte, prix_eff, frais, reste, self.horloge()))

    def enregistrer_valeur_ref(self, ts: float, iid: str, valeur: float) -> None:
        self.cx.execute("INSERT INTO valorisations_ref VALUES (?, ?, ?)", (ts, iid, valeur))

    def enregistrer_strategie(self, empreinte: str, description: str) -> bool:
        r = self.cx.execute("SELECT empreinte FROM changements_strategie ORDER BY id DESC LIMIT 1").fetchone()
        if r is not None and r["empreinte"] == empreinte:
            return False
        self.cx.execute("INSERT INTO changements_strategie (horodatage, empreinte, description) VALUES (?, ?, ?)",
                        (self.horloge(), empreinte, description))
        return True

    def enregistrer_backtest(self, empreinte: str, resultats: dict) -> None:
        self.cx.execute("INSERT INTO backtests (horodatage, empreinte, resultats) VALUES (?, ?, ?)",
                        (self.horloge(), empreinte, json.dumps(resultats, ensure_ascii=False)))

    # ----------------------------------------------------------- compteur API
    def lire(self, jour: str) -> int:
        r = self.cx.execute("SELECT valeur FROM meta WHERE cle = ?", ("credits_twelvedata_" + jour,)).fetchone()
        return int(r["valeur"]) if r else 0

    def ecrire(self, jour: str, n: int) -> None:
        self.cx.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
                        ("credits_twelvedata_" + jour, str(n)))

    def lire_meta(self, cle: str):
        r = self.cx.execute("SELECT valeur FROM meta WHERE cle = ?", (cle,)).fetchone()
        return r["valeur"] if r else None

    def ecrire_meta(self, cle: str, valeur: str) -> None:
        self.cx.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
                        (cle, valeur))

    def fermer(self) -> None:
        self.cx.close()
