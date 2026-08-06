#!/usr/bin/env python3
"""Pre-genera le spiegazioni dell'AI Tutor per le domande piu' sbagliate.

Perche' farlo. La spiegazione generata al volo costa 2-8 secondi di attesa
proprio nel momento peggiore: l'allievo ha appena sbagliato e la sua attenzione
e' al massimo. Pre-generare le domande statisticamente piu' critiche significa
che nella stragrande maggioranza dei casi la risposta arriva dalla cache in
meno di 50 ms.

Come sceglie le domande. Ordina per `domande.difficolta` (probabilita' d'errore
osservata sull'intera popolazione) filtrando quelle con almeno N somministrazioni,
cosi' da non sprecare chiamate su domande viste due volte per caso.

Per ogni domanda genera **entrambe** le varianti (allievo che ha risposto VERO,
allievo che ha risposto FALSO): sono le uniche due possibili, dopodiche' la
domanda e' coperta per sempre.

    export ANTHROPIC_API_KEY=sk-ant-...
    python3 scripts/pregenera_spiegazioni.py --listato B --quante 200
    python3 scripts/pregenera_spiegazioni.py --stima          # solo il preventivo
"""
from __future__ import annotations

import argparse
import time

from _comune import log

# Costo indicativo per stimare il preventivo prima di lanciare il job.
COSTO_1K_INPUT = 0.003
COSTO_1K_OUTPUT = 0.015
TOKEN_INPUT_STIMA = 1100      # system + domanda + immagine
TOKEN_OUTPUT_STIMA = 320


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listato", default="B")
    ap.add_argument("--quante", type=int, default=100, help="numero di domande da coprire")
    ap.add_argument("--min-somministrazioni", type=int, default=3)
    ap.add_argument("--pausa", type=float, default=0.4, help="secondi fra le chiamate")
    ap.add_argument("--stima", action="store_true", help="mostra il preventivo e termina")
    args = ap.parse_args()

    from app import db
    from app.config import settings
    from app.services import tutor

    candidate = db.query(
        "SELECT d.id, d.testo, d.risposta, i.percorso AS immagine, q.tronco,"
        "       c.titolo AS capitolo, a.titolo AS argomento, l.codice AS listato,"
        "       d.difficolta, d.n_somministr "
        "FROM domande d JOIN listati l ON l.id = d.listato_id AND l.codice = ? "
        "LEFT JOIN immagini i  ON i.id = d.immagine_id "
        "LEFT JOIN quesiti q   ON q.id = d.quesito_id "
        "LEFT JOIN capitoli c  ON c.id = d.capitolo_id "
        "LEFT JOIN argomenti a ON a.id = d.argomento_id "
        "WHERE d.attiva = 1 AND d.n_somministr >= ? "
        "ORDER BY d.difficolta DESC, d.n_somministr DESC LIMIT ?",
        (args.listato, args.min_somministrazioni, args.quante))

    if not candidate:
        log(f"nessuna domanda con almeno {args.min_somministrazioni} somministrazioni "
            f"sul listato {args.listato}: serve prima un po' di storico d'uso")
        return 0

    # Si generano solo le varianti non gia' presenti in cache.
    da_fare = []
    for d in candidate:
        for risposta_data in (True, False):
            if bool(d["risposta"]) == risposta_data:
                continue          # non e' un errore: nulla da spiegare
            gia = db.query_one(
                "SELECT 1 FROM ai_spiegazioni WHERE domanda_id=? AND risposta_data=? AND prompt_ver=?",
                (d["id"], 1 if risposta_data else 0, settings.prompt_version))
            if not gia:
                da_fare.append((dict(d), risposta_data))

    costo = len(da_fare) * (TOKEN_INPUT_STIMA / 1000 * COSTO_1K_INPUT +
                            TOKEN_OUTPUT_STIMA / 1000 * COSTO_1K_OUTPUT)
    log(f"domande candidate: {len(candidate)} | spiegazioni mancanti: {len(da_fare)}")
    log(f"costo stimato: {costo:.2f} USD | tempo stimato: {len(da_fare)*4/60:.0f} minuti")

    if args.stima:
        return 0
    if not settings.anthropic_api_key:
        log("ANTHROPIC_API_KEY non configurata: impossibile procedere")
        return 1
    if not da_fare:
        log("cache gia' completa, nulla da generare")
        return 0

    tenant = db.query_one("SELECT id FROM autoscuole ORDER BY id LIMIT 1")
    fatte = falliti = 0
    for i, (dom, risposta_data) in enumerate(da_fare, 1):
        try:
            tutor.spiega_errore(dom, risposta_data, tenant["id"])
            fatte += 1
        except tutor.TutorError as e:
            falliti += 1
            log(f"errore su domanda {dom['id']}: {e}")
            if falliti > 10:
                log("troppi errori consecutivi, interrompo")
                break
        if i % 20 == 0:
            log(f"avanzamento {i}/{len(da_fare)} ({fatte} generate, {falliti} fallite)")
        time.sleep(args.pausa)

    log(f"completato: {fatte} spiegazioni generate, {falliti} fallite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
