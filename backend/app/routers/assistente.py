"""Assistente conversazionale sempre disponibile.

E' il riquadro fluttuante in basso a destra: l'allievo puo' fare una domanda
in qualsiasi momento, senza partire da una domanda ministeriale sbagliata.

DIFFERENZA CON IL TUTOR
    Il tutor (routers/tutoraggio.py) spiega UN errore preciso: riceve la
    domanda ufficiale, l'immagine e la risposta data. Questo assistente e'
    generico: "cosa vuol dire linea continua?", "quanti errori posso fare?",
    "come funziona il ripasso mirato?".

PERCHE' SPARISCE DURANTE IL QUIZ
    Il nascondimento e' deciso dall'interfaccia, ma il vincolo e' anche qui:
    se una scheda dell'utente e' in corso, l'assistente rifiuta di rispondere.
    Se il controllo stesse solo nel browser, basterebbe una richiesta diretta
    all'API per aggirarlo e trasformare l'assistente in un suggeritore
    durante la simulazione d'esame.

MEMORIA
    Solo gli ultimi turni della conversazione, ricostruiti lato server dalla
    tabella ai_conversazioni: il client non puo' iniettare finto storico.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..rbac import Principal, current_user
from ..services import tutor

router = APIRouter(prefix="/api/assistente", tags=["assistente"])

TURNI_MEMORIA = 8          # quattro scambi: contesto sufficiente, costo basso
LIMITE_MESSAGGIO = 800

SYSTEM_ASSISTENTE = """Sei l'assistente di un'autoscuola italiana, dentro l'app con cui gli allievi si preparano all'esame di teoria. Rispondi a chi sta studiando: allievi dai 14 anni in su, spesso alle prime armi.

## Di cosa ti occupi
- Codice della Strada italiano (D.Lgs. 285/1992) e Regolamento (D.P.R. 495/1992): segnali, precedenze, sorpasso, sosta e fermata, velocita', documenti, sanzioni, comportamento in caso di incidente.
- Come si usa l'app: esercitazioni per argomento, simulazione d'esame, ripasso mirato, videocorsi, statistiche.
- Come funziona l'esame: numero di domande, tempo, errori ammessi per ciascuna categoria di patente.

## Come rispondi
- In italiano, con il "tu", in modo diretto e incoraggiante.
- Breve: tre o quattro frasi quando basta. Se la domanda e' ampia, dai la risposta essenziale e proponi di approfondire.
- Concreto: preferisci un esempio pratico a una definizione astratta.
- Se la domanda e' vaga, chiedi una precisazione invece di tirare a indovinare.

## Vincoli
- Non inventare MAI numeri di articolo, limiti di velocita', misure, distanze, importi di sanzioni o dati di cui non sei certo. Meglio descrivere la regola senza citare il numero.
- Se la normativa distingue casi (centro abitato o fuori, categoria del veicolo, patente conseguita da meno di tre anni), dillo: semplificare fino all'errore non aiuta all'esame.
- Resta nel dominio. Se ti chiedono altro (compiti di scuola, questioni personali, argomenti non stradali), riporta gentilmente il discorso alla preparazione dell'esame.
- Non sei un avvocato ne' un medico: per contenziosi, ricorsi o idoneita' sanitaria indirizza all'autoscuola o agli uffici competenti.
- Niente emoji.

