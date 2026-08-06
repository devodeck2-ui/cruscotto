"""Scheda dell'autoscuola: dove siamo, come contattarci, chi siamo.

Compare nella schermata dell'allievo. E' la risposta a due domande banali ma
frequentissime in segreteria: "a che ora siete aperti?" e "dove siete?".

I dati stanno nella colonna JSON autoscuole.impostazioni, che esisteva gia'
proprio per questo: informazioni di contorno che cambiano da scuola a scuola
e che non meritano una colonna ciascuna.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..rbac import Principal, current_user, require_admin

router = APIRouter(prefix="/api/scuola", tags=["scuola"])

# Valori generici di partenza: meglio una scheda plausibile e subito
# modificabile che una schermata vuota con scritto "da configurare".
PREDEFINITI = {
    "indirizzo": "Via Roma 1",
    "citta": "Milano (MI)",
    "telefono": "02 1234567",
    "email": "info@autoscuola.it",
    "orari": "Lunedi - Venerdi 9:00-12:30 e 15:00-19:00, Sabato 9:00-12:00",
    "descrizione": ("Autoscuola in centro citta'. Prepariamo agli esami di teoria e "
                    "pratica per le patenti A, B, superiori, CQC e CAP, con lezioni in "
                    "aula, videocorsi e quiz ministeriali sempre aggiornati."),
    "sito": "",
    "mappa": "",
}

CAMPI = tuple(PREDEFINITI)


def _leggi(autoscuola_id):
    r = db.query_one("SELECT ragione_sociale, citta, impostazioni FROM autoscuole WHERE id = ?",
                     (autoscuola_id,))
    if not r:
        raise HTTPException(404, "Autoscuola non trovata")
    try:
        salvate = json.loads(r["impostazioni"] or "{}")
    except (TypeError, ValueError):
        salvate = {}
    scheda = dict(PREDEFINITI)
    # La citta' registrata in anagrafica ha la precedenza sul valore generico,
    # cosi' la scheda e' gia' parzialmente corretta senza toccare niente.
    if r["citta"]:
        scheda["citta"] = r["citta"]
    scheda.update({k: v for k, v in salvate.items() if k in CAMPI and v not in (None, "")})
    scheda["ragione_sociale"] = r["ragione_sociale"]
    return scheda


@router.get("")
def leggi(p: Principal = Depends(current_user)):
    """Visibile a tutti gli utenti dell'autoscuola, allievi compresi."""
    return _leggi(p.autoscuola_id)


class SchedaIn(BaseModel):
    ragione_sociale: str | None = Field(default=None, max_length=120)
    indirizzo: str | None = Field(default=None, max_length=160)
    citta: str | None = Field(default=None, max_length=80)
    telefono: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=120)
    orari: str | None = Field(default=None, max_length=300)
    descrizione: str | None = Field(default=None, max_length=1200)
    sito: str | None = Field(default=None, max_length=200)
    mappa: str | None = Field(default=None, max_length=400)


@router.put("")
def aggiorna(body: SchedaIn, p: Principal = Depends(require_admin)):
    r = db.query_one("SELECT impostazioni FROM autoscuole WHERE id = ?", (p.autoscuola_id,))
    if not r:
        raise HTTPException(404, "Autoscuola non trovata")
    try:
        salvate = json.loads(r["impostazioni"] or "{}")
    except (TypeError, ValueError):
        salvate = {}

    dati = body.model_dump(exclude_none=True)
    if "ragione_sociale" in dati:
        db.execute("UPDATE autoscuole SET ragione_sociale = ? WHERE id = ?",
                   (dati.pop("ragione_sociale").strip(), p.autoscuola_id))
    if "citta" in dati:
        db.execute("UPDATE autoscuole SET citta = ? WHERE id = ?",
                   (dati["citta"].strip(), p.autoscuola_id))

    salvate.update({k: (v.strip() if isinstance(v, str) else v)
                    for k, v in dati.items() if k in CAMPI})
    db.execute("UPDATE autoscuole SET impostazioni = ? WHERE id = ?",
               (json.dumps(salvate, ensure_ascii=False), p.autoscuola_id))
    db.execute("INSERT INTO audit_log(utente_id, autoscuola_id, azione, entita, entita_id) "
               "VALUES(?,?,'modifica_scheda_scuola','autoscuole',?)",
               (p.utente_id, p.autoscuola_id, p.autoscuola_id))
    return _leggi(p.autoscuola_id)
