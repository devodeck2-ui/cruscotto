#!/usr/bin/env python3
"""Backup del database con rotazione.

Usa `VACUUM INTO`, che produce una copia **consistente** anche mentre
l'applicazione sta scrivendo: e' l'unico modo corretto di copiare uno SQLite
in WAL. Copiare il file con `cp` mentre il server e' attivo produce backup
silenziosamente corrotti, perche' il contenuto del WAL non viene incluso.

Il backup risultante e' gia' compattato (VACUUM ricostruisce le pagine) e
puo' essere spedito tale e quale su storage esterno.

Un backup contiene TUTTE le anagrafiche degli allievi: se esce dal server,
esce l'intero elenco. Con --cifra il file viene cifrato con la password in
AC_BACKUP_PASSWORD (AES-256-GCM, chiave derivata con PBKDF2): si puo' spedire
su un disco esterno o su un cloud senza consegnare i dati a chi lo ospita.

    python3 scripts/backup.py                       # in data/backup/
    python3 scripts/backup.py --destinazione /mnt/nas --tieni 30
    python3 scripts/backup.py --comprimi --cifra    # pronto da portare fuori
    python3 scripts/backup.py --decifra data/backup/autoscuola-....db.gz.enc
"""
from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from _comune import RADICE, log, percorso_db

MAGIA = b"ACBK1"          # riconosce i nostri file cifrati
ITERAZIONI = 200_000


def _chiave(password: str, sale: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), sale, ITERAZIONI, dklen=32)


def _aesgcm():
    """La libreria di cifratura, con un messaggio comprensibile se manca."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except ImportError:
        raise SystemExit("Per cifrare serve la libreria cryptography: "
                         "pip install cryptography")


def _password(conferma: bool) -> str:
    pw = os.getenv("AC_BACKUP_PASSWORD", "")
    if pw:
        return pw
    pw = getpass.getpass("Password del backup: ")
    if conferma and pw != getpass.getpass("Ripeti la password: "):
        raise SystemExit("Le due password non coincidono.")
    if len(pw) < 12:
        raise SystemExit("Password troppo corta: almeno 12 caratteri. "
                         "Meglio ancora impostarla in AC_BACKUP_PASSWORD.")
    return pw


def cifra(percorso: Path) -> Path:
    """Sostituisce il file con la sua versione cifrata (.enc).

    Formato: MAGIA | sale (16) | nonce (12) | testo cifrato. La password non
    viene salvata da nessuna parte: se la si perde il backup e' perduto, ed e'
    esattamente il punto - va custodita come le chiavi della sede.
    """
    AESGCM = _aesgcm()
    pw = _password(conferma=True)
    sale, nonce = os.urandom(16), os.urandom(12)
    dati = percorso.read_bytes()
    cifrato = AESGCM(_chiave(pw, sale)).encrypt(nonce, dati, None)
    uscita = Path(str(percorso) + ".enc")
    uscita.write_bytes(MAGIA + sale + nonce + cifrato)
    percorso.unlink()
    return uscita


def decifra(percorso: Path) -> Path:
    AESGCM = _aesgcm()
    grezzo = percorso.read_bytes()
    if not grezzo.startswith(MAGIA):
        raise SystemExit(f"{percorso.name} non e' un backup cifrato da questo script.")
    sale, nonce, corpo = grezzo[5:21], grezzo[21:33], grezzo[33:]
    pw = _password(conferma=False)
    try:
        dati = AESGCM(_chiave(pw, sale)).decrypt(nonce, corpo, None)
    except Exception:
        raise SystemExit("Password sbagliata, oppure il file e' danneggiato.")
    uscita = Path(str(percorso)[:-4])          # toglie il .enc
    uscita.write_bytes(dati)
    return uscita


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--destinazione", type=str, default=str(RADICE / "data" / "backup"))
    ap.add_argument("--tieni", type=int, default=14, help="numero di backup da conservare")
    ap.add_argument("--comprimi", action="store_true", help="comprime in gzip dopo il vacuum")
    ap.add_argument("--cifra", action="store_true",
                    help="cifra il backup con la password in AC_BACKUP_PASSWORD")
    ap.add_argument("--decifra", type=str, metavar="FILE",
                    help="rilegge un backup cifrato e lo riporta in chiaro")
    args = ap.parse_args()

    if args.decifra:
        uscita = decifra(Path(args.decifra))
        log(f"backup decifrato in {uscita} - ricordati di cancellarlo quando hai finito")
        return 0

    dest = Path(args.destinazione)
    dest.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    uscita = dest / f"autoscuola-{marca}.db"

    con = sqlite3.connect(percorso_db())
    con.execute("VACUUM INTO ?", (str(uscita),))
    con.close()
    dimensione = uscita.stat().st_size

    if args.comprimi:
        with open(uscita, "rb") as f_in, gzip.open(str(uscita) + ".gz", "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        uscita.unlink()
        uscita = Path(str(uscita) + ".gz")
        log(f"backup creato {uscita.name} ({dimensione/1e6:.1f} MB -> {uscita.stat().st_size/1e6:.1f} MB)")
    else:
        log(f"backup creato {uscita.name} ({dimensione/1e6:.1f} MB)")

    if args.cifra:
        uscita = cifra(uscita)
        log(f"backup cifrato in {uscita.name}")
    else:
        log("ATTENZIONE: backup in chiaro, contiene le anagrafiche degli allievi. "
            "Tienilo su un disco cifrato, oppure rilancia con --cifra.")

    # Rotazione: si conservano gli N piu' recenti.
    esistenti = sorted(dest.glob("autoscuola-*.db*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for vecchio in esistenti[args.tieni:]:
        vecchio.unlink()
        log(f"rimosso backup obsoleto {vecchio.name}")

    log(f"backup conservati: {min(len(esistenti), args.tieni)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
