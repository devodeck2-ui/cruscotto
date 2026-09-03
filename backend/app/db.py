"""Accesso a SQLite: pool per-thread, PRAGMA di performance, helper transazionali.

SQLite in modalita' WAL regge comodamente le migliaia di scritture/minuto di
una rete di autoscuole (le scritture sono piccole e serializzate, le letture
non bloccano). Il livello di accesso e' volutamente sottile e centralizzato:
il giorno in cui il carico giustifica Postgres, si sostituisce questo modulo
e le query (SQL standard) restano valide.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .config import settings

_local = threading.local()

PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA cache_size = -64000",     # 64 MB di page cache
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 268435456",   # 256 MB memory-mapped I/O
]


SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Colonne aggiunte a tabelle preesistenti dopo il primo build del database.
# Ri-eseguire schema.sql (sotto) crea da solo le tabelle NUOVE, perche' ogni
# CREATE TABLE e' IF NOT EXISTS; non aggiunge pero' colonne a una tabella che
# esiste gia', quindi quelle vanno elencate qui ed applicate con ALTER TABLE.
COLONNE_AGGIUNTE = {
    "utenti": [
        ("username", "TEXT"),
        ("listati_extra", "TEXT"),
        ("indirizzo", "TEXT"),
        ("ore_acquistate", "INTEGER NOT NULL DEFAULT 0"),
        ("importo_pagato", "REAL"),
        ("note_admin", "TEXT"),
        ("data_iscrizione", "TEXT"),
        # Classe di appartenenza: un allievo per volta ne ha una sola, e puo'
        # non averne (iscritto ma non ancora assegnato). Vedi routers/classi.py.
        ("classe_id", "INTEGER REFERENCES classi(id) ON DELETE SET NULL"),
    ],
}


# Regole d'esame corrette per i database creati prima della verifica sulle
# schede ministeriali. Si aggiornano solo se il valore e' ancora quello vecchio
# e sbagliato, cosi' un'autoscuola che li avesse gia' sistemati a mano non si
# vede sovrascrivere la sua configurazione.
REGOLE_ESAME = {
    # codice: (vecchio errato, nuovo corretto) su (domande, minuti, errori)
    "CQC":     ((60, 90, 6), (70, 90, 7)),
    "REV_CQC": ((60, 90, 6), (70, 90, 7)),
    "CAP":     ((30, 30, 3), (20, 30, 2)),
    "AM":      ((30, 20, 3), (30, 25, 3)),
    "REV_AM":  ((30, 20, 3), (30, 25, 3)),
}


def _allinea_regole_esame(con: sqlite3.Connection) -> None:
    for codice, (vecchio, nuovo) in REGOLE_ESAME.items():
        try:
            con.execute(
                "UPDATE listati SET domande_esame = ?, minuti_esame = ?, errori_max = ? "
                "WHERE codice = ? AND domande_esame = ? AND minuti_esame = ? AND errori_max = ?",
                (*nuovo, codice, *vecchio))
        except sqlite3.Error as e:
            print(f"! regole esame per {codice}: {e}")


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Allinea un database gia' esistente all'ultima versione di schema.sql.

    Due meccanismi complementari, eseguiti una volta per connessione:
      1. si ri-esegue lo schema per intero: tabelle, indici e viste NUOVI
         vengono creati (tutte le istruzioni sono IF NOT EXISTS, quindi e'
         un no-op su cio' che esiste gia' - vedi es. aula_slot/aula_lezione/
         aula_presenza in routers/gestione.py, introdotte dopo il primo build);
      2. per le colonne NUOVE su tabelle esistenti (CREATE TABLE IF NOT
         EXISTS non le aggiunge da sola) si usa un ALTER TABLE esplicito,
         elencato in COLONNE_AGGIUNTE sopra.

    Non deve mai impedire l'avvio del server: in caso di errore si registra
    e si prosegue, cosi' un problema di migrazione non blocca l'intera app.
    """
    try:
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    except sqlite3.Error as e:
        print(f"! _ensure_schema: rieseguire schema.sql ha dato un errore (si prosegue comunque): {e}")

    _allinea_regole_esame(con)

    for tabella, colonne in COLONNE_AGGIUNTE.items():
        try:
            esistenti = {r[1] for r in con.execute(f"PRAGMA table_info({tabella})").fetchall()}
        except sqlite3.Error:
            continue
        for nome, tipo in colonne:
            if nome not in esistenti:
                try:
                    con.execute(f"ALTER TABLE {tabella} ADD COLUMN {nome} {tipo}")
                except sqlite3.Error as e:
                    print(f"! _ensure_schema: impossibile aggiungere {tabella}.{nome}: {e}")
    _assegna_username(con)
    _migra_tipi_notifica(con)
    con.commit()


