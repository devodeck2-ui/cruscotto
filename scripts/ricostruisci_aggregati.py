#!/usr/bin/env python3
"""Rigenera gli aggregati statistici dalla tabella `risposte`.

Gli aggregati (`stat_utente_argomento`, `stat_utente_giorno`) sono mantenuti
in modo incrementale a ogni risposta. Restano pero' dati **derivati**:
`risposte` e' l'unica fonte di verita'. Questa ricostruzione va eseguita dopo
un import massivo, una migrazione di schema, o in caso di sospetta deriva.

L'operazione e' idempotente: eseguirla due volte produce lo stesso risultato.

    python3 scripts/ricostruisci_aggregati.py [--verifica]
"""
from __future__ import annotations

import argparse

from _comune import log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verifica", action="store_true",
                    help="confronta gli aggregati col ricalcolo senza modificarli")
    args = ap.parse_args()

    from app import db
    from app.services import analytics

    if args.verifica:
        atteso = db.query(
            "SELECT utente_id, COUNT(*) n, SUM(CASE WHEN corretta=0 THEN 1 ELSE 0 END) e "
            "FROM risposte WHERE corretta IS NOT NULL AND argomento_id IS NOT NULL "
            "GROUP BY utente_id")
        reale = {r["utente_id"]: (r["n"], r["e"]) for r in db.query(
            "SELECT utente_id, SUM(n_risposte) n, SUM(n_errori) e "
            "FROM stat_utente_argomento GROUP BY utente_id")}
        divergenze = 0
        for r in atteso:
            att = (r["n"], r["e"] or 0)
            got = reale.get(r["utente_id"], (0, 0))
            if att != got:
                divergenze += 1
                log(f"DIVERGENZA utente {r['utente_id']}: aggregato {got} != ricalcolo {att}")
        log(f"utenti controllati: {len(atteso)}, divergenze: {divergenze}")
        return 1 if divergenze else 0

    esito = analytics.ricostruisci_aggregati()
    log(f"aggregati ricostruiti: {esito['righe_ricostruite']} righe per argomento, "
        f"{esito.get('giorni_ricostruiti', 0)} giornate")

    from app import db as _db
    _db.execute("ANALYZE")
    log("statistiche del pianificatore aggiornate (ANALYZE)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
