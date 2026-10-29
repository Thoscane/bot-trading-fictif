"""Collecte et CONTRÔLE QUALITÉ des données de marché.

Règle absolue : aucun cours n'est inventé. Si une donnée est manquante, ancienne
ou incohérente, l'instrument est marqué « non négociable », le problème est
journalisé, et aucune transaction simulée n'est faite dessus.

Tous les prix renvoyés sont en EUR (les actions US sont converties avec le taux
EUR/USD de la même source, bougie par bougie).
"""
from __future__ import annotations

from dataclasses import dataclass

import json

from .outils import nyse_ouvert, secondes_avant_cloture_nyse
from .sources import ErreurSource


@dataclass
class Cotation:
    instrument: str
    bid: float               # prix acheteur en EUR (prix auquel on VEND)
    ask: float               # prix vendeur en EUR (prix auquel on ACHÈTE)
    source: str
    heure_donnee: float      # horodatage de la donnée (pas de la réception)
    fourchette: str          # "observée" ou "estimée (…)"
    negociable: bool
    motif: str = ""
    derniere_bougie_ts: int | None = None


def verifier_achat_vente(bid: float, ask: float, ecart_max_pct: float) -> str:
    if not (bid > 0 and ask > 0):
        return f"prix nul ou négatif (acheteur {bid}, vendeur {ask})"
    if bid > ask:
        return f"prix acheteur {bid} supérieur au prix vendeur {ask}"
    ecart = (ask - bid) / ((ask + bid) / 2) * 100
    if ecart > ecart_max_pct:
        return f"écart acheteur/vendeur anormal ({ecart:.2f} % > {ecart_max_pct} %)"
    return ""


def verifier_bougies(bougies: list, maintenant: float, duree: int, fraicheur_max: float) -> str:
    if not bougies:
        return "aucune bougie clôturée disponible"
    precedent = None
    for b in bougies[-300:]:
        ts, o, h, l, c, _ = b
        if min(o, h, l, c) <= 0:
            return f"bougie avec prix nul ou négatif ({ts})"
        if l > min(o, c) + 1e-9 or h < max(o, c) - 1e-9:
            return f"bougie incohérente (haut/bas) à {ts}"
        if precedent is not None and ts <= precedent:
            return "bougies dans le désordre ou en double"
        precedent = ts
    age = maintenant - (bougies[-1][0] + duree)
    if age > fraicheur_max:
        return f"dernière bougie clôturée trop ancienne ({age / 60:.0f} min)"
    return ""


