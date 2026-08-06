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
REM  Due casi distinti: file modificati non ancora committati, oppure commit
REM  gia' pronti in locale ma non ancora caricati su GitHub.
set "DA_COMMITTARE="
git status --porcelain > "%TEMP%\ac_stato.txt"
for %%A in ("%TEMP%\ac_stato.txt") do if not %%~zA==0 set "DA_COMMITTARE=1"
del "%TEMP%\ac_stato.txt" >nul 2>&1

if defined DA_COMMITTARE goto :ci_sono_modifiche

REM --- Niente da committare: restano commit da caricare? ---------------------
for /f %%N in ('git rev-list --count @{u}..HEAD 2^>nul') do set "IN_ATTESA=%%N"
if not defined IN_ATTESA set "IN_ATTESA=0"

if "%IN_ATTESA%"=="0" (
    echo   Non c'e' niente da salvare: la cartella e' identica a GitHub.
    echo.
    pause
    exit /b 0
)

echo   Nessun file nuovo da salvare, ma ci sono %IN_ATTESA% modifiche
echo   gia' pronte da caricare su GitHub.
echo.
echo   Carico...
git push
if errorlevel 1 goto :errore
echo.
echo   ----------------------------------------------------
echo   Fatto: tutto e' su GitHub.
echo   ----------------------------------------------------
echo.
pause
exit /b 0

:ci_sono_modifiche

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
