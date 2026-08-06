"""Generazione delle schede quiz.

Tre modalita', un solo motore:

  * esercitazione : campionamento uniforme su un sottoinsieme di capitoli
  * simulazione   : replica la composizione della scheda d'esame ministeriale
  * recupero      : campionamento pesato dagli argomenti critici (vedi srs.py)

Nota sul campionamento: `ORDER BY RANDOM() LIMIT n` su 30.000 righe costringe
SQLite a materializzare e ordinare l'intero set filtrato. Con l'indice di
copertura ix_domande_sel il costo resta accettabile (<15 ms), ma per la
simulazione - che gira sotto timer e deve essere istantanea - si usa un
campionamento a due fasi: si estraggono gli id con una scansione dell'indice
e si sceglie in Python, poi si caricano le sole righe selezionate.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

from .. import db


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _campiona_ids(listato_id: int, capitoli: list[int] | None, n: int,
                  escludi: set[int] | None = None) -> list[int]:
    sql = "SELECT id FROM domande WHERE listato_id = ? AND attiva = 1"
    par: list = [listato_id]
    if capitoli:
        sql += f" AND capitolo_id IN ({','.join('?' * len(capitoli))})"
        par += capitoli
    ids = [r[0] for r in db.query(sql, par)]
    if escludi:
        ids = [i for i in ids if i not in escludi]
    random.shuffle(ids)
    return ids[:n]


def _composizione_esame(listato_id: int, n: int) -> list[int]:
    """La scheda ministeriale non e' un campione casuale piatto: estrae quesiti
    distribuiti su tutti i capitoli. Si replica la proporzionalita' rispetto
    alla numerosita' del listato, garantendo almeno una domanda per capitolo
    finche' il budget lo consente."""
    caps = db.query(
        "SELECT capitolo_id, COUNT(*) AS n FROM domande "
        "WHERE listato_id = ? AND attiva = 1 AND capitolo_id IS NOT NULL "
        "GROUP BY capitolo_id ORDER BY n DESC", (listato_id,))
    if not caps:
        return _campiona_ids(listato_id, None, n)

    totale = sum(c["n"] for c in caps)
    quote: dict[int, int] = {}
    residuo = n
    for c in caps:
        q = max(1, round(n * c["n"] / totale)) if residuo > 0 else 0
        q = min(q, residuo)
        quote[c["capitolo_id"]] = q
        residuo -= q
    # distribuisce l'eventuale residuo sui capitoli piu' popolosi
    i = 0
    while residuo > 0 and caps:
        cid = caps[i % len(caps)]["capitolo_id"]
        quote[cid] = quote.get(cid, 0) + 1
        residuo -= 1
        i += 1

    scelti: list[int] = []
    for cid, q in quote.items():
        if q:
            scelti += _campiona_ids(listato_id, [cid], q)
    random.shuffle(scelti)
    return scelti[:n]


def crea_scheda(utente_id: int, autoscuola_id: int, listato_id: int, tipo: str,
                capitoli: list[int] | None = None, n_domande: int | None = None,
                parametri: dict | None = None) -> dict:
    reg = db.query_one("SELECT domande_esame, minuti_esame, errori_max FROM listati WHERE id = ?",
                       (listato_id,))
    parametri = dict(parametri or {})

    if tipo == "simulazione":
        n = reg["domande_esame"]
        limite = reg["minuti_esame"] * 60
        ids = _composizione_esame(listato_id, n)
    elif tipo == "recupero":
        from .srs import seleziona_recupero
        n = n_domande or 30
        ids = seleziona_recupero(utente_id, listato_id, n)
        limite = None
        parametri["algoritmo"] = "srs-ema"
    else:
        n = n_domande or 30
        ids = _campiona_ids(listato_id, capitoli, n)
        limite = None

    if not ids:
        raise ValueError("Nessuna domanda disponibile per i filtri selezionati")

    parametri.update({"capitoli": capitoli or [], "richieste": n})

    with db.transaction() as con:
        cur = con.execute(
            "INSERT INTO schede(utente_id, autoscuola_id, listato_id, tipo, parametri,"
            " n_domande, limite_sec, iniziata_il) VALUES(?,?,?,?,?,?,?,?)",
            (utente_id, autoscuola_id, listato_id, tipo, json.dumps(parametri, ensure_ascii=False),
             len(ids), limite, _now()))
        scheda_id = cur.lastrowid
        righe = con.execute(
            f"SELECT id, capitolo_id, argomento_id FROM domande WHERE id IN ({','.join('?'*len(ids))})",
            ids).fetchall()
        meta = {r["id"]: (r["capitolo_id"], r["argomento_id"]) for r in righe}
        con.executemany(
            "INSERT INTO risposte(scheda_id, domanda_id, utente_id, capitolo_id, argomento_id,"
            " posizione) VALUES(?,?,?,?,?,?)",
            [(scheda_id, did, utente_id, meta[did][0], meta[did][1], pos)
             for pos, did in enumerate(ids, start=1)])

    return {"scheda_id": scheda_id, "n_domande": len(ids), "limite_sec": limite,
            "errori_max": reg["errori_max"], "tipo": tipo}


def carica_scheda(scheda_id: int, utente_id: int, *, con_soluzioni: bool = False) -> dict:
    """Il payload consegnato al client NON contiene la risposta corretta finche'
    la scheda e' in corso: impedisce di leggere le soluzioni dal traffico di rete."""
    s = db.query_one(
        "SELECT s.*, l.codice AS listato, l.errori_max FROM schede s "
        "JOIN listati l ON l.id = s.listato_id WHERE s.id = ? AND s.utente_id = ?",
        (scheda_id, utente_id))
    if not s:
        raise LookupError("Scheda inesistente o non appartenente all'utente")

    mostra = con_soluzioni or s["stato"] != "in_corso"
    righe = db.query(
        "SELECT r.posizione, r.domanda_id, r.risposta_data, r.corretta, r.tempo_ms,"
        "       r.flag_dubbio, d.testo, d.risposta, i.percorso AS immagine,"
        "       c.titolo AS capitolo, a.titolo AS argomento, q.tronco "
        "FROM risposte r "
        "JOIN domande d       ON d.id = r.domanda_id "
        "LEFT JOIN immagini i ON i.id = d.immagine_id "
        "LEFT JOIN capitoli c ON c.id = d.capitolo_id "
        "LEFT JOIN argomenti a ON a.id = d.argomento_id "
        "LEFT JOIN quesiti q  ON q.id = d.quesito_id "
        "WHERE r.scheda_id = ? ORDER BY r.posizione", (scheda_id,))

    domande = []
    for r in righe:
        item = {"posizione": r["posizione"], "domanda_id": r["domanda_id"],
                "testo": r["testo"], "tronco": r["tronco"], "immagine": r["immagine"],
                "capitolo": r["capitolo"], "argomento": r["argomento"],
                "risposta_data": r["risposta_data"], "flag_dubbio": bool(r["flag_dubbio"]),
                "tempo_ms": r["tempo_ms"]}
        if mostra:
            item["risposta_corretta"] = r["risposta"]
            item["corretta"] = r["corretta"]
        domande.append(item)

    return {"scheda": {k: s[k] for k in s.keys()}, "domande": domande, "soluzioni_visibili": mostra}
