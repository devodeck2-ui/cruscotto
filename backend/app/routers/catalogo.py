"""Catalogo ministeriale: listati, capitoli, ricerca full-text."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import db
from ..rbac import Principal, current_user
from .patenti import filtro_sql

router = APIRouter(prefix="/api/catalogo", tags=["catalogo"])


def _espressione_fts(testo: str) -> str:
    """Trasforma il testo digitato in un'espressione FTS5 innocua.

    FTS5 legge come sintassi anche il trattino, le parentesi e le parole
    OR/AND/NOT/NEAR: bastava un "precedenza-" o una "OR" in mezzo alla frase
    perche' la ricerca cadesse con un errore 500. Si estraggono quindi le sole
    parole e si passa ognuna fra virgolette, come termine letterale.
    """
    parole = re.findall(r"\w+", testo, flags=re.UNICODE)
    return " ".join('"%s"' % p for p in parole)


@router.get("/listati")
def listati(p: Principal = Depends(current_user)):
    # L'allievo vede solo le patenti a cui e' iscritto: mostrargli tutti e
    # nove i listati, CQC e CAP compresi, e' solo confusione.
    dove, par = ("", [])
    if p.ruolo == "allievo":
        dove, par = filtro_sql(p.utente_id, "l.codice")
    return db.rows_to_dicts(db.query(
        "SELECT l.id, l.codice, l.nome, l.domande_esame, l.minuti_esame, l.errori_max,"
        "       (SELECT COUNT(*) FROM domande d WHERE d.listato_id = l.id AND d.attiva = 1) AS n_domande "
        "FROM listati l WHERE l.attivo = 1" + dove + " "
        "ORDER BY (SELECT COUNT(*) FROM domande d WHERE d.listato_id = l.id) DESC", par))


@router.get("/capitoli")
def capitoli(listato: str = Query("B"), p: Principal = Depends(current_user)):
    return db.rows_to_dicts(db.query(
        "SELECT c.id, c.slug, c.titolo, c.ordine,"
        "       COUNT(d.id) AS n_domande,"
        "       COALESCE(s.n_risposte, 0) AS mie_risposte,"
        "       COALESCE(s.n_errori, 0)   AS miei_errori,"
        "       ROUND(100.0 * COALESCE(s.n_errori,0) / NULLIF(s.n_risposte,0), 1) AS tasso_errore_pct "
        "FROM capitoli c "
        "JOIN listati l ON l.id = c.listato_id AND l.codice = ? "
        "LEFT JOIN domande d ON d.capitolo_id = c.id AND d.attiva = 1 "
        "LEFT JOIN (SELECT capitolo_id, SUM(n_risposte) AS n_risposte, SUM(n_errori) AS n_errori "
        "           FROM stat_utente_argomento WHERE utente_id = ? GROUP BY capitolo_id) s "
        "       ON s.capitolo_id = c.id "
        "GROUP BY c.id HAVING n_domande > 0 ORDER BY c.ordine, c.titolo",
        (listato, p.utente_id)))


@router.get("/cerca")
def cerca(q: str = Query(min_length=3), listato: str = "B", limite: int = 30,
          _: Principal = Depends(current_user)):
    """Ricerca full-text FTS5: utile all'allievo per rivedere una domanda vista
    in aula e all'istruttore per costruire una spiegazione mirata."""
    espressione = _espressione_fts(q)
    if not espressione:
        return []
    return db.rows_to_dicts(db.query(
        "SELECT d.id, d.testo, d.risposta, c.titolo AS capitolo, i.percorso AS immagine "
        "FROM domande_fts f JOIN domande d ON d.id = f.rowid "
        "JOIN listati l ON l.id = d.listato_id AND l.codice = ? "
        "LEFT JOIN capitoli c ON c.id = d.capitolo_id "
        "LEFT JOIN immagini i ON i.id = d.immagine_id "
        "WHERE domande_fts MATCH ? ORDER BY rank LIMIT ?",
        (listato, espressione, limite)))
