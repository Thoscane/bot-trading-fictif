@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=py -3
py -3 --version >nul 2>&1 || set PY=python
%PY% --version >nul 2>&1 || goto :nopython
%PY% lancer.py verifier
echo.
echo Tests automatiques :
%PY% -m unittest tests.test_bot
pause
exit /b
:nopython
echo Python introuvable. Installez-le depuis https://www.python.org/downloads/ puis relancez ce fichier.
pause
