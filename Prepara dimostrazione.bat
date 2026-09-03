@echo off
REM ===========================================================================
REM  Autoscuola Cruscotto - prepara i dati per una dimostrazione
REM
REM  Crea tre classi, ci mette dentro gli allievi, assegna le videolezioni e
REM  le dirette alle classi, e genera un mese di esercitazioni e prove d'esame
REM  con quattro profili diversi: chi e' pronto, chi ci sta arrivando, chi va
REM  male, chi ha smesso di esercitarsi. Serve per far vedere dashboard,
REM  statistiche e grafici pieni invece che a zero.
REM
REM  Non tocca il catalogo domande. Le attivita' generate sono riconoscibili e
REM  si tolgono tutte con l'opzione di pulizia in fondo.
REM
REM  PRIMA chiudere la finestra del server (Avvia.bat), poi doppio clic qui.
REM ===========================================================================

setlocal
cd /d "%~dp0"
title Cruscotto - prepara dimostrazione

set PYEXE=
if exist ".venv\Scripts\python.exe" set PYEXE=.venv\Scripts\python.exe
if "%PYEXE%"=="" ( where py >nul 2>&1 && set PYEXE=py -3 )
if "%PYEXE%"=="" ( where python >nul 2>&1 && set PYEXE=python )
if "%PYEXE%"=="" (
    echo   x Non trovo Python. Avvia prima una volta Avvia.bat.
    pause
    goto :fine
)

echo.
echo   PREPARA DIMOSTRAZIONE
echo   ----------------------------------------------------
echo   Assicurati di aver chiuso la finestra del server.
echo.
echo   Ecco cosa farebbe:
echo.
%PYEXE% scripts\genera_demo.py --prova
echo.
echo   ----------------------------------------------------
echo   Premi un tasto per generare davvero, oppure chiudi
echo   la finestra per annullare.
echo   ----------------------------------------------------
pause

%PYEXE% scripts\genera_demo.py
if errorlevel 1 goto :guasto
%PYEXE% scripts\ricostruisci_aggregati.py
if errorlevel 1 goto :guasto

echo.
echo   ====================================================
echo   FATTO. Riapri Avvia.bat e fai Ctrl+F5 nel browser.
echo   ====================================================
echo.
echo   Per togliere i dati dimostrativi (le classi restano):
echo     %PYEXE% scripts\genera_demo.py --pulisci
echo     %PYEXE% scripts\ricostruisci_aggregati.py
echo.
pause
goto :fine

:guasto
echo.
echo   x Qualcosa si e' fermato: leggi il motivo qui sopra.
echo.
pause

:fine
endlocal
