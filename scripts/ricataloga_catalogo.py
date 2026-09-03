#!/usr/bin/env python3
"""Ricostruisce capitoli e argomenti dei listati importati dai PDF ministeriali.

    python3 scripts/ricataloga_catalogo.py            # sul database di lavoro
    python3 scripts/ricataloga_catalogo.py --db X.db  # su un database preciso
    python3 scripts/ricataloga_catalogo.py --prova    # mostra cosa farebbe

## Il problema

L'importazione (etl/build_db.py) ricavava il titolo del capitolo togliendo dal
titolo del quesito il prefisso ministeriale "NN##tipologiaNN". Per quasi tutti
i listati va bene, perche' dopo il prefisso c'e' il nome dell'argomento. Per la
CQC no: dopo il prefisso resta "CQC - DL 30 LUGLIO 2021", uguale per tutti e
393 i quesiti. Risultato: 5.406 domande su 5.416 finite in un capitolo unico, e
la funzione piu' importante del progetto - ripetere i capitoli dove si sbaglia -
per chi prepara la CQC non funzionava affatto.

Il secondo problema riguarda tutti i listati importati dai PDF: veniva creato
un solo argomento per capitolo, quindi il livello "argomento" era degenere e le
statistiche per argomento avevano la stessa grana dei capitoli.

## Cosa fa

Per ogni listato importato da PDF ricostruisce la gerarchia leggendo la fonte
in data/raw/:

  capitolo  = la tipologia ministeriale (il numero nel prefisso del titolo)
  argomento = il singolo quesito, cioe' il gruppo di 10-15 vero/falso che il
              Ministero pubblica insieme

Le domande NON vengono ne' create ne' cancellate: cambiano solo di capitolo e
argomento. Le risposte gia' date restano valide e gli aggregati vengono
ricalcolati alla fine, cosi' le statistiche restano coerenti.

Rieseguirlo non fa danni: gli slug sono derivati dai codici ministeriali,
quindi la seconda esecuzione trova tutto gia' a posto.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
RAW = RADICE / "data" / "raw"

# I 16 raggruppamenti della CQC hanno nel file solo il numero di tipologia.
# Questi nomi sono ricavati dal contenuto reale dei quesiti di ciascun gruppo
# (verificati a campione) e servono a rendere leggibile l'elenco dei capitoli:
# se l'autoscuola preferisce la dicitura ufficiale del programma d'esame,
# basta cambiarli qui e rieseguire.
TIPOLOGIE_CQC = {
    "01": "Motore, coppia e trasmissione",
    "02": "Sistemi elettronici di sicurezza",
    "03": "Uso del cambio e guida economica",
    "04": "Frenata, aderenza e ingombri del veicolo",
    "05": "Sicurezza sul lavoro e psicologia del traffico",
    "06": "Tempi di guida e di riposo",
    "07": "Prevenzione della criminalita' e del traffico di clandestini",
    "08": "Immagine dell'azienda e qualita' del servizio",
    "09": "Ergonomia, postura e salute del conducente",
    "10": "Alimentazione, alcol e condizioni psicofisiche",
    "11": "Merci, imballaggi e unita' di carico",
    "12": "Disciplina dell'autotrasporto di merci",
    "13": "Mercato dei trasporti, ADR e sostenibilita'",
    "14": "Guida sicura e comportamento del conducente",
    "15": "Forze, energia e dinamica del veicolo",
    "16": "Trasporto di persone e servizio alla clientela",
}
TIPOLOGIE = {"CQC": TIPOLOGIE_CQC, "REV_CQC": TIPOLOGIE_CQC}

PREFISSO = re.compile(r"^(\d+)##(\w+)\s*")


def slugify(s: str, maxlen: int = 80) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:maxlen] or "senza-titolo"


def etichetta_argomento(quesito: dict) -> str:
    """Titolo leggibile per il quesito: la sua prima affermazione, accorciata.

    I quesiti dei PDF ministeriali non hanno un titolo proprio (il campo
    "tronco" e' vuoto per tutti), quindi si usa la prima domanda del gruppo:
    e' cio' che rende riconoscibile l'argomento nell'elenco delle aree critiche.
    """
    testo = (quesito["domande"][0]["testo"] if quesito.get("domande") else "").strip()
    testo = re.sub(r"\s+", " ", testo)
    if len(testo) <= 70:
        return testo or "Quesito " + str(quesito.get("codice", "?"))
    taglio = testo[:70].rsplit(" ", 1)[0]
    return taglio + "..."


def piano(dati: dict, codice_listato: str) -> list[dict]:
    """Calcola, per ogni quesito, capitolo e argomento di destinazione."""
    quesiti = dati["quesiti"]

    # Il prefisso si toglie solo se cio' che resta distingue davvero i gruppi.
    # Per la CQC resta "CQC - DL 30 LUGLIO 2021" per tutti e 393 i quesiti (17
    # titoli grezzi -> 2 puliti): li' comanda il numero di tipologia. Per la
    # revisione CQC invece i nomi ci sono e se ne perderebbero solo cinque su
    # trentatre, quindi si tengono. La soglia di meta' separa i due casi senza
    # dover elencare a mano i listati.
    puliti = {PREFISSO.sub("", q["titolo"]).strip() for q in quesiti}
    grezzi = {q["titolo"] for q in quesiti}
    usa_numero = len(puliti) * 2 <= len(grezzi)

    nomi = TIPOLOGIE.get(codice_listato, {})
    fuori = []
    for ordine, q in enumerate(quesiti):
        m = PREFISSO.match(q["titolo"])
        pulito = PREFISSO.sub("", q["titolo"]).strip()
        if usa_numero and m:
            numero = m.group(1)
            titolo_cap = nomi.get(numero) or f"Tipologia {int(numero)}"
            slug_cap = f"tipologia-{numero}"
        else:
            titolo_cap = pulito or "Non classificato"
            slug_cap = slugify(titolo_cap)
        fuori.append({
            "codice": str(q["codice"]),
            "capitolo_titolo": titolo_cap,
            "capitolo_slug": slug_cap,
            "capitolo_ordine": int(m.group(1)) if (usa_numero and m) else ordine,
            "argomento_titolo": etichetta_argomento(q),
            "argomento_slug": "q-" + str(q["codice"]),
            "argomento_ordine": ordine,
        })
    return fuori


def applica(con: sqlite3.Connection, codice_listato: str, voci: list[dict], prova: bool) -> dict:
    riga = con.execute("SELECT id FROM listati WHERE codice = ?", (codice_listato,)).fetchone()
    if not riga:
        return {"saltato": "listato assente dal database"}
    lid = riga[0]

    prima = con.execute(
        "SELECT (SELECT COUNT(*) FROM capitoli WHERE listato_id = ?),"
        "       (SELECT COUNT(*) FROM argomenti a JOIN capitoli c ON c.id = a.capitolo_id"
        "        WHERE c.listato_id = ?)", (lid, lid)).fetchone()

    if prova:
        return {"capitoli": f"{prima[0]} -> {len({v['capitolo_slug'] for v in voci})}",
                "argomenti": f"{prima[1]} -> {len({v['argomento_slug'] for v in voci})}"}

    capitoli: dict[str, int] = {}
    for v in voci:
        if v["capitolo_slug"] in capitoli:
            continue
        con.execute(
            "INSERT INTO capitoli(listato_id, slug, titolo, ordine) VALUES(?,?,?,?) "
            "ON CONFLICT(listato_id, slug) DO UPDATE SET titolo = excluded.titolo,"
            " ordine = excluded.ordine",
            (lid, v["capitolo_slug"], v["capitolo_titolo"], v["capitolo_ordine"]))
        capitoli[v["capitolo_slug"]] = con.execute(
            "SELECT id FROM capitoli WHERE listato_id = ? AND slug = ?",
            (lid, v["capitolo_slug"])).fetchone()[0]

    spostati = 0
    for v in voci:
        cid = capitoli[v["capitolo_slug"]]
        con.execute(
            "INSERT INTO argomenti(capitolo_id, slug, titolo, ordine) VALUES(?,?,?,?) "
            "ON CONFLICT(capitolo_id, slug) DO UPDATE SET titolo = excluded.titolo,"
            " ordine = excluded.ordine",
            (cid, v["argomento_slug"], v["argomento_titolo"], v["argomento_ordine"]))
        aid = con.execute("SELECT id FROM argomenti WHERE capitolo_id = ? AND slug = ?",
                          (cid, v["argomento_slug"])).fetchone()[0]

        qid = con.execute("SELECT id FROM quesiti WHERE listato_id = ? AND codice_min = ?",
                          (lid, v["codice"])).fetchone()
        if not qid:
            continue
        qid = qid[0]
        con.execute("UPDATE quesiti SET capitolo_id = ?, argomento_id = ? WHERE id = ?",
                    (cid, aid, qid))
        cur = con.execute(
            "UPDATE domande SET capitolo_id = ?, argomento_id = ? WHERE quesito_id = ?",
            (cid, aid, qid))
        spostati += cur.rowcount
        # Le risposte gia' date portano con se' capitolo e argomento: vanno
        # riallineate, altrimenti le statistiche continuerebbero a puntare a
        # righe che stiamo per cancellare.
        con.execute(
            "UPDATE risposte SET capitolo_id = ?, argomento_id = ? WHERE domanda_id IN "
            "(SELECT id FROM domande WHERE quesito_id = ?)", (cid, aid, qid))

    vuoti_a = con.execute(
        "DELETE FROM argomenti WHERE capitolo_id IN (SELECT id FROM capitoli WHERE listato_id = ?)"
        " AND id NOT IN (SELECT DISTINCT argomento_id FROM domande WHERE argomento_id IS NOT NULL)",
        (lid,)).rowcount
    vuoti_c = con.execute(
        "DELETE FROM capitoli WHERE listato_id = ?"
        " AND id NOT IN (SELECT DISTINCT capitolo_id FROM domande WHERE capitolo_id IS NOT NULL)",
        (lid,)).rowcount

    dopo = con.execute(
        "SELECT (SELECT COUNT(*) FROM capitoli WHERE listato_id = ?),"
        "       (SELECT COUNT(*) FROM argomenti a JOIN capitoli c ON c.id = a.capitolo_id"
        "        WHERE c.listato_id = ?)", (lid, lid)).fetchone()
    return {"capitoli": f"{prima[0]} -> {dopo[0]}", "argomenti": f"{prima[1]} -> {dopo[1]}",
            "domande_spostate": spostati, "rimossi_vuoti": vuoti_a + vuoti_c}


def ricostruisci_aggregati(con: sqlite3.Connection) -> int:
    """Riallinea stat_utente_argomento alle risposte, ora che gli argomenti
    sono cambiati. Stessa logica dell'endpoint di manutenzione."""
    con.execute("DELETE FROM stat_utente_argomento")
    cur = con.execute("""
        INSERT INTO stat_utente_argomento(utente_id, argomento_id, capitolo_id, listato_id,
                                          n_risposte, n_errori, tempo_tot_ms, ultima_att, ema_errore)
        SELECT r.utente_id, r.argomento_id, r.capitolo_id, d.listato_id,
               COUNT(*), SUM(CASE WHEN r.corretta = 0 THEN 1 ELSE 0 END),
               COALESCE(SUM(r.tempo_ms), 0), MAX(r.risposto_il),
               1.0 * SUM(CASE WHEN r.corretta = 0 THEN 1 ELSE 0 END) / COUNT(*)
        FROM risposte r JOIN domande d ON d.id = r.domanda_id
        WHERE r.corretta IS NOT NULL AND r.argomento_id IS NOT NULL
        GROUP BY r.utente_id, r.argomento_id""")
    return cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("AC_DB", str(RADICE / "data" / "autoscuola.db")))
    ap.add_argument("--prova", action="store_true", help="non scrive nulla, mostra solo il piano")
    args = ap.parse_args()

    percorso = Path(args.db)
    if not percorso.exists():
        print(f"database non trovato: {percorso}")
        return 1

    sorgenti = sorted(p for p in RAW.glob("*.json") if ".part." not in p.name)
    if not sorgenti:
        print(f"nessuna fonte in {RAW}")
        return 1

    con = sqlite3.connect(percorso)
    con.execute("PRAGMA foreign_keys = ON")
    print(f"database: {percorso}")
    if args.prova:
        print("(prova: nessuna scrittura)")

    try:
        with con:
            for fonte in sorgenti:
                dati = json.loads(fonte.read_text(encoding="utf-8"))
                codice = dati.get("listato")
                if not codice:
                    continue
                esito = applica(con, codice, piano(dati, codice), args.prova)
                dettagli = "  ".join(f"{k}: {v}" for k, v in esito.items())
                print(f"  {codice:8} {dettagli}")
            if not args.prova:
                n = ricostruisci_aggregati(con)
                print(f"  aggregati ricostruiti: {n} righe")
    except sqlite3.Error as e:
        print(f"errore, nessuna modifica salvata: {e}")
        return 1
    finally:
        con.close()

    print("fatto." if not args.prova else "prova conclusa: rilancia senza --prova per applicare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
