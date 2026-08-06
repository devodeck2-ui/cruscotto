#!/usr/bin/env python3
"""
Autoscuola Cruscotto - avviatore.

Un solo file che porta l'applicazione da "cartella scaricata" a "aperta nel
browser", senza che l'utente debba conoscere Python, pip o gli ambienti
virtuali. Fa cinque cose:

  1. verifica la versione di Python e, se le dipendenze mancano, crea un
     ambiente virtuale in .venv, le installa e si riavvia al suo interno
  2. prepara i dati in una cartella scrivibile (necessario quando gira come
     eseguibile congelato, dove il bundle e' in sola lettura)
  3. sceglie una porta libera partendo da 8080
  4. si mette in ascolto su TUTTA la rete locale e stampa l'indirizzo con cui
     raggiungerlo da tablet e telefono, con un QR code da inquadrare
  5. apre il browser appena il server risponde

    python3 avvia.py                  avvio normale
    python3 avvia.py --porta 9000     porta specifica
    python3 avvia.py --solo-locale    ascolta solo su 127.0.0.1
    python3 avvia.py --no-browser     non apre il browser
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PYTHON_MINIMO = (3, 9)
PORTA_PREFERITA = 8080
GUARDIA_BOOTSTRAP = "AC_BOOTSTRAP_FATTO"


# --------------------------------------------------------------------------- #
# Presentazione
# --------------------------------------------------------------------------- #

def _colore(testo: str, codice: str) -> str:
    # Su Windows i codici ANSI funzionano da Windows 10; se il terminale non li
    # supporta si degrada a testo semplice invece di sporcare l'output.
    if os.name == "nt" and not os.environ.get("WT_SESSION") and not os.environ.get("ANSICON"):
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7)
        except Exception:
            return testo
    return f"\033[{codice}m{testo}\033[0m"


grassetto = lambda t: _colore(t, "1")
verde = lambda t: _colore(t, "32")
giallo = lambda t: _colore(t, "33")
rosso = lambda t: _colore(t, "31")
azzurro = lambda t: _colore(t, "36")


def intestazione() -> None:
    print()
    print(grassetto("  AUTOSCUOLA CRUSCOTTO"))
    print("  Quiz ministeriali, videocorsi e tutor AI")
    print("  " + "-" * 52)


def passo(testo: str) -> None:
    print(f"  {azzurro('>')} {testo}")


def errore(testo: str) -> None:
    print(f"  {rosso('x')} {testo}")


# --------------------------------------------------------------------------- #
# Percorsi: sorgente vs eseguibile congelato
# --------------------------------------------------------------------------- #

def congelato() -> bool:
    return getattr(sys, "frozen", False)


def cartella_risorse() -> Path:
    """Dove stanno i file di sola lettura (codice, frontend, dati iniziali)."""
    if congelato():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def cartella_dati() -> Path:
    """Dove stanno i file scrivibili (database, immagini, backup).

    Nell'eseguibile congelato il bundle viene estratto in una cartella
    temporanea di sola lettura, cancellata alla chiusura: scriverci il
    database significherebbe perdere i progressi degli allievi a ogni riavvio.
    I dati vengono quindi copiati, alla prima esecuzione, in una cartella
    stabile accanto all'eseguibile.
    """
    if congelato():
        d = Path(sys.executable).parent / "dati-cruscotto"
        if not (d / "autoscuola.db").exists():
            d.mkdir(parents=True, exist_ok=True)
            origine = cartella_risorse() / "data"
            passo("prima esecuzione: copio database e immagini in " + str(d))
            for elemento in origine.iterdir():
                destinazione = d / elemento.name
                if destinazione.exists():
                    continue
                if elemento.is_dir():
                    shutil.copytree(elemento, destinazione)
                else:
                    shutil.copy2(elemento, destinazione)
        return d
    return cartella_risorse() / "data"


# --------------------------------------------------------------------------- #
# Dipendenze
# --------------------------------------------------------------------------- #

def dipendenze_presenti() -> bool:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        return True
    except ImportError:
        return False


def python_del_venv(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepara_ambiente() -> None:
    """Crea .venv, installa le dipendenze e riavvia lo script al suo interno.

    La guardia in variabile d'ambiente impedisce il ciclo infinito nel caso
    l'installazione riesca ma l'import continui a fallire.
    """
    radice = cartella_risorse()
    venv = radice / ".venv"
    py_venv = python_del_venv(venv)
    requisiti = radice / "backend" / "requirements.txt"

    if os.environ.get(GUARDIA_BOOTSTRAP):
        errore("le dipendenze risultano ancora mancanti dopo l'installazione.")
        errore("prova a mano:  " + str(py_venv) + " -m pip install -r " + str(requisiti))
        sys.exit(1)

    if not py_venv.exists():
        passo("primo avvio: preparo l'ambiente Python (una sola volta, ~1 minuto)")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        except subprocess.CalledProcessError:
            errore("impossibile creare l'ambiente virtuale.")
            errore("su Debian/Ubuntu potrebbe servire:  sudo apt install python3-venv")
            sys.exit(1)

    passo("installo le dipendenze")
    comando = [str(py_venv), "-m", "pip", "install", "--quiet",
               "--disable-pip-version-check", "-r", str(requisiti)]
    if subprocess.run(comando).returncode != 0:
        errore("installazione delle dipendenze fallita: controlla la connessione a internet")
        sys.exit(1)

    print(f"  {verde('v')} ambiente pronto, riavvio nell'ambiente virtuale")
    ambiente = {**os.environ, GUARDIA_BOOTSTRAP: "1"}
    os.execve(str(py_venv), [str(py_venv), str(radice / "avvia.py"), *sys.argv[1:]], ambiente)


# --------------------------------------------------------------------------- #
# Rete
# --------------------------------------------------------------------------- #

def porta_libera(preferita: int) -> int:
    for porta in range(preferita, preferita + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", porta)) != 0:
                return porta
    return preferita


def ip_locale() -> str | None:
    """IP della macchina sulla rete locale.

    Si apre un socket UDP verso un indirizzo esterno: non viene inviato
    alcun pacchetto, ma il sistema operativo assegna al socket l'interfaccia
    che userebbe per uscire, che e' esattamente quella giusta anche in
    presenza di piu' schede di rete o VPN.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.4)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None


