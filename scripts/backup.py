#!/usr/bin/env python3
"""Backup del database con rotazione.

Usa `VACUUM INTO`, che produce una copia **consistente** anche mentre
l'applicazione sta scrivendo: e' l'unico modo corretto di copiare uno SQLite
in WAL. Copiare il file con `cp` mentre il server e' attivo produce backup
silenziosamente corrotti, perche' il contenuto del WAL non viene incluso.

Il backup risultante e' gia' compattato (VACUUM ricostruisce le pagine) e
puo' essere spedito tale e quale su storage esterno.

    python3 scripts/backup.py                       # in data/backup/
    python3 scripts/backup.py --destinazione /mnt/nas --tieni 30
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
from datetime import datetime

from _comune import RADICE, log, percorso_db


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--destinazione", type=str, default=str(RADICE / "data" / "backup"))
    ap.add_argument("--tieni", type=int, default=14, help="numero di backup da conservare")
    ap.add_argument("--comprimi", action="store_true", help="comprime in gzip dopo il vacuum")
    args = ap.parse_args()

    from pathlib import Path
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

    # Rotazione: si conservano gli N piu' recenti.
    esistenti = sorted(dest.glob("autoscuola-*.db*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for vecchio in esistenti[args.tieni:]:
        vecchio.unlink()
        log(f"rimosso backup obsoleto {vecchio.name}")

    log(f"backup conservati: {min(len(esistenti), args.tieni)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
