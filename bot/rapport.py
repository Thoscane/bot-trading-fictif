"""Tableau de bord HTML (français, autonome, sans internet) + rapport texte quotidien."""
from __future__ import annotations

import html
import json
import os
import time

from .outils import eur, heure_paris, nombre, pct, quantite
from .portefeuille import MORT, BLOQUE, ACTIF, EN_ATTENTE


def _stats(stock, capital: float) -> dict:
    cx = stock.cx
    valos = cx.execute("SELECT horodatage, valeur FROM valorisations WHERE valeur IS NOT NULL ORDER BY horodatage").fetchall()
    sommet, dd, dd_max = 0.0, 0.0, 0.0
    for v in valos:
        sommet = max(sommet, v["valeur"])
        dd = (sommet - v["valeur"]) / sommet * 100 if sommet else 0
        dd_max = max(dd_max, dd)
    derniere = cx.execute("SELECT * FROM valorisations ORDER BY horodatage DESC LIMIT 1").fetchone()
    txs = cx.execute("SELECT * FROM transactions ORDER BY id DESC").fetchall()
    ventes = [t for t in txs if t["sens"] == "VENTE"]
    refs = {}
    for r in cx.execute("SELECT * FROM references_bh"):
        v = cx.execute("SELECT valeur, horodatage FROM valorisations_ref WHERE instrument = ? ORDER BY horodatage DESC LIMIT 1",
                       (r["instrument"],)).fetchone()
        refs[r["instrument"]] = {"valeur": v["valeur"] if v else None, "depuis": r["cree_le"], "quantite": r["quantite"],
                                 "frais": r["frais"]}
    return {
        "etat": stock.etat(), "derniere": derniere, "valos": valos, "dd_actuel": dd, "dd_max": dd_max,
        "transactions": txs, "nb_ventes_gagnantes": sum(1 for v in ventes if (v["pnl_realise"] or 0) > 0),
        "nb_ventes_perdantes": sum(1 for v in ventes if (v["pnl_realise"] or 0) <= 0),
        "pnl_realise": sum(v["pnl_realise"] or 0 for v in ventes),
        "positions": cx.execute("SELECT * FROM positions").fetchall(),
        "cotations": cx.execute("SELECT * FROM cotations ORDER BY instrument").fetchall(),
        "decisions": cx.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 40").fetchall(),
        "incidents": cx.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 40").fetchall(),
        "strategies": cx.execute("SELECT * FROM changements_strategie ORDER BY id DESC").fetchall(),
        "backtest": cx.execute("SELECT * FROM backtests ORDER BY id DESC LIMIT 1").fetchone(),
        "references": refs,
    }


def _classe_statut(s: str) -> str:
    return {MORT: "mort", BLOQUE: "bloque", ACTIF: "actif", EN_ATTENTE: "attente"}.get(s, "suspendu")


