@echo off
REM ===========================================================================
REM  Autoscuola Cruscotto - avvio su Windows
REM
REM  Fare doppio clic su questo file. Al primo avvio prepara l'ambiente
REM  (circa un minuto), poi apre da solo il browser.
REM
REM  Requisito: Python 3.9 o superiore installato con l'opzione
REM  "Add Python to PATH" attiva.
REM ===========================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"
title Autoscuola Cruscotto

echo.
echo   AUTOSCUOLA CRUSCOTTO
echo   ----------------------------------------------------

REM --- Individua l'interprete Python -----------------------------------------
REM  Si prova prima il launcher ufficiale "py", che e' il modo piu' affidabile
REM  di trovare l'installazione corretta anche con piu' versioni presenti.
set PYEXE=
where py >nul 2>&1 && set PYEXE=py -3
if "%PYEXE%"=="" ( where python >nul 2>&1 && set PYEXE=python )
if "%PYEXE%"=="" ( where python3 >nul 2>&1 && set PYEXE=python3 )

if "%PYEXE%"=="" goto :senza_python

REM --- Verifica la versione ---------------------------------------------------
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo   x Python presente ma troppo vecchio: serve la versione 3.9 o superiore.
    echo.
    goto :senza_python
)

REM --- Se l'ambiente virtuale esiste gia', si usa direttamente ----------------
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" avvia.py %*
) else (
    %PYEXE% avvia.py %*
)

set CODICE=%errorlevel%
if not "%CODICE%"=="0" (
    echo.
    echo   Il server si e' chiuso con codice %CODICE%.
    echo   Se l'errore si ripete, elimina la cartella .venv e riprova.
    echo.
    pause
)
goto :fine

:senza_python
echo.
echo   Python non e' installato su questo computer.
echo.
echo   1. Si aprira' la pagina di download di Python.
echo   2. Scarica la versione per Windows e installala.
echo   3. IMPORTANTE: nella prima schermata dell'installazione
echo      spunta la casella "Add python.exe to PATH".
echo   4. Al termine, richiudi questa finestra e fai di nuovo
echo      doppio clic su Avvia.bat
echo.
pause
start https://www.python.org/downloads/
goto :fine

:fine
endlocal
