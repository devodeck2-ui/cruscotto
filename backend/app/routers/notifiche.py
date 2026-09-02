"""Notifiche: iscrizione ai push del browser e storico personale.

Il permesso di notifica e la sottoscrizione (endpoint + chiavi del
dispositivo) li dà il browser: qui si salva solo dove recapitarle e si tiene
lo storico per il campanellino nell'interfaccia.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import db
from ..config import settings
from ..rbac import Principal, current_user

router = APIRouter(prefix="/api/notifiche", tags=["notifiche"])


@router.get("/chiave-pubblica")
def chiave_pubblica():
    """Chiave VAPID pubblica: il browser ne ha bisogno per iscriversi al
    push. Non è un segreto (viaggia comunque dentro ogni sottoscrizione),
    quindi l'endpoint non richiede login."""
    return {"chiave": settings.vapid_public_key or None}


class IscrizioneIn(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str | None = None


@router.post("/iscrizione")
def iscrivi(body: IscrizioneIn, p: Principal = Depends(current_user)):
    """Salva (o aggiorna) l'iscrizione push di questo dispositivo per
    l'utente che ha effettuato il login."""
    db.execute(
        "INSERT INTO push_subscription(utente_id, endpoint, p256dh, auth, user_agent) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET utente_id = excluded.utente_id, "
        "  p256dh = excluded.p256dh, auth = excluded.auth",
        (p.utente_id, body.endpoint, body.p256dh, body.auth, (body.user_agent or "")[:200]))
    return {"ok": True}


class DisiscrizioneIn(BaseModel):
    endpoint: str


@router.post("/disiscrizione")
def disiscrivi(body: DisiscrizioneIn, p: Principal = Depends(current_user)):
    db.execute("DELETE FROM push_subscription WHERE endpoint = ? AND utente_id = ?",
               (body.endpoint, p.utente_id))
    return {"ok": True}


@router.get("")
def elenco(p: Principal = Depends(current_user)):
    """Ultime notifiche dell'utente, per il campanellino: funziona anche per
    chi non ha mai dato il permesso push."""
    righe = db.rows_to_dicts(db.query(
        "SELECT id, tipo, titolo, corpo, url, letta, created_at FROM notifica "
        "WHERE utente_id = ? ORDER BY created_at DESC LIMIT 30", (p.utente_id,)))
    non_lette = db.query_one(
        "SELECT COUNT(*) AS n FROM notifica WHERE utente_id = ? AND letta = 0",
        (p.utente_id,))["n"]
    return {"notifiche": righe, "non_lette": non_lette}


@router.post("/segna-lette")
def segna_lette(p: Principal = Depends(current_user)):
    db.execute("UPDATE notifica SET letta = 1 WHERE utente_id = ? AND letta = 0", (p.utente_id,))
    return {"ok": True}
