"""Endpoint dell'AI Tutor."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..rbac import Principal, current_user
from ..services import tutor

router = APIRouter(prefix="/api/tutor", tags=["ai-tutor"])


def _carica_domanda(domanda_id, utente_id):
    """Recupera la domanda SOLO se l'utente l'ha effettivamente affrontata.

    Controllo di autorizzazione essenziale: senza di esso il tutor diventerebbe
    un oracolo interrogabile su qualsiasi domanda del listato, vanificando
    l'esercitazione e moltiplicando i costi.
    """
    row = db.query_one(
        "SELECT d.id, d.testo, d.risposta, i.percorso AS immagine, q.tronco,"
        "       c.titolo AS capitolo, a.titolo AS argomento, l.codice AS listato,"
        "       r.risposta_data, r.corretta "
        "FROM risposte r JOIN domande d ON d.id = r.domanda_id "
        "JOIN listati l        ON l.id = d.listato_id "
        "LEFT JOIN immagini i  ON i.id = d.immagine_id "
        "LEFT JOIN quesiti q   ON q.id = d.quesito_id "
        "LEFT JOIN capitoli c  ON c.id = d.capitolo_id "
        "LEFT JOIN argomenti a ON a.id = d.argomento_id "
        "WHERE r.utente_id = ? AND d.id = ? AND r.risposta_data IS NOT NULL "
        "ORDER BY r.risposto_il DESC LIMIT 1", (utente_id, domanda_id))
    if not row:
        raise HTTPException(404, "Domanda non presente nel tuo storico")
    return dict(row), bool(row["risposta_data"])


def _normalizza(messaggi):
    """Converte al formato neutro {'ruolo','testo'}.

    Le conversazioni salvate prima del multi-fornitore usano il formato di
    Anthropic ({'role','content'}): vengono tradotte al volo invece di essere
    buttate, cosi' nessuno storico va perso.
    """
    fuori = []
    for m in messaggi:
        if "ruolo" in m:
            fuori.append({"ruolo": m["ruolo"], "testo": m.get("testo", "")})
        elif "role" in m:
            contenuto = m.get("content", "")
            if isinstance(contenuto, list):
                contenuto = "".join(c.get("text", "") for c in contenuto if isinstance(c, dict))
            fuori.append({"ruolo": "assistente" if m["role"] in ("assistant", "model") else "utente",
                          "testo": contenuto})
    return fuori


class SpiegaIn(BaseModel):
    domanda_id: int


@router.post("/spiega")
def spiega(body: SpiegaIn, p: Principal = Depends(current_user)):
    dom, data = _carica_domanda(body.domanda_id, p.utente_id)
    try:
        res = tutor.spiega_errore(dom, data, p.autoscuola_id)
    except tutor.TutorError as e:
        raise HTTPException(503, "Tutor AI non disponibile: " + str(e))

    db.execute(
        "INSERT INTO ai_conversazioni(utente_id, domanda_id, messaggi) VALUES(?,?,?)",
        (p.utente_id, body.domanda_id, json.dumps(
            [{"ruolo": "assistente", "testo": res["testo"]}], ensure_ascii=False)))
    return res


class ChatIn(BaseModel):
    domanda_id: int
    conversazione_id: int | None = None
    messaggio: str = Field(min_length=2, max_length=1000)


@router.post("/chat")
def chat(body: ChatIn, p: Principal = Depends(current_user)):
    dom, data = _carica_domanda(body.domanda_id, p.utente_id)
    storia = []
    conv = None
    if body.conversazione_id:
        conv = db.query_one("SELECT id, messaggi FROM ai_conversazioni WHERE id = ? AND utente_id = ?",
                            (body.conversazione_id, p.utente_id))
        if conv:
            storia = _normalizza(json.loads(conv["messaggi"]))
    try:
        risposta = tutor.follow_up(dom, data, storia, body.messaggio, p.autoscuola_id)
    except tutor.TutorError as e:
        raise HTTPException(503, "Tutor AI non disponibile: " + str(e))

    nuovi = (_normalizza(json.loads(conv["messaggi"])) if conv else []) + [
        {"ruolo": "utente", "testo": body.messaggio},
        {"ruolo": "assistente", "testo": risposta}]
    if conv:
        db.execute("UPDATE ai_conversazioni SET messaggi = ?, updated_at = "
                   "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                   (json.dumps(nuovi, ensure_ascii=False), conv["id"]))
        cid = conv["id"]
    else:
        cur = db.execute("INSERT INTO ai_conversazioni(utente_id, domanda_id, messaggi) VALUES(?,?,?)",
                         (p.utente_id, body.domanda_id, json.dumps(nuovi, ensure_ascii=False)))
        cid = cur.lastrowid
    return {"conversazione_id": cid, "testo": risposta}


@router.get("/stato")
def stato(_: Principal = Depends(current_user)):
    """Quale fornitore e' attivo. Utile per diagnosticare senza leggere i log."""
    return tutor.stato()


@router.get("/anteprima-prompt/{domanda_id}")
def anteprima_prompt(domanda_id: int, risposta_data: bool = True,
                     p: Principal = Depends(current_user)):
    """Espone il prompt che verrebbe inviato al modello, immagine esclusa."""
    row = db.query_one(
        "SELECT d.id, d.testo, d.risposta, i.percorso AS immagine, q.tronco,"
        "       c.titolo AS capitolo, a.titolo AS argomento, l.codice AS listato "
        "FROM domande d JOIN listati l ON l.id = d.listato_id "
        "LEFT JOIN immagini i ON i.id = d.immagine_id "
        "LEFT JOIN quesiti q ON q.id = d.quesito_id "
        "LEFT JOIN capitoli c ON c.id = d.capitolo_id "
        "LEFT JOIN argomenti a ON a.id = d.argomento_id WHERE d.id = ?", (domanda_id,))
    if not row:
        raise HTTPException(404, "Domanda inesistente")
    return {"system": tutor.SYSTEM_PROMPT,
            "user": tutor._template_utente(dict(row), risposta_data),
            "immagine_allegata": bool(row["immagine"]),
            "provider": settings.provider_attivo(),
            "modello": settings.modello_attivo(),
            "prompt_version": settings.prompt_version}
