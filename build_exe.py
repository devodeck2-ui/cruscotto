#!/usr/bin/env python3
"""
Compila Autoscuola Cruscotto in un eseguibile autonomo.

Va eseguito SUL SISTEMA per cui si vuole l'eseguibile: PyInstaller non fa
cross-compilazione. Compilando su Windows si ottiene Cruscotto.exe, su macOS
un .app, su Linux un binario ELF.

    python3 build_exe.py                 eseguibile in una sola cartella (avvio rapido)
    python3 build_exe.py --file-unico    un unico file, piu' comodo da spostare
    python3 build_exe.py --senza-dati    non include il database (piu' leggero)

Perche' di default NON si usa --file-unico: l'eseguibile monofile deve
estrarre ~20 MB in una cartella temporanea a ogni avvio, con 3-5 secondi di
attesa. La versione a cartella parte all'istante; si distribuisce comunque
comprimendo la cartella in uno zip.

Dopo la compilazione:
    dist/Cruscotto/Cruscotto.exe         <- si lancia con doppio clic
    dist/Cruscotto/dati-cruscotto/       <- creata al primo avvio, contiene il
                                            database: e' la cartella da salvare
                                            nei backup
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parent
NOME = "Cruscotto"


def separatore() -> str:
    # PyInstaller usa ';' su Windows e ':' altrove per --add-data
    return ";" if sys.platform == "win32" else ":"


def assicura_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        return
    except ImportError:
        pass
    print(">> installo PyInstaller")
    subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file-unico", action="store_true")
    ap.add_argument("--senza-dati", action="store_true",
                    help="esclude database e immagini dal bundle")
    ap.add_argument("--console", action="store_true", default=True,
                    help="mantiene la finestra del terminale (consigliato: mostra "
                         "l'indirizzo di rete e il QR code)")
    args = ap.parse_args()

    assicura_pyinstaller()
    sep = separatore()

    # Le dipendenze vanno installate nell'interprete che compila, altrimenti
    # PyInstaller non le trova da inserire nel bundle.
    print(">> verifico le dipendenze dell'applicazione")
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "-r", str(RADICE / "backend" / "requirements.txt")], check=True)

    dati = [
        (RADICE / "frontend", "frontend"),
        (RADICE / "backend" / "app" / "schema.sql", "backend/app"),
    ]
    if not args.senza_dati:
        dati.append((RADICE / "data", "data"))

    comando = [
        sys.executable, "-m", "PyInstaller",
        "--name", NOME,
        "--noconfirm", "--clean",
        "--paths", str(RADICE / "backend"),
        # Uvicorn e FastAPI caricano parte dei moduli per nome a runtime:
        # senza questi import espliciti il bundle risulta incompleto e
        # l'eseguibile fallisce all'avvio con ModuleNotFoundError.
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "email_validator",
        "--hidden-import", "app.main",
        "--collect-submodules", "app",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pdfplumber",   # servono solo all'ETL, non a runtime
        "--exclude-module", "fitz",
    ]
    for origine, destinazione in dati:
        comando += ["--add-data", f"{origine}{sep}{destinazione}"]
    if args.file_unico:
        comando.append("--onefile")
    if not args.console:
        comando.append("--windowed")

    icona = RADICE / "frontend" / "assets" / "icon.ico"
    if icona.exists():
        comando += ["--icon", str(icona)]

    comando.append(str(RADICE / "avvia.py"))

    print(">> compilo (pochi minuti)")
    print("   " + " ".join(comando[:6]) + " ...")
    if subprocess.run(comando).returncode != 0:
        print("!! compilazione fallita")
        return 1

    uscita = RADICE / "dist" / (NOME + (".exe" if sys.platform == "win32" and args.file_unico else ""))
    cartella = RADICE / "dist" / NOME
    bersaglio = uscita if args.file_unico else cartella

    # Un .env di esempio accanto all'eseguibile: e' li' che l'utente finale
    # mettera' la chiave dell'AI Tutor.
    destinazione_env = (RADICE / "dist") if args.file_unico else cartella
    esempio = RADICE / ".env.example"
    if esempio.exists():
        shutil.copy2(esempio, destinazione_env / ".env.example")

    print()
    print("=" * 60)
    print(f"  Eseguibile pronto: {bersaglio}")
    if not args.file_unico:
        print(f"  Distribuisci l'INTERA cartella {cartella.name}, non il solo eseguibile.")
    print(f"  Al primo avvio crea 'dati-cruscotto/' accanto a se': e' li'")
    print(f"  che risiede il database, ed e' la cartella da mettere nei backup.")
    print(f"  Per attivare il tutor AI: rinomina .env.example in .env e")
    print(f"  inserisci ANTHROPIC_API_KEY.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