class Collecteur:
    CLE_CACHE = "cache_actions_v1"

    def __init__(self, cfg: dict, instruments: dict, kraken, twelve, incident, persistance=None):
        self.cfg = cfg
        self.ctrl = cfg["controle_donnees"]
        self.instruments = instruments
        self.kraken = kraken
        self.twelve = twelve
        self.incident = incident
        self.P = cfg["intervalle_minutes"] * 60
        self._cache: dict[str, list] = {}       # bougies brutes (devise d'origine)
        self._cache_fx: list = []
        self._maj_actions: float = 0.0
        self._erreur_actions: str = ""
        self._rotation: int = 0          # prochain élément à rafraîchir (rotation des titres)
        self._maj_titre: dict[str, float] = {}   # heure du dernier rafraîchissement réussi, par titre
        self.persistance = persistance   # objet avec lire_meta(cle) / ecrire_meta(cle, texte)
        self._charger_cache()

    # ---------------------------------------------------------- cache durable
    # Le bot hébergé redémarre à chaque passage : sans cache durable, il redemanderait tous les
    # cours d'actions à chaque fois et épuiserait le quota gratuit de Twelve Data.
    def _charger_cache(self) -> None:
        if not self.persistance:
            return
        try:
            brut = self.persistance.lire_meta(self.CLE_CACHE)
            if not brut:
                return
            d = json.loads(brut)
            self._maj_actions = float(d.get("maj", 0))
            self._rotation = int(d.get("rot", 0))
            self._maj_titre = {k: float(v) for k, v in d.get("maj_titre", {}).items()}
            self._cache = {k: [tuple(b) for b in v] for k, v in d.get("bougies", {}).items()}
            self._cache_fx = [(t, c, c, c, c, 0.0) for t, c in d.get("fx", [])]
        except (ValueError, TypeError):
            self._cache, self._cache_fx, self._maj_actions = {}, [], 0.0

    def _sauver_cache(self) -> None:
        if not self.persistance:
            return
        debut = min((v[0][0] for v in self._cache.values() if v), default=0)
        fx = [[b[0], b[4]] for b in self._cache_fx if b[0] >= debut - 4 * 86400]  # assez pour couvrir un week-end
        self.persistance.ecrire_meta(self.CLE_CACHE, json.dumps(
            {"maj": self._maj_actions, "rot": self._rotation, "maj_titre": self._maj_titre,
             "bougies": self._cache, "fx": fx},
            separators=(",", ":")))

    def _taille_lot(self) -> int:
        """Nombre d'appels par passage : l'offre gratuite autorise 8 crédits par minute."""
        return max(1, int(self.cfg["twelvedata"].get("symboles_par_passage", 7)))

    def _intervalle_rafraichissement(self, insts: list, now: float) -> float:
        """Espace les lots pendant la séance pour ne jamais dépasser le quota du jour."""
        lot = self._taille_lot()
        restants = self.twelve.credits_max - self.twelve.credits_utilises()
        possibles = restants // lot - 6                     # garde de quoi valoriser après la clôture
        if possibles <= 0:
            return float("inf")
        return max(55.0, secondes_avant_cloture_nyse(now) / possibles)

    # ------------------------------------------------------------------ public
    def collecter(self, maintenant: float) -> tuple[dict, dict]:
        cot, bg = {}, {}
        self._crypto(maintenant, cot, bg)
        self._actions(maintenant, cot, bg)
        return cot, bg

    # ------------------------------------------------------------- crypto
    def _crypto(self, now: float, cot: dict, bg: dict) -> None:
        insts = [i for i in self.instruments.values() if i.actif and i.source == "kraken"]
        if not insts or self.kraken is None:
            return
        try:
            srv = self.kraken.heure_serveur()
        except ErreurSource as e:
            self.incident("donnees", f"Kraken injoignable : {e} — transactions crypto suspendues")
            return
        bloque_global = ""
        if abs(srv - now) > self.ctrl["ecart_horloge_max_secondes"]:
            bloque_global = f"horloge locale décalée de {srv - now:+.0f} s par rapport à Kraken"
            self.incident("horloge", bloque_global + " — transactions crypto suspendues")
        try:
            statut = self.kraken.statut_systeme()
            if statut != "online":
                bloque_global = f"Kraken en statut « {statut} »"
                self.incident("marche", bloque_global + " — transactions crypto suspendues")
        except ErreurSource as e:
            bloque_global = "statut Kraken inconnu"
            self.incident("donnees", f"statut Kraken illisible : {e}")
        try:
            ticks = self.kraken.tickers({i.cle_source or i.symbole_source: i.symbole_source for i in insts})
        except ErreurSource as e:
            self.incident("donnees", f"prix Kraken indisponibles : {e} — transactions crypto suspendues")
            return
        for ins in insts:
            t = ticks.get(ins.symbole_source)
            if not t:
                self.incident("donnees", f"{ins.id} : prix absent de la réponse Kraken")
                continue
            pb = verifier_achat_vente(t["bid"], t["ask"], self.ctrl["ecart_max_achat_vente_pct"])
            if pb:
                self.incident("incoherence", f"{ins.id} : {pb} — prix rejeté, transactions suspendues")
                continue  # prix incohérent : on ne l'utilise même pas pour valoriser
            motif = bloque_global
            try:
                brutes = self.kraken.bougies(ins.symbole_source, ins.cle_source or ins.symbole_source,
                                             self.cfg["intervalle_minutes"])
                closes = [b for b in brutes if b[0] + self.P <= srv]
                pb_b = verifier_bougies(closes, srv, self.P, self.ctrl["fraicheur_max_bougie_secondes"])
                if not pb_b:
                    milieu = (t["bid"] + t["ask"]) / 2
                    dev = abs(milieu - closes[-1][4]) / closes[-1][4] * 100
                    if dev > self.ctrl["ecart_max_prix_vs_bougie_pct"]:
                        pb_b = f"prix actuel éloigné de {dev:.1f} % de la dernière clôture"
                if pb_b:
                    self.incident("donnees", f"{ins.id} : {pb_b} — transactions suspendues")
                    motif = motif or pb_b
                else:
                    bg[ins.id] = closes
            except ErreurSource as e:
                motif = motif or "bougies indisponibles"
                self.incident("donnees", f"{ins.id} : bougies Kraken indisponibles : {e}")
                closes = []
            cot[ins.id] = Cotation(ins.id, t["bid"], t["ask"], self.kraken.NOM, srv, "observée (Kraken)",
                                   negociable=not motif, motif=motif,
                                   derniere_bougie_ts=closes[-1][0] if closes else None)

    # ----------------------------------------------------------- actions
    def _actions(self, now: float, cot: dict, bg: dict) -> None:
        insts = [i for i in self.instruments.values() if i.actif and i.source == "twelvedata"]
        if not insts:
            return
        if self.twelve is None:
            return  # désactivées (clé absente) — signalé une fois au démarrage
        ouvert = nyse_ouvert(now)
        lot = self._taille_lot()
        credits_ok = self.twelve.credits_max - self.twelve.credits_utilises() >= lot
        manquants = any(not self._cache.get(i.id) for i in insts)
        a_rafraichir = credits_ok and now - self._maj_actions >= 55 and (
            manquants or not self._cache_fx
            or (ouvert and now - self._maj_actions >= self._intervalle_rafraichissement(insts, now))
            or (not ouvert and now - self._maj_actions >= 3600)
        )
        if a_rafraichir:
            self._rafraichir_actions(insts, now)
        fx = [b for b in self._cache_fx if b[0] + self.P <= now]
        besoin_fx = any(i.devise == "USD" for i in insts)
        for ins in insts:
            brutes = [b for b in self._cache.get(ins.id, []) if b[0] + self.P <= now]
            if not brutes or (besoin_fx and ins.devise == "USD" and not fx):
                continue  # aucune donnée : aucun cours, aucune valorisation (rien n'est inventé)
            pb = verifier_bougies(brutes, now, self.P, self.ctrl["anciennete_max_valorisation_heures"] * 3600)
            if pb:
                self.incident("donnees", f"{ins.id} : {pb} — aucun cours utilisé")
                continue
            conv = self._convertir(brutes, fx) if ins.devise == "USD" else brutes
            if not conv:
                self.incident("donnees", f"{ins.id} : aucun taux de change connu pour ces bougies — aucun cours utilisé")
                continue
            dernier = conv[-1]
            age = now - (dernier[0] + self.P)
            age_fx = now - (fx[-1][0] + self.P) if (ins.devise == "USD" and fx) else 0
            tolerance_fx = self.cfg["twelvedata"].get("fraicheur_change_secondes", 1800)
            alerte = self.cfg["twelvedata"].get("alerte_retard_secondes", 3600)
            if not ouvert:
                motif = "marché fermé (séance NYSE 15h30-22h00, heure de Paris)"
            elif ((age > alerte and now - self._maj_titre.get(ins.id, 0) < alerte)
                  or (6.5 * 3600 - secondes_avant_cloture_nyse(now) > alerte          # séance ouverte depuis > 1 h
                      and now - self._maj_titre.get(ins.id, now) > alerte)):
                # vrai problème : le titre a été rafraîchi récemment mais la source ne livre rien de neuf
                # (jour férié, retard), ou il n'a plus été rafraîchi depuis longtemps (quota épuisé)
                motif = f"données anciennes ({age / 60:.0f} min) — jour férié, retard de la source ou quota épuisé ?"
                self.incident("donnees", f"{ins.id} : données anciennes — transactions suspendues")
            elif age > self.ctrl["fraicheur_max_bougie_secondes"] or age_fx > tolerance_fx:
                # cas normal de la rotation : ce titre sera rafraîchi dans un prochain passage
                motif = f"cours de {age / 60:.0f} min, en attente de son tour de rafraîchissement"
            else:
                motif = ""
            demi = ins.fourchette_estimee_pct / 200
            px = dernier[4]
            cot[ins.id] = Cotation(ins.id, px * (1 - demi), px * (1 + demi),
                                   f"{self.twelve.NOM} — bougies 5 min + EUR/USD",
                                   dernier[0] + self.P,
                                   f"estimée ±{ins.fourchette_estimee_pct / 2:.3f} % (non observée)",
                                   negociable=not motif, motif=motif, derniere_bougie_ts=dernier[0])
            if not motif:
                bg[ins.id] = conv
            else:
                bg.setdefault(ins.id, conv)  # utile au contexte, mais non négociable

    def _rafraichir_actions(self, insts: list, now: float) -> None:
        """Rafraîchit un LOT de titres (rotation) : file = [EUR/USD, titre1, titre2, …].
        Les titres sans historique passent en premier. Une erreur sur un titre n'arrête pas les autres."""
        self._maj_actions = now
        file = (["__FX__"] if any(i.devise == "USD" for i in insts) else []) + [i.id for i in insts]
        par_id = {i.id: i for i in insts}
        lot = self._taille_lot()
        prioritaires = [x for x in file if (x == "__FX__" and not self._cache_fx) or (x != "__FX__" and not self._cache.get(x))]
        choix = prioritaires[:lot]
        k = self._rotation % len(file)
        while len(choix) < min(lot, len(file)):
            x = file[k % len(file)]
            if x not in choix:
                choix.append(x)
            k += 1
        self._rotation = k % len(file)
        erreurs = []
        for x in choix:
            try:
                if x == "__FX__":
                    # 5000 bougies (≈ 17 jours) : couvre les week-ends où le change ne cote pas.
                    self._cache_fx = self.twelve.bougies(self.cfg["twelvedata"]["symbole_change"],
                                                         self.cfg["intervalle_minutes"], nombre=5000)
                else:
                    self._cache[x] = self.twelve.bougies(par_id[x].symbole_source, self.cfg["intervalle_minutes"],
                                                         nombre=400)
                self._maj_titre[x] = now
            except ErreurSource as e:
                erreurs.append(f"{'EUR/USD' if x == '__FX__' else x} : {e}")
                if "plafond" in str(e) or "429" in str(e):
                    break   # quota atteint : inutile d'insister
        self._erreur_actions = " ; ".join(erreurs)
        if erreurs:
            self.incident("donnees", "actions/ETF : " + self._erreur_actions[:300])
        self._sauver_cache()

    @staticmethod
    def _convertir(bougies: list, fx: list) -> list:
        """Convertit des bougies en USD vers l'EUR avec le dernier taux EUR/USD connu À CET INSTANT
        (jamais un taux futur)."""
        sortie, j, taux = [], 0, None
        for b in bougies:
            while j < len(fx) and fx[j][0] <= b[0]:
                taux = fx[j][4]
                j += 1
            if taux is None:
                continue  # pas de taux connu avant cette bougie : on l'ignore
            sortie.append((b[0], b[1] / taux, b[2] / taux, b[3] / taux, b[4] / taux, b[5]))
        return sortie
