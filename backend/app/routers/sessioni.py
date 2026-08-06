"""Tracciamento del tempo di utilizzo.

Il tempo "davanti all'app" e' la metrica che l'autoscuola usa per capire chi
sta studiando davvero. Misurarla male e' peggio che non misurarla: una tab
dimenticata aperta per otto ore falserebbe ogni classifica.

Protocollo:
  * il client apre una sessione all'ingresso (POST /apri)
  * invia un heartbeat ogni 30 s con la sezione attiva, SOLO se la pagina e'
    visibile (Page Visibility API) e c'e' stata interazione recente
  * il server accumula il delta fra due ping consecutivi, ma scarta i delta
    superiori alla soglia di inattivita': se il client tace 20 minuti, quei
    20 minuti non vengono conteggiati
  * il reaper chiude le sessioni orfane (browser chiuso, rete caduta)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..rbac import Principal, current_user
from ..security import hash_opaque

router = APIRouter(prefix="/api/sessioni", tags=["tracking"])


class AperturaIn(BaseModel):
    piattaforma: str = Field(default="web", max_length=20)


@router.post("/apri")
def apri(body: AperturaIn, request: Request, p: Principal = Depends(current_user)):
    aperta = db.query_one(
        "SELECT id FROM sessioni_app WHERE utente_id = ? AND fine IS NULL "
        "AND ultimo_ping >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
        (p.utente_id, f"-{settings.inattivita_sec} seconds"))
    if aperta:
        return {"sessione_id": aperta["id"], "ripresa": True}

    cur = db.execute(
        "INSERT INTO sessioni_app(utente_id, autoscuola_id, piattaforma, user_agent, ip_hash) "
        "VALUES(?,?,?,?,?)",
        (p.utente_id, p.autoscuola_id, body.piattaforma,
         request.headers.get("user-agent", "")[:200],
         hash_opaque(request.client.host if request.client else "")))
    return {"sessione_id": cur.lastrowid, "ripresa": False,
            "heartbeat_sec": settings.heartbeat_sec}


class PingIn(BaseModel):
    sessione_id: int
    sezione: str = Field(default="home", max_length=30)
    delta_sec: int = Field(default=30, ge=0, le=120)


@router.post("/ping")
def ping(body: PingIn, p: Principal = Depends(current_user)):
    s = db.query_one("SELECT id, ultimo_ping, breakdown FROM sessioni_app "
                     "WHERE id = ? AND utente_id = ? AND fine IS NULL",
                     (body.sessione_id, p.utente_id))
    if not s:
        return {"ok": False, "riapri": True}

    ultimo = datetime.fromisoformat(s["ultimo_ping"].replace("Z", "+00:00"))
    trascorso = (datetime.now(timezone.utc) - ultimo).total_seconds()
    # Delta accettato = il minore fra tempo reale e delta dichiarato, e comunque
    # mai oltre la soglia di inattivita'. Regge sia il client onesto sia quello
    # che tenta di gonfiare le proprie ore.
    delta = int(max(0, min(trascorso, body.delta_sec, settings.inattivita_sec)))

    breakdown = json.loads(s["breakdown"])
    breakdown[body.sezione] = breakdown.get(body.sezione, 0) + delta

    with db.transaction() as con:
        con.execute(
            "UPDATE sessioni_app SET ultimo_ping = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            " durata_sec = durata_sec + ?, breakdown = ? WHERE id = ?",
            (delta, json.dumps(breakdown), body.sessione_id))
        con.execute(
            "INSERT INTO stat_utente_giorno(utente_id, giorno, secondi_app) VALUES(?, date('now'), ?) "
            "ON CONFLICT(utente_id, giorno) DO UPDATE SET secondi_app = secondi_app + ?",
            (p.utente_id, delta, delta))
    return {"ok": True, "conteggiati_sec": delta}


@router.post("/chiudi")
def chiudi(body: PingIn, p: Principal = Depends(current_user)):
    db.execute("UPDATE sessioni_app SET fine = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
               " chiusa_da = 'logout' WHERE id = ? AND utente_id = ? AND fine IS NULL",
               (body.sessione_id, p.utente_id))
    return {"ok": True}


def reaper() -> int:
    """Chiude le sessioni silenti. Da schedulare ogni minuto."""
    cur = db.execute(
        "UPDATE sessioni_app SET fine = ultimo_ping, chiusa_da = 'reaper' "
        "WHERE fine IS NULL AND ultimo_ping < datetime('now', ?)",
        (f"-{settings.inattivita_sec} seconds",))
    return cur.rowcount