## Formato
Testo scorrevole. Grassetto solo sui termini chiave. Elenco puntato al massimo di tre voci, e solo se serve davvero."""


def _chiama(messaggi):
    """Invia la conversazione al fornitore attivo.

    Riusa il livello di trasporto del tutor (stesse chiavi, stesso limite
    orario, stessa gestione degli errori), ma con il proprio prompt.
    """
    provider = settings.provider_attivo()
    if provider == "anthropic":
        payload = {
            "model": settings.ai_model,
            "max_tokens": 600,
            "temperature": 0.3,
            "system": [{"type": "text", "text": SYSTEM_ASSISTENTE,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "assistant" if m["ruolo"] == "assistente" else "user",
                          "content": [{"type": "text", "text": m["testo"]}]}
                         for m in messaggi],
        }
        dati = tutor._http(tutor.API_ANTHROPIC, payload,
                           {"x-api-key": settings.anthropic_api_key,
                            "anthropic-version": "2023-06-01"})
        testo = "".join(b.get("text", "") for b in dati.get("content", [])
                        if b.get("type") == "text").strip()
        uso = dati.get("usage", {})
        return testo, {"input": uso.get("input_tokens", 0),
                       "output": uso.get("output_tokens", 0)}

    if provider == "gemini":
        categorie = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                     "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_ASSISTENTE}]},
            "contents": [{"role": "model" if m["ruolo"] == "assistente" else "user",
                          "parts": [{"text": m["testo"]}]} for m in messaggi],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 600},
            "safetySettings": [{"category": c, "threshold": "BLOCK_ONLY_HIGH"}
                               for c in categorie],
        }
        testo, uso, _ = tutor._chiama_gemini(payload)
        return testo, uso

    raise tutor.TutorError("nessuna chiave configurata: imposta GEMINI_API_KEY "
                           "oppure ANTHROPIC_API_KEY nel file .env")


def _scheda_in_corso(utente_id):
    r = db.query_one(
        "SELECT 1 FROM schede WHERE utente_id = ? AND stato = 'in_corso' "
        "AND iniziata_il > datetime('now', '-3 hours') LIMIT 1", (utente_id,))
    return bool(r)


class DomandaIn(BaseModel):
    messaggio: str = Field(min_length=2, max_length=LIMITE_MESSAGGIO)
    conversazione_id: int | None = None


@router.post("/chiedi")
def chiedi(body: DomandaIn, p: Principal = Depends(current_user)):
    # Vincolo lato server, non solo grafico: durante una scheda aperta
    # l'assistente non risponde, altrimenti diventerebbe un suggeritore.
    if _scheda_in_corso(p.utente_id):
        raise HTTPException(
            409, "Hai una scheda in corso: consegnala e poi potrai chiedermi "
                 "tutto quello che vuoi. Sugli errori ti spiego domanda per domanda.")

    conv = None
    storia = []
    if body.conversazione_id:
        conv = db.query_one(
            "SELECT id, messaggi FROM ai_conversazioni WHERE id = ? AND utente_id = ?",
            (body.conversazione_id, p.utente_id))
        if conv:
            storia = [m for m in json.loads(conv["messaggi"]) if "ruolo" in m][-TURNI_MEMORIA:]

    storia.append({"ruolo": "utente", "testo": body.messaggio.strip()})

    try:
        tutor._rate_limit(p.autoscuola_id)
        testo, uso = _chiama(storia)
    except tutor.TutorError as e:
        raise HTTPException(503, "Assistente non disponibile: " + str(e))
    if not testo:
        raise HTTPException(503, "L'assistente non ha prodotto una risposta")
    tutor._contabilizza(p.autoscuola_id, uso)

    nuovi = storia + [{"ruolo": "assistente", "testo": testo}]
    if conv:
        db.execute("UPDATE ai_conversazioni SET messaggi = ?, updated_at = "
                   "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                   (json.dumps(nuovi, ensure_ascii=False), conv["id"]))
        cid = conv["id"]
    else:
        cur = db.execute(
            "INSERT INTO ai_conversazioni(utente_id, domanda_id, messaggi) VALUES(?, NULL, ?)",
            (p.utente_id, json.dumps(nuovi, ensure_ascii=False)))
        cid = cur.lastrowid
    return {"conversazione_id": cid, "testo": testo}


@router.get("/stato")
def stato(p: Principal = Depends(current_user)):
    """Dice all'interfaccia se puo' mostrare il riquadro e con quale messaggio."""
    return {"disponibile": bool(settings.provider_attivo()),
            "scheda_in_corso": _scheda_in_corso(p.utente_id),
            "provider": settings.provider_attivo()}


@router.get("/conversazione/{conversazione_id}")
def leggi(conversazione_id: int, p: Principal = Depends(current_user)):
    c = db.query_one("SELECT messaggi FROM ai_conversazioni WHERE id = ? AND utente_id = ?",
                     (conversazione_id, p.utente_id))
    if not c:
        raise HTTPException(404, "Conversazione non trovata")
    return {"messaggi": [m for m in json.loads(c["messaggi"]) if "ruolo" in m]}
