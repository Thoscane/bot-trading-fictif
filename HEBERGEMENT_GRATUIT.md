# Faire tourner le bot 24 h/24 pour 0 € (GitHub Actions)

Principe : GitHub exécute gratuitement `python lancer.py cycle` toutes les ~5 minutes sur
ses serveurs et enregistre la base `donnees/bot.sqlite` dans le dépôt. Votre ordinateur
peut être éteint. Coût réel : 0 € (dépôt public). Aucune carte bancaire.

## Étapes (une fois, 10 minutes)

1. Créez un compte sur https://github.com (gratuit).
2. Cliquez **New repository** → nom `bot-trading-fictif`, visibilité **Public**
   (indispensable pour la gratuité illimitée) → Create.
3. Sur la page du dépôt vide : **uploading an existing file** → glissez-déposez TOUT le
   contenu du dossier `bot_trading_fictif` (y compris le dossier caché `.github`).
   Astuce Windows : dans l'explorateur, Affichage → cocher « Éléments masqués ».
   Ne déposez PAS `cle_twelvedata.txt`.
4. (Facultatif, actions/ETF) **Settings → Secrets and variables → Actions → New repository
   secret** : nom `TWELVEDATA_API_KEY`, valeur = votre clé Twelve Data.
5. **Settings → Actions → General → Workflow permissions** : cochez
   « Read and write permissions » → Save.
6. Onglet **Actions** → « bot-fictif » → **Run workflow** pour le premier cycle.
   Ensuite il tourne seul toutes les ~5 min.

## Consulter

- Tableau de bord : fichier `donnees/tableau_de_bord.html` dans le dépôt
  (bouton « Raw » puis enregistrer, ou activez GitHub Pages : Settings → Pages →
  Branch `main`, dossier `/` → l'adresse `https://VOTRE-NOM.github.io/bot-trading-fictif/donnees/tableau_de_bord.html`).
- Rapports quotidiens : dossier `donnees/rapports/`.
- Historique complet : `donnees/bot.sqlite` (ouvrable avec « DB Browser for SQLite »).

## Limites de cette solution gratuite

- Interrogation toutes les ~5–15 min (GitHub retarde souvent les tâches planifiées) au lieu
  de 60 s. Les décisions restent prises sur bougies de 5 min clôturées ; certaines bougies
  seront simplement sautées. Le bot ne décide jamais deux fois sur la même bougie.
- GitHub désactive les tâches planifiées après 60 jours sans activité sur le dépôt ;
  les sauvegardes automatiques comptent comme activité, mais vérifiez de temps en temps.
- Les données d'exécution (la base) sont publiques, comme le code : elles ne contiennent
  que des valeurs fictives, jamais votre clé API.

## Alternative gratuite plus fidèle (plus technique)

Oracle Cloud « Always Free » fournit une vraie machine virtuelle gratuite 24 h/24
(vérification par carte bancaire, mais 0 € débité) : on y lance `python3 lancer.py demarrer`
dans un service `systemd`. À envisager si le rythme de 60 s devient important.
