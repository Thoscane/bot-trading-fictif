# Bot de trading entièrement fictif (10 € simulés, données réelles)

**Aucun ordre réel n'est jamais envoyé.** Le code ne contient aucune fonction privée d'API
(pas de clé Kraken, pas de signature d'ordre) : il est techniquement incapable de trader.
Les 10 € sont une variable dans une base de données. **Aucune rentabilité n'est promise** —
sur 10 € avec des frais réalistes, perdre de la valeur est le scénario le plus probable.

## 1. Ce que fait le programme, en termes simples

1. Toutes les 60 s, il demande à **Kraken** (crypto, gratuit, sans clé) et à **Twelve Data**
   (actions/ETF, clé gratuite) les derniers prix et les bougies de 5 minutes.
2. Il vérifie les données : horloge, fraîcheur, cohérence acheteur/vendeur, prix vs bougie.
   Au moindre doute → `SUSPENDU`, incident journalisé, **aucun cours inventé**.
3. **Une seule décision par bougie clôturée** (5 min) : acheter, vendre ou attendre.
   Observer un prix ne déclenche jamais une transaction à lui seul.
4. Chaque décision est **vérifiée** avant exécution simulée (espèces, minimums, pas de levier,
   pas de vente à découvert) puis écrite en une seule opération dans `donnees/bot.sqlite`.
5. Le tableau de bord `donnees/tableau_de_bord.html` est régénéré à chaque cycle.

Hypothèses de simulation (modifiables dans `config.json`) :
achat au **prix vendeur**, vente au **prix acheteur**, glissement 0,05 %, frais 0,40 % crypto /
0,50 % actions, minimums Kraken (0,45 € ; 0,00005 BTC ; 0,001 ETH), minimum 1 € pour les
actions (fractions autorisées, comme chez les courtiers à fractions d'actions).

## 2. Installation (Windows, 5 minutes)

1. Installez Python 3.10 ou plus récent : https://www.python.org/downloads/
   → cochez **« Add python.exe to PATH »** lors de l'installation.
2. Ouvrez le dossier `bot_trading_fictif` et double-cliquez `verifier.bat`.
   Il affiche la source des données, l'heure serveur, la disponibilité des actifs.
3. (Facultatif, actions/ETF) Créez une clé gratuite sur https://twelvedata.com/register,
   collez-la dans un fichier texte nommé `cle_twelvedata.txt` à côté de `config.json`.
   Sans clé, seules les cryptos sont suivies (message clair au démarrage).
4. Double-cliquez `demarrer.bat` : le bot tourne tant que la fenêtre est ouverte.
   Fermez la fenêtre ou Ctrl+C pour arrêter. Relancez : il **reprend** là où il en était,
   sans transaction en double.
5. Ouvrez `donnees\tableau_de_bord.html` dans votre navigateur (rafraîchir pour actualiser).

Aucune dépendance à installer : bibliothèque standard Python uniquement.

## 3. Commandes

| Commande | Rôle |
|---|---|
| `python lancer.py verifier` | contrôle des sources et des actifs |
| `python lancer.py cycle` | un seul cycle (utile pour un planificateur de tâches) |
| `python lancer.py demarrer` | boucle continue |
| `python lancer.py backtest` | rejoue la stratégie sur l'historique disponible (≈ 60 h de bougies 5 min chez Kraken, 25 h chez Twelve Data) |
| `python lancer.py rapport` | point quotidien en texte, archivé dans `donnees/rapports/` |
| `python lancer.py tableau` | régénère le tableau de bord |
| `python -m unittest tests.test_bot -v` | 19 tests automatiques (hors ligne) |

## 4. Statuts

- **ACTIF** : au moins un marché négociable.
- **EN ATTENTE** : marchés fermés (actions : séance NYSE 15h30–22h00 heure de Paris).
- **SUSPENDU** : données manquantes, anciennes ou incohérentes → pas de transaction.
- **BLOQUÉ** : espèces sous le plus petit achat possible et aucune position revendable.
  La valeur est > 0 : **ce n'est pas une faillite**.
- **MORT** : valeur de liquidation nette ≤ 0 €. Définitif : déclencheurs SQL + fichier
  `donnees/MORT.txt` qui survit même à la suppression de la base. Historique consultable.

## 5. Stratégie (« stratégie à règles », aucune IA connectée)

Croisement de moyennes mobiles 9/21 sur bougies 5 min clôturées, un actif à la fois,
achat avec toutes les espèces, vente au croisement inverse ou à −3 % (stop de protection),
repos de 6 bougies après chaque transaction. Toute modification de `config.json` est
enregistrée dans l'historique des stratégies (empreinte).
Pour brancher une IA : passer `decideur_ia={"appeler": fonction, "nom": "..."}` au `Moteur`
(voir `bot/strategie.py`, classe `DecideurIA`) ; elle ne reçoit que le contexte de l'instant et
sa sortie JSON est vérifiée comme toute décision.

## 6. Coûts réels

- Kraken public : 0 €. Twelve Data : 0 € (offre gratuite, 800 crédits/jour ; le bot se
  plafonne à 700). Fonctionnement sur votre PC : 0 € hors électricité.
- Hébergement 24 h/24 ailleurs (petit serveur) : typiquement 4–6 €/mois — à décider ensemble.
  Ces coûts sont réels et **totalement séparés** des 10 € fictifs.

## 7. Limites connues

- Actions/ETF : pas de carnet d'ordres → fourchette **estimée** (±0,025 %) ; données
  Twelve Data gratuites potentiellement **retardées** ; conversion USD→EUR par le taux
  EUR/USD de la même source ; jours fériés US non gérés (→ SUSPENDU automatique).
- Univers volontairement réduit (BTC, ETH, SPY, QQQ, AAPL) ; actions européennes = offre
  payante chez Twelve Data (non retenue).
- Avec 10 € et sans levier, l'état MORT exige des espèces exactement nulles ET une position
  devenue invendable ; en pratique le bot atteindra bien plus souvent **BLOQUÉ**.
- Historique disponible pour le backtest : court (quelques jours). Résultat indicatif seulement.
- Une seule instance à la fois (deux copies seraient protégées des doublons, mais inutiles).
