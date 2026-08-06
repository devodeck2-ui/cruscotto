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


def get_conn() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(settings.db_path, check_same_thread=False, timeout=10)
        con.row_factory = sqlite3.Row
        for p in PRAGMAS:
            con.execute(p)
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