def stampa_qr(url: str) -> None:
    """QR code nel terminale. Se la libreria non c'e', si stampa solo l'URL:
    il QR e' una comodita', non una dipendenza bloccante."""
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1, box_size=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    matrice = qr.get_matrix()
    # Due righe per riga di caratteri usando i blocchi mezzi: il QR resta
    # quadrato e leggibile dalla fotocamera anche su terminali stretti.
    pieno, alto, basso, vuoto = "█", "▀", "▄", " "
    print()
    for y in range(0, len(matrice), 2):
        riga = ""
        for x in range(len(matrice[y])):
            su = matrice[y][x]
            giu = matrice[y + 1][x] if y + 1 < len(matrice) else False
            riga += vuoto if (su and giu) else basso if su else alto if giu else pieno
        print("   " + riga)


def attendi_e_apri(url: str, apri: bool) -> None:
    for _ in range(60):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(url + "api/salute", timeout=1)
            break
        except Exception:
            continue
    else:
        return
    if apri:
        try:
            webbrowser.open(url)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Avvio
# --------------------------------------------------------------------------- #

def main() -> int:
    # Riga per riga: se l'output viene rediretto su file (servizio, log di
    # sistema) il buffering nasconderebbe l'indirizzo proprio a chi serve.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--porta", type=int, default=int(os.environ.get("PORT", PORTA_PREFERITA)))
    ap.add_argument("--solo-locale", action="store_true",
                    help="ascolta solo su questo PC, non sulla rete locale")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--ricarica", action="store_true", help="ricarica automatica (sviluppo)")
    args = ap.parse_args()

    intestazione()

    if sys.version_info < PYTHON_MINIMO:
        errore(f"serve Python {PYTHON_MINIMO[0]}.{PYTHON_MINIMO[1]} o superiore "
               f"(trovato {sys.version_info.major}.{sys.version_info.minor})")
        return 1

    if not congelato() and not dipendenze_presenti():
        prepara_ambiente()          # non ritorna: sostituisce il processo

    risorse, dati = cartella_risorse(), cartella_dati()
    if not (dati / "autoscuola.db").exists():
        errore(f"database non trovato in {dati}")
        errore("ricostruiscilo con:  python3 etl/build_db.py --demo --reset")
        return 1

    os.environ.setdefault("AC_DB", str(dati / "autoscuola.db"))
    os.environ.setdefault("AC_MEDIA", str(dati / "media"))
    os.environ.setdefault("AC_FRONTEND", str(risorse / "frontend"))
    sys.path.insert(0, str(risorse / "backend"))

    # Il file .env, se presente, ha priorita' sui valori di default ma non
    # sovrascrive quanto gia' impostato nell'ambiente reale.
    env_file = (Path(sys.executable).parent if congelato() else risorse) / ".env"
    if env_file.exists():
        for riga in env_file.read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if riga and not riga.startswith("#") and "=" in riga:
                chiave, valore = riga.split("=", 1)
                os.environ.setdefault(chiave.strip(), valore.strip())
        passo("configurazione caricata da .env")

    porta = porta_libera(args.porta)
    if porta != args.porta:
        passo(f"porta {args.porta} occupata, uso la {porta}")
    host = "127.0.0.1" if args.solo_locale else "0.0.0.0"
    url_locale = f"http://localhost:{porta}/"

    try:
        import uvicorn
        from app.main import app
    except Exception as e:
        errore(f"impossibile caricare l'applicazione: {e}")
        return 1

    import sqlite3
    con = sqlite3.connect(os.environ["AC_DB"])
    n_domande = con.execute("SELECT COUNT(*) FROM domande").fetchone()[0]
    con.close()

    print(f"  {verde('v')} database: {n_domande:,} domande ministeriali".replace(",", "."))
    chiave_ai = (os.environ.get("ANTHROPIC_API_KEY")
                 or os.environ.get("GEMINI_API_KEY")
                 or os.environ.get("GOOGLE_API_KEY"))
    if os.environ.get("ANTHROPIC_API_KEY"):
        ai = "attivo (Claude)"
    elif chiave_ai:
        ai = "attivo (Google Gemini, piano gratuito)"
    else:
        ai = "non configurato (manca ANTHROPIC_API_KEY o GEMINI_API_KEY)"
    print(f"  {verde('v') if chiave_ai else giallo('!')} tutor AI: {ai}")
    print("  " + "-" * 52)
    print(f"  Su questo PC:        {grassetto(url_locale)}")

    ip = None if args.solo_locale else ip_locale()
    if ip:
        url_rete = f"http://{ip}:{porta}/"
        print(f"  Da tablet/telefono:  {grassetto(url_rete)}")
        print(f"  {giallo('Stesso Wi-Fi. Dal telefono: Condividi > Aggiungi a schermata Home')}")
        stampa_qr(url_rete)
    print()
    print(f"  Accessi demo: marco@demo.it (allievo) / admin@demo.it (autoscuola)")
    print(f"  Password: demo1234")
    print("  " + "-" * 52)
    print(f"  {giallo('Premi CTRL+C per fermare il server')}")
    print()

    threading.Thread(target=attendi_e_apri, args=(url_locale, not args.no_browser),
                     daemon=True).start()

    try:
        uvicorn.run("app.main:app" if args.ricarica else app, host=host, port=porta,
                    reload=args.ricarica, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        pass
    print(f"\n  {verde('Server fermato.')} A presto.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
