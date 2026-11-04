@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=py -3
py -3 --version >nul 2>&1 || set PY=python
%PY% --version >nul 2>&1 || goto :nopython
echo Bot de trading FICTIF - aucun ordre reel. Fermez la fenetre pour arreter.
%PY% lancer.py demarrer
pause
exit /b
:nopython
echo Python introuvable. Installez-le depuis https://www.python.org/downloads/ puis relancez ce fichier.
pause
