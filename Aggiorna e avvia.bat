@echo off
REM ===========================================================================
REM  Autoscuola Cruscotto - scarica le modifiche da GitHub e avvia il sito
REM
REM  Doppio clic su questo file quando il collega ha caricato del lavoro nuovo:
REM  scarica le sue modifiche nella cartella e poi avvia normalmente il sito.
REM ===========================================================================

cd /d "%~dp0"
title Aggiorna e avvia - Autoscuola Cruscotto

echo.
echo   AGGIORNAMENTO DA GITHUB
echo   ----------------------------------------------------
echo.

REM --- Git installato? -------------------------------------------------------
where git >nul 2>&1
if errorlevel 1 (
    echo   x Git non risulta installato su questo computer.
    echo     Scaricalo da https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)

REM --- Modifiche locali non salvate? -----------------------------------------
REM  Se ci sono file modificati e non ancora committati, git pull puo' fallire.
REM  Meglio avvisare prima che lasciare un errore incomprensibile a schermo.
git diff --quiet
if errorlevel 1 goto :modifiche_locali
git diff --cached --quiet
if errorlevel 1 goto :modifiche_locali
goto :scarica

:modifiche_locali
echo   ! Attenzione: hai modifiche non ancora salvate su GitHub.
echo.
echo     Prima di aggiornare conviene salvarle, altrimenti l'operazione
echo     potrebbe fermarsi. Da Git Bash:
echo.
echo        git add .
echo        git commit -m "descrivi cosa hai cambiato"
echo        git push
echo.
echo   Premi un tasto per provare comunque, oppure chiudi questa finestra.
pause >nul
echo.

:scarica
echo   Scarico le modifiche...
echo.
git pull
if errorlevel 1 (
    echo.
    echo   x L'aggiornamento non e' andato a buon fine.
    echo     Leggi il messaggio qui sopra: se parla di "conflict" oppure di
    echo     "local changes", chiedi aiuto prima di insistere.
    echo.
    pause
    exit /b 1
)

echo.
echo   Aggiornato. Avvio il sito...
echo.
timeout /t 2 /nobreak >nul

call "%~dp0Avvia.bat" %*
