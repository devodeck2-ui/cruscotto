"""Videocorsi registrati e lezioni live."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..rbac import Principal, current_user, require_staff
from ..services import notifiche
from .patenti import filtro_sql, utenti_con_patente

router = APIRouter(prefix="/api/video", tags=["video"])


@router.get("/corsi")
def corsi(p: Principal = Depends(current_user)):
    # Chi studia per la B non deve trovarsi in elenco il corso CQC.
    dove, par = ("", [])
    if p.ruolo == "allievo":
        dove, par = filtro_sql(p.utente_id, "l.codice")
    return db.rows_to_dicts(db.query(
        "SELECT c.id, c.titolo, c.descrizione, c.copertina, l.codice AS listato,"
        "       COUNT(v.id) AS n_lezioni,"
        "       SUM(CASE WHEN vv.completata = 1 THEN 1 ELSE 0 END) AS completate "
        "FROM corsi c JOIN listati l ON l.id = c.listato_id "
        "LEFT JOIN lezioni_video v ON v.corso_id = c.id AND v.pubblicata = 1 "
        "LEFT JOIN visione_video vv ON vv.lezione_id = v.id AND vv.utente_id = ? "
        "WHERE c.pubblicato = 1 AND (c.autoscuola_id IS NULL OR c.autoscuola_id = ?)"
        + dove + " GROUP BY c.id ORDER BY c.ordine", [p.utente_id, p.autoscuola_id, *par]))


@router.get("/corsi/{corso_id}/lezioni")
def lezioni(corso_id: int, p: Principal = Depends(current_user)):
    c = db.query_one("SELECT id FROM corsi WHERE id = ? AND pubblicato = 1 "
                     "AND (autoscuola_id IS NULL OR autoscuola_id = ?)",
                     (corso_id, p.autoscuola_id))
    if not c:
        raise HTTPException(404, "Corso non disponibile")
    return db.rows_to_dicts(db.query(
        "SELECT v.id, v.titolo, v.descrizione, v.tipo, v.url, v.durata_sec, v.inizio_live,"
        "       v.stato_live, v.ordine, c.titolo AS capitolo,"
        "       COALESCE(vv.posizione_sec, 0) AS riprendi_da,"
        "       COALESCE(vv.completata, 0) AS completata "
        "FROM lezioni_video v LEFT JOIN capitoli c ON c.id = v.capitolo_id "
        "LEFT JOIN visione_video vv ON vv.lezione_id = v.id AND vv.utente_id = ? "
        "WHERE v.corso_id = ? AND v.pubblicata = 1 ORDER BY v.ordine", (p.utente_id, corso_id)))


class ProgressoIn(BaseModel):
    posizione_sec: int = Field(ge=0)
    delta_sec: int = Field(default=0, ge=0, le=120)
    completata: bool = False


@router.post("/lezioni/{lezione_id}/progresso")
def progresso(lezione_id: int, body: ProgressoIn, p: Principal = Depends(current_user)):
    """Il resume point e' server-side: l'allievo inizia la lezione sul telefono
    in autobus e la riprende dal secondo esatto sul PC di casa."""
    with db.transaction() as con:
        con.execute(
            "INSERT INTO visione_video(utente_id, lezione_id, secondi_visti, posizione_sec,"
            " completata, ultima_il) VALUES(?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(utente_id, lezione_id) DO UPDATE SET "
            " secondi_visti = secondi_visti + ?, posizione_sec = excluded.posizione_sec,"
            " completata = MAX(completata, excluded.completata),"
            " ultima_il = excluded.ultima_il",
            (p.utente_id, lezione_id, body.delta_sec, body.posizione_sec,
             1 if body.completata else 0, body.delta_sec))
        if body.delta_sec:
            con.execute(
                "INSERT INTO stat_utente_giorno(utente_id, giorno, minuti_video) "
                "VALUES(?, date('now'), ?) ON CONFLICT(utente_id, giorno) DO UPDATE SET "
                " minuti_video = minuti_video + ?",
                (p.utente_id, body.delta_sec // 60, body.delta_sec // 60))
    return {"ok": True}


class LiveIn(BaseModel):
    stato: str = Field(pattern="^(programmata|in_onda|conclusa)$")


@router.post("/lezioni/{lezione_id}/stato-live")
def stato_live(lezione_id: int, body: LiveIn, p: Principal = Depends(require_staff)):
    # Si verifica che la diretta appartenga alla scuola di chi chiama (o sia
    # di catalogo globale) prima di toccarla o di avvisare qualcuno: l'id
    # arriva dall'URL, non ci si puo' fidare che sia dei "propri" allievi.
    riga = db.query_one(
        "SELECT v.titolo, l.codice AS listato FROM lezioni_video v "
        "JOIN corsi c ON c.id = v.corso_id JOIN listati l ON l.id = c.listato_id "
        "WHERE v.id = ? AND v.tipo = 'live' AND (c.autoscuola_id IS NULL OR c.autoscuola_id = ?)",
        (lezione_id, p.autoscuola_id))
    if not riga:
        raise HTTPException(404, "Diretta non trovata")

    db.execute("UPDATE lezioni_video SET stato_live = ? WHERE id = ?", (body.stato, lezione_id))

    if body.stato == "in_onda":
        notifiche.notifica_utenti(
            utenti_con_patente(p.autoscuola_id, riga["listato"]),
            "diretta_iniziata", f"E\' iniziata: {riga['titolo']}",
            "Collegati ora dalla sezione Videocorsi.", "/#/video")
    return {"ok": True}
