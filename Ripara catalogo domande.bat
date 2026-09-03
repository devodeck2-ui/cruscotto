@echo off
REM ===========================================================================
REM  Autoscuola Cruscotto - riparazione del catalogo domande
REM
REM  Esegue in fila tre riparazioni, ognuna con il suo giro a vuoto:
REM    1. rimette il listato B, che nel database era rimasto senza domande
REM       ("Nessuna domanda disponibile per i filtri selezionati")
REM    2. ricataloga CQC, AM, SUP, CAP e i REV_, che avevano un solo argomento
REM       per capitolo (una simulazione CQC pescava da un capitolo soltanto)
REM    3. rimette in sesto il CAP: la domanda tornava leggibile e le
REM       alternative perse in importazione tornano al loro posto
REM
REM  PRIMA chiudere la finestra del server (Avvia.bat), poi doppio clic qui.
REM  Ogni passo salva da solo una copia del database in data\backup\.
REM ===========================================================================

setlocal
cd /d "%~dp0"
title Cruscotto - riparazione catalogo

echo.
echo   RIPARAZIONE DEL CATALOGO DOMANDE
echo   ----------------------------------------------------
echo   Assicurati di aver chiuso la finestra del server.
echo.
pause

set PYEXE=
if exist ".venv\Scripts\python.exe" set PYEXE=.venv\Scripts\python.exe
if "%PYEXE%"=="" ( where py >nul 2>&1 && set PYEXE=py -3 )
if "%PYEXE%"=="" ( where python >nul 2>&1 && set PYEXE=python )
if "%PYEXE%"=="" (
    echo   x Non trovo Python. Avvia prima una volta Avvia.bat.
    echo.
    pause
    goto :fine
)

echo.
echo   ====================================================
echo   PRIMA A VUOTO: nessuna scrittura, solo cosa farebbe
echo   ====================================================
echo.
echo   --- 1. Listato B ---
%PYEXE% scripts\ripristina_listato_b.py --prova
echo.
echo   --- 2. Ricatalogazione ---
%PYEXE% scripts\ricataloga_catalogo.py --prova
echo.
echo   --- 3. Listato CAP ---
%PYEXE% scripts\ripara_cap.py --prova
echo.
echo   ====================================================
echo   Se qui sopra non compaiono errori, premi un tasto per
echo   applicare davvero. Per annullare, chiudi la finestra.
echo   ====================================================
echo.
pause

echo.
echo   --- 1. Listato B ---
%PYEXE% scripts\ripristina_listato_b.py
if errorlevel 1 goto :guasto
echo.
echo   --- 2. Ricatalogazione ---
%PYEXE% scripts\ricataloga_catalogo.py
if errorlevel 1 goto :guasto
echo.
echo   --- 3. Listato CAP ---
%PYEXE% scripts\ripara_cap.py
if errorlevel 1 goto :guasto
echo.
echo   --- 4. Stessa correzione sul catalogo demo ---
REM  autoscuola.demo.db e' la copia versionata da cui nasce ogni nuova
REM  installazione: se non si corregge anche quella, il prossimo computer
REM  riparte con lo stesso CAP illeggibile.
%PYEXE% scripts\ripara_cap.py --db data\autoscuola.demo.db
if errorlevel 1 goto :guasto
echo.
echo   --- Ricalcolo delle statistiche ---
%PYEXE% scripts\ricostruisci_aggregati.py

echo.
echo   ====================================================
echo   FATTO. Ora si puo' riaprire Avvia.bat.
echo   ====================================================
echo.
pause
goto :fine

:guasto
echo.
echo   x Un passo si e' fermato con un errore: leggi qui sopra il motivo.
echo     Il database di partenza e' salvo in data\backup\ e si puo'
echo     rimettere al suo posto rinominandolo autoscuola.db
echo.
pause

:fine
endlocal