def generer_tableau_de_bord(stock, cfg: dict, decideur_nom: str, chemin: str, instruments: dict) -> str:
    s = _stats(stock, cfg["capital_initial_eur"])
    e, d = s["etat"], s["derniere"]
    statut = MORT if e["mort"] else (d["statut"] if d else "—")
    valeur = d["valeur"] if d else None
    cap = e["capital_initial"]
    perf = (valeur / cap - 1) * 100 if valeur is not None else None
    H = html.escape

    def sect(titre, corps):
        return f"<section><h2>{H(titre)}</h2>{corps}</section>"

    courbe = [[v["horodatage"] * 1000, round(v["valeur"], 4)] for v in s["valos"]]
    courbes_ref = {}
    for iid in s["references"]:
        rows = stock.cx.execute("SELECT horodatage, valeur FROM valorisations_ref WHERE instrument = ? ORDER BY horodatage",
                                (iid,)).fetchall()
        courbes_ref[iid] = [[r["horodatage"] * 1000, round(r["valeur"], 4)] for r in rows]

    pos_html = "".join(
        f"<tr><td>{H(p['instrument'])}</td><td>{quantite(p['quantite'])}</td><td>{eur(p['cout_total'], 4)}</td>"
        f"<td>{eur(p['cout_total'] / p['quantite'], 4) if p['quantite'] else '—'}</td>"
        + (lambda c: f"<td>{eur(c['bid'], 4) if c else '—'}</td>")(
            next((c for c in s['cotations'] if c['instrument'] == p['instrument']), None))
        + "</tr>" for p in s["positions"]) or "<tr><td colspan=5>Aucune position — 100 % en espèces</td></tr>"

    cot_html = "".join(
        f"<tr class='{'ok' if c['negociable'] else 'ko'}'><td>{H(c['instrument'])}</td><td>{eur(c['bid'], 4)}</td>"
        f"<td>{eur(c['ask'], 4)}</td><td>{H(c['fourchette'] or '')}</td><td>{H(c['source'] or '')}</td>"
        f"<td>{heure_paris(c['heure_donnee'])}</td><td>{'négociable' if c['negociable'] else 'suspendu : ' + H(c['motif'] or '')}</td></tr>"
        for c in s["cotations"]) or "<tr><td colspan=7>Aucune cotation reçue</td></tr>"

    tx_html = "".join(
        f"<tr class='{t['sens'].lower()}'><td>{heure_paris(t['horodatage'])}</td><td>{t['sens']}</td><td>{H(t['instrument'])}</td>"
        f"<td>{quantite(t['quantite'])}</td><td>{eur(t['prix_marche'], 4)}</td><td>{eur(t['prix_effectif'], 4)}</td>"
        f"<td>{eur(t['montant_brut'], 4)}</td><td>{eur(t['frais'], 4)}</td>"
        f"<td>{eur(t['pnl_realise'], 4) if t['pnl_realise'] is not None else '—'}</td><td>{eur(t['cash_apres'], 4)}</td>"
        f"<td class='just'>{H(t['justification'] or '')}</td></tr>" for t in s["transactions"][:100]) \
        or "<tr><td colspan=11>Aucune transaction simulée pour l'instant</td></tr>"

    dec_html = "".join(
        f"<tr class='{'ok' if x['valide'] else 'ko'}'><td>{heure_paris(x['horodatage'])}</td><td>{heure_paris(x['cycle_ts'], '%H:%M')}</td>"
        f"<td>{H(x['action'] or '')}</td><td>{H(x['instrument'] or '—')}</td><td>{quantite(x['quantite'])}</td>"
        f"<td class='just'>{H(x['justification'] or '')}</td><td>{'exécutée' if x['valide'] and x['action'] != 'attendre' else ('—' if x['valide'] else 'REFUSÉE : ' + H(x['motif_refus'] or ''))}</td></tr>"
        for x in s["decisions"]) or "<tr><td colspan=7>Aucune décision pour l'instant</td></tr>"

    inc_html = "".join(
        f"<tr class='{'mort' if i['type'] == 'MORT' else ''}'><td>{heure_paris(i['horodatage'])}</td><td>{H(i['type'])}</td><td>{H(i['message'])}</td></tr>"
        for i in s["incidents"]) or "<tr><td colspan=3>Aucun incident</td></tr>"

    ref_html = ""
    for iid, r in s["references"].items():
        rp = (r["valeur"] / cap - 1) * 100 if r["valeur"] is not None else None
        ref_html += (f"<tr><td>Acheter puis conserver {H(iid)}</td><td>{eur(r['valeur'], 4)}</td><td>{pct(rp)}</td>"
                     f"<td>{heure_paris(r['depuis'])}</td><td>{eur(r['frais'], 4)}</td>"
                     f"<td>{pct(perf - rp) if (perf is not None and rp is not None) else '—'}</td></tr>")
    ref_html = (f"<tr class='bot'><td><b>Le bot ({H(decideur_nom)})</b></td><td>{eur(valeur, 4)}</td><td>{pct(perf)}</td>"
                f"<td>{heure_paris(e['cree_le'])}</td><td>{eur(e['frais_cumules'], 4)}</td><td>—</td></tr>" + ref_html)

    bt_html = "<p>Aucun backtest enregistré (lancez <code>python lancer.py backtest</code>).</p>"
    if s["backtest"]:
        b = json.loads(s["backtest"]["resultats"])
        refs_bt = "".join(f"<li>Acheter puis conserver {H(k)} : {eur(v['valeur_finale'], 4)} ({pct(v['performance_pct'])})</li>"
                          for k, v in b.get("references", {}).items())
        bt_html = (f"<p>Réalisé le {heure_paris(s['backtest']['horodatage'])} sur {b['nb_bougies']} bougies "
                   f"({heure_paris(b['debut'])} → {heure_paris(b['fin'])}), stratégie {H(s['backtest']['empreinte'])}.</p>"
                   f"<ul><li>Bot : {eur(b['valeur_finale'], 4)} ({pct(b['performance_pct'])}), {b['nb_transactions']} transactions, "
                   f"frais {eur(b['frais_cumules'], 4)}, baisse max {pct(b['baisse_max_pct'], signe=False)}, "
                   f"{b['nb_ventes_gagnantes']} ventes gagnantes / {b['nb_ventes_perdantes']} perdantes</li>{refs_bt}</ul>"
                   "<p class='note'>Le passé ne préjuge pas de l'avenir : ce résultat ne promet aucune rentabilité.</p>")

    strat_html = "".join(f"<li>{heure_paris(x['horodatage'])} — <code>{H(x['empreinte'])}</code> — {H(x['description'])}</li>"
                         for x in s["strategies"])
    mort_html = ""
    if e["mort"]:
        mort_html = (f"<div class='banniere mort'>☠ PORTEFEUILLE MORT depuis le {heure_paris(e['mort_le'])} — "
                     f"{H(e['mort_raison'] or '')}. Aucune nouvelle décision ni transaction n'est possible. "
                     "L'historique reste consultable ci-dessous.</div>")
    elif statut == BLOQUE:
        mort_html = ("<div class='banniere bloque'>⏸ BLOQUÉ — espèces insuffisantes pour le plus petit achat possible "
                     "et aucune position revendable. Ce n'est PAS une faillite : la valeur reste positive.</div>")

    dernier_cot = max((c["recu_le"] or 0 for c in s["cotations"]), default=None)
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot fictif — tableau de bord</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--card:#f7f7f8;--ok:#1b7f3b;--ko:#b3261e;--warn:#9a6b00;--acc:#421C4A}}
@media(prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#141418;--fg:#ececec;--muted:#a0a0a0;--line:#2a2a30;--card:#1e1e24}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 Calibri,"Segoe UI",system-ui,sans-serif;padding:16px}}
h1{{font-size:1.5rem;margin:0 0 4px}} h2{{font-size:1.1rem;margin:28px 0 8px;color:var(--acc)}}
.sous{{color:var(--muted);font-size:.9rem}} .grille{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}}
.carte{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}} .carte .l{{font-size:.8rem;color:var(--muted)}}
.carte .v{{font-size:1.35rem;font-weight:700;margin-top:2px}} .pos{{color:var(--ok)}} .neg{{color:var(--ko)}}
.statut{{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;color:#fff}}
.statut.actif{{background:var(--ok)}} .statut.attente{{background:#4a6fa5}} .statut.suspendu{{background:var(--warn)}} .statut.bloque{{background:#6b4e9a}} .statut.mort{{background:#000}}
.banniere{{padding:12px 14px;border-radius:10px;margin:14px 0;font-weight:600}} .banniere.mort{{background:#000;color:#fff}} .banniere.bloque{{background:#efe6ff;color:#3b1d66}}
table{{width:100%;border-collapse:collapse;font-size:.88rem}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{color:var(--muted);font-weight:600}}
tr.ko td{{color:var(--ko)}} tr.bot td{{font-weight:600}} td.just{{max-width:420px;color:var(--muted)}} tr.mort td{{font-weight:700}}
.scroll{{overflow-x:auto}} .note{{color:var(--muted);font-size:.85rem}} code{{font-size:.85em}}
#graph{{width:100%;height:260px;background:var(--card);border:1px solid var(--line);border-radius:10px}}
.legende span{{display:inline-block;margin-right:14px;font-size:.85rem}} .legende i{{display:inline-block;width:22px;height:3px;vertical-align:middle;margin-right:5px}}
</style></head><body>
<h1>Bot de trading <u>fictif</u> — tableau de bord</h1>
<div class="sous">Simulation pédagogique, aucun ordre réel. Décideur : <b>{H(decideur_nom)}</b>. Généré le {heure_paris(time.time())} (heure de Paris).
Dernières données reçues : {heure_paris(dernier_cot)}. Fréquence d'interrogation : toutes les {cfg['frequence_interrogation_secondes']} s ; décisions sur bougies clôturées de {cfg['intervalle_minutes']} min.</div>
{mort_html}
<div class="grille">
<div class="carte"><div class="l">Statut</div><div class="v"><span class="statut {_classe_statut(statut)}">{H(statut)}</span></div></div>
<div class="carte"><div class="l">Valeur totale (liquidation nette)</div><div class="v">{eur(valeur, 4)}</div></div>
<div class="carte"><div class="l">Gain / perte depuis le départ ({eur(cap)})</div><div class="v {'pos' if (perf or 0) >= 0 else 'neg'}">{eur(valeur - cap, 4) if valeur is not None else '—'}<br><small>{pct(perf)}</small></div></div>
<div class="carte"><div class="l">Espèces</div><div class="v">{eur(e['cash'], 4)}</div></div>
<div class="carte"><div class="l">Valeur des positions</div><div class="v">{eur(d['valeur_positions'], 4) if d else '—'}</div></div>
<div class="carte"><div class="l">Frais cumulés</div><div class="v">{eur(e['frais_cumules'], 4)}</div></div>
<div class="carte"><div class="l">Gains réalisés (ventes)</div><div class="v {'pos' if s['pnl_realise'] >= 0 else 'neg'}">{eur(s['pnl_realise'], 4)}</div></div>
<div class="carte"><div class="l">Baisse max. depuis le sommet</div><div class="v neg">{pct(s['dd_max'], signe=False)}<br><small>actuelle : {pct(s['dd_actuel'], signe=False)}</small></div></div>
<div class="carte"><div class="l">Transactions</div><div class="v">{len(s['transactions'])}<br><small>{s['nb_ventes_gagnantes']} ventes gagnantes / {s['nb_ventes_perdantes']} perdantes</small></div></div>
</div>

{sect("Évolution de la valeur (bot vs références « acheter puis conserver »)", '<svg id="graph"></svg><div class="legende" id="legende"></div>')}
{sect("Comparaison avec la référence « acheter puis conserver »", f"<div class='scroll'><table><tr><th>Stratégie</th><th>Valeur actuelle</th><th>Performance</th><th>Depuis</th><th>Frais payés</th><th>Écart bot − référence</th></tr>{ref_html}</table></div><p class='note'>Même capital de départ ({eur(cap)}), mêmes frais, même glissement, achat au premier prix négociable vu par le bot. Valorisation en liquidation nette dans les deux cas.</p>")}
{sect("Positions", f"<div class='scroll'><table><tr><th>Instrument</th><th>Quantité</th><th>Coût total (frais inclus)</th><th>Prix de revient</th><th>Prix acheteur actuel</th></tr>{pos_html}</table></div>")}
{sect("Cotations et source des données", f"<div class='scroll'><table><tr><th>Instrument</th><th>Prix acheteur (vente)</th><th>Prix vendeur (achat)</th><th>Fourchette</th><th>Source</th><th>Heure de la donnée</th><th>État</th></tr>{cot_html}</table></div>")}
{sect("Transactions simulées", f"<div class='scroll'><table><tr><th>Date</th><th>Sens</th><th>Instrument</th><th>Quantité</th><th>Prix marché</th><th>Prix effectif (glissement)</th><th>Montant brut</th><th>Frais</th><th>Gain/perte réalisé</th><th>Espèces après</th><th>Justification</th></tr>{tx_html}</table></div>")}
{sect("Dernières décisions (une par bougie clôturée)", f"<div class='scroll'><table><tr><th>Date</th><th>Bougie</th><th>Action</th><th>Instrument</th><th>Quantité</th><th>Justification</th><th>Vérification</th></tr>{dec_html}</table></div>")}
{sect("Backtest sur historique", bt_html)}
{sect("Historique des stratégies", f"<ul>{strat_html}</ul>")}
{sect("Incidents et journal", f"<div class='scroll'><table><tr><th>Date</th><th>Type</th><th>Message</th></tr>{inc_html}</table></div>")}
<p class="note">Les 10 € sont fictifs. Aucune rentabilité n'est promise ni suggérée. Les frais, le glissement et la fourchette des actions/ETF sont des estimations configurables.</p>
<script>
(function(){{
const bot={json.dumps(courbe)}, refs={json.dumps(courbes_ref, ensure_ascii=False)};
const svg=document.getElementById('graph'); const L=document.getElementById('legende');
const series=[{{n:'Bot',c:'#421C4A',d:bot}}]; const cols=['#1b7f3b','#9a6b00','#4a6fa5'];
Object.keys(refs).forEach((k,i)=>series.push({{n:'Acheter-conserver '+k,c:cols[i%3],d:refs[k]}}));
const pts=series.flatMap(s=>s.d); if(pts.length<2){{svg.outerHTML='<p class="note">Pas encore assez de points pour tracer la courbe.</p>';return;}}
const W=svg.clientWidth||800,H=260,m={{t:12,r:12,b:24,l:56}};
const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);let x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
if(y1-y0<0.02){{y0-=0.01;y1+=0.01}} const X=v=>m.l+(v-x0)/(x1-x0||1)*(W-m.l-m.r),Y=v=>H-m.b-(v-y0)/(y1-y0)*(H-m.t-m.b);
let h='';for(let i=0;i<=4;i++){{const v=y0+(y1-y0)*i/4,y=Y(v);h+=`<line x1="${{m.l}}" x2="${{W-m.r}}" y1="${{y}}" y2="${{y}}" stroke="currentColor" stroke-opacity=".12"/><text x="${{m.l-6}}" y="${{y+4}}" font-size="11" text-anchor="end" fill="currentColor">${{v.toFixed(2).replace('.',',')}} €</text>`}}
const f=t=>{{const d=new Date(t);return d.toLocaleDateString('fr-FR',{{day:'2-digit',month:'2-digit'}})+' '+d.toLocaleTimeString('fr-FR',{{hour:'2-digit',minute:'2-digit'}})}};
h+=`<text x="${{m.l}}" y="${{H-6}}" font-size="11" fill="currentColor">${{f(x0)}}</text><text x="${{W-m.r}}" y="${{H-6}}" font-size="11" text-anchor="end" fill="currentColor">${{f(x1)}}</text>`;
series.forEach(s=>{{if(!s.d.length)return;h+=`<polyline fill="none" stroke="${{s.c}}" stroke-width="2" points="${{s.d.map(p=>X(p[0])+','+Y(p[1])).join(' ')}}"/>`;L.innerHTML+=`<span><i style="background:${{s.c}}"></i>${{s.n}}</span>`}});
svg.setAttribute('viewBox',`0 0 ${{W}} ${{H}}`);svg.innerHTML=h;
}})();
</script></body></html>"""
    os.makedirs(os.path.dirname(os.path.abspath(chemin)), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(page)
    return chemin


def rapport_texte(stock, cfg: dict, decideur_nom: str, instruments: dict) -> str:
    s = _stats(stock, cfg["capital_initial_eur"])
    e, d = s["etat"], s["derniere"]
    cap = e["capital_initial"]
    valeur = d["valeur"] if d else None
    statut = MORT if e["mort"] else (d["statut"] if d else "—")
    perf = (valeur / cap - 1) * 100 if valeur is not None else None
    maintenant = time.time()
    depuis_24h = [t for t in s["transactions"] if maintenant - t["horodatage"] <= 86400]
    inc_24h = [i for i in s["incidents"] if maintenant - i["horodatage"] <= 86400]
    lignes = [
        f"POINT QUOTIDIEN — bot fictif — {heure_paris(maintenant)} (heure de Paris)",
        f"Décideur : {decideur_nom}",
        f"Statut : {statut}" + (f" — {e['mort_raison']}" if e["mort"] else ""),
        f"Valeur totale : {eur(valeur, 4)} ({pct(perf)} depuis {eur(cap)})",
        f"Espèces : {eur(e['cash'], 4)} | Positions : {eur(d['valeur_positions'], 4) if d else '—'} | Frais cumulés : {eur(e['frais_cumules'], 4)}",
        f"Gains réalisés : {eur(s['pnl_realise'], 4)} | Baisse max depuis le sommet : {pct(s['dd_max'], signe=False)}",
        f"Transactions : {len(s['transactions'])} au total, {len(depuis_24h)} sur 24 h "
        f"({s['nb_ventes_gagnantes']} ventes gagnantes / {s['nb_ventes_perdantes']} perdantes)",
    ]
    for iid, r in s["references"].items():
        rp = (r["valeur"] / cap - 1) * 100 if r["valeur"] is not None else None
        lignes.append(f"Référence acheter-conserver {iid} : {eur(r['valeur'], 4)} ({pct(rp)})"
                      + (f" — écart bot : {pct(perf - rp)}" if perf is not None and rp is not None else ""))
    if s["positions"]:
        lignes.append("Positions : " + " ; ".join(f"{p['instrument']} {quantite(p['quantite'])} (coût {eur(p['cout_total'], 4)})"
                                                 for p in s["positions"]))
    else:
        lignes.append("Positions : aucune (100 % espèces)")
    lignes.append("Cotations : " + " ; ".join(
        f"{c['instrument']} {eur(c['bid'], 2)}/{eur(c['ask'], 2)} {'OK' if c['negociable'] else 'SUSPENDU (' + (c['motif'] or '') + ')'} "
        f"[{heure_paris(c['heure_donnee'], '%d/%m %H:%M')}]" for c in s["cotations"]) or "aucune")
    if depuis_24h:
        lignes.append("Transactions des 24 h :")
        for t in reversed(depuis_24h):
            lignes.append(f"  - {heure_paris(t['horodatage'], '%d/%m %H:%M')} {t['sens']} {t['instrument']} "
                          f"{quantite(t['quantite'])} @ {eur(t['prix_effectif'], 4)} frais {eur(t['frais'], 4)} — {t['justification']}")
    if inc_24h:
        lignes.append(f"Incidents des 24 h : {len(inc_24h)}")
        for i in list(reversed(inc_24h))[:8]:
            lignes.append(f"  - {heure_paris(i['horodatage'], '%d/%m %H:%M')} [{i['type']}] {i['message']}")
    lignes.append("Rappel : simulation fictive, aucune rentabilité promise.")
    return "\n".join(lignes)
