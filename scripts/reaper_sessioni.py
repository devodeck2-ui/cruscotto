#!/usr/bin/env python3
"""Chiude le sessioni rimaste aperte (browser chiuso, rete caduta, tab uccisa).

Senza questo passaggio le sessioni orfane resterebbero aperte per sempre e la
successiva apertura verrebbe erroneamente "ripresa", falsando i tempi di
utilizzo. La chiusura imposta `fine = ultimo_ping`, cioe' l'ultimo istante in
cui l'allievo era dimostrabilmente davanti all'app: non si regala tempo.

Da schedulare ogni minuto.

    python3 scripts/reaper_sessioni.py [--dry-run]
"""
from __future__ import annotations

import argparse

from _comune import log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app import db
    from app.config import settings

    aperte = db.query(
        "SELECT id, utente_id, ultimo_ping, durata_sec FROM sessioni_app "
        "WHERE fine IS NULL AND ultimo_ping < datetime('now', ?)",
        (f"-{settings.inattivita_sec} seconds",))

    if not aperte:
        log("nessuna sessione orfana")
        return 0

    if args.dry_run:
        for s in aperte:
            log(f"[dry-run] chiuderei sessione {s['id']} utente {s['utente_id']} "
                f"({s['durata_sec']}s, ultimo ping {s['ultimo_ping']})")
        log(f"[dry-run] sessioni da chiudere: {len(aperte)}")
        return 0

    from app.routers.sessioni import reaper
    n = reaper()
    log(f"sessioni chiuse dal reaper: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
