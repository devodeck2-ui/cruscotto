@echo off
REM ===========================================================================
REM  Autoscuola Cruscotto - salva il tuo lavoro su GitHub
REM
REM  Doppio clic quando hai finito di lavorare: mostra cosa e' cambiato,
REM  ti chiede una descrizione e carica tutto su GitHub.
REM ===========================================================================

setlocal
cd /d "%~dp0"
title Salva su GitHub - Autoscuola Cruscotto

echo.
echo   SALVA IL TUO LAVORO SU GITHUB
echo   ----------------------------------------------------
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo   x Git non risulta installato su questo computer.
    echo.
    pause
    exit /b 1
)

REM --- C'e' qualcosa da salvare? ---------------------------------------------
git status --porcelain > "%TEMP%\ac_stato.txt"
for %%A in ("%TEMP%\ac_stato.txt") do if %%~zA==0 (
    del "%TEMP%\ac_stato.txt" >nul 2>&1
    echo   Non ci sono modifiche da salvare: la cartella e' identica a GitHub.
    echo.
    pause
    exit /b 0
)
del "%TEMP%\ac_stato.txt" >nul 2>&1

echo   File modificati:
echo.
git status --short
echo.
echo   ----------------------------------------------------
echo.

REM --- Descrizione ------------------------------------------------------------
set "MESSAGGIO="
set /p "MESSAGGIO=  Cosa hai cambiato? (poi premi Invio): "
if "%MESSAGGIO%"=="" set "MESSAGGIO=Modifiche varie"

echo.
echo   Salvo in locale...
git add -A
if errorlevel 1 goto :errore

git commit -m "%MESSAGGIO%"
if errorlevel 1 goto :errore

REM --- Prima scarico, per non sovrascrivere il lavoro del collega -------------
echo.
echo   Controllo se ci sono novita' da GitHub...
git pull --rebase
if errorlevel 1 (
    echo.
    echo   ! Il collega ha modificato gli stessi file e git non sa come unirli.
    echo     Il tuo lavoro e' comunque salvato in locale, non hai perso niente.
    echo     Serve sistemare il conflitto a mano: chiedi aiuto prima di insistere.
    echo.
    pause
    exit /b 1
)

echo.
echo   Carico su GitHub...
git push
if errorlevel 1 goto :errore

echo.
echo   ----------------------------------------------------
echo   Fatto: il tuo lavoro e' su GitHub.
echo   ----------------------------------------------------
echo.
pause
exit /b 0

:errore
echo.
echo   x Qualcosa non e' andato a buon fine. Leggi il messaggio qui sopra.
echo.
pause
exit /b 1