def _assegna_username(con: sqlite3.Connection) -> None:
    """Da' un nome utente a chi non ce l'ha ancora.

    Nell'app si entra con lo username, non con l'email. Gli account nati prima
    che la colonna esistesse - l'admin per primo - resterebbero senza, quindi
    glielo si costruisce qui con la stessa regola dell'iscrizione: iniziale del
    nome, punto, cognome, piu' un numero progressivo se il nome e' gia' preso.
    Gira una volta sola: al secondo avvio non trova piu' nessuno da sistemare.
    """
    import re as _re
    import unicodedata as _ud
    try:
        senza = con.execute(
            "SELECT id, nome, cognome, email FROM utenti "
            "WHERE username IS NULL OR trim(username) = ''").fetchall()
        if not senza:
            return
        presi = {r[0].lower() for r in con.execute(
            "SELECT username FROM utenti "
            "WHERE username IS NOT NULL AND trim(username) <> ''").fetchall()}
    except sqlite3.Error as e:
        print(f"! _assegna_username: {e}")
        return
    for id_utente, nome, cognome, email in senza:
        grezzo = (f"{nome[:1]}.{cognome}" if nome and cognome
                  else (nome or cognome or (email or "").split("@")[0]))
        grezzo = _ud.normalize("NFKD", grezzo).encode("ascii", "ignore").decode()
        base = _re.sub(r"[^a-zA-Z0-9.]+", "", grezzo).lower()[:24] or "utente"
        candidato, n = base, 1
        while candidato in presi:
            n += 1
            candidato = f"{base}{n}"
        presi.add(candidato)
        try:
            con.execute("UPDATE utenti SET username = ? WHERE id = ?", (candidato, id_utente))
            print(f"  nome utente assegnato: {candidato} (utente {id_utente}, {email})")
        except sqlite3.Error as e:
            print(f"! _assegna_username sull'utente {id_utente}: {e}")


def _migra_tipi_notifica(con: sqlite3.Connection) -> None:
    """Allarga il vincolo su notifica.tipo quando compaiono tipi nuovi.

    SQLite non sa modificare un CHECK con un ALTER: l'unica strada e'
    ricostruire la tabella. Si fa solo se il vincolo attuale non conosce
    ancora l'ultimo tipo introdotto, quindi e' un no-op ad ogni avvio
    successivo. Lo storico delle notifiche viene ricopiato per intero.
    """
    try:
        riga = con.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                           "AND name='notifica'").fetchone()
    except sqlite3.Error:
        return
    if not riga or not riga[0] or "promemoria_studio" in riga[0]:
        return
    try:
        con.executescript("""
            PRAGMA foreign_keys = OFF;
            BEGIN;
            CREATE TABLE notifica_nuova (
                id           INTEGER PRIMARY KEY,
                utente_id    INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
                tipo         TEXT    NOT NULL CHECK (tipo IN ('lezione_programmata','diretta_iniziata','promemoria_studio')),
                titolo       TEXT    NOT NULL,
                corpo        TEXT,
                url          TEXT,
                letta        INTEGER NOT NULL DEFAULT 0 CHECK (letta IN (0,1)),
                inviata_push INTEGER NOT NULL DEFAULT 0 CHECK (inviata_push IN (0,1)),
                created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            INSERT INTO notifica_nuova
                SELECT id, utente_id, tipo, titolo, corpo, url, letta, inviata_push, created_at
                FROM notifica;
            DROP TABLE notifica;
            ALTER TABLE notifica_nuova RENAME TO notifica;
            CREATE INDEX IF NOT EXISTS ix_notifica_utente ON notifica(utente_id, created_at DESC);
            COMMIT;
            PRAGMA foreign_keys = ON;
        """)
        print("_ensure_schema: tabella notifica ricostruita per accogliere i promemoria di studio")
    except sqlite3.Error as e:
        print(f"! _ensure_schema: ricostruzione di notifica non riuscita (si prosegue): {e}")


def get_conn() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=10)
        con.row_factory = sqlite3.Row
        for p in PRAGMAS:
            con.execute(p)
        _ensure_schema(con)
        _local.con = con
    return con


@contextmanager
def transaction():
    """Transazione esplicita: BEGIN IMMEDIATE evita i deadlock da upgrade di lock."""
    con = get_conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    con = get_conn()
    cur = con.execute(sql, tuple(params))
    con.commit()
    return cur


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
