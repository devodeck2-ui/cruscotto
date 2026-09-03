"""Ciclo di vita di una scheda: creazione, risposta, chiusura, correzione."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..rbac import Principal, current_user
from ..services import analytics, generatore, srs
from .patenti import codici_utente

router = APIRouter(prefix="/api/quiz", tags=["quiz"])


class NuovaScheda(BaseModel):
    tipo: str = Field(pattern="^(esercitazione|simulazione|recupero)$")
    listato: str = "B"
    capitoli: list[int] | None = None
    n_domande: int = Field(default=30, ge=5, le=60)


@router.post("/schede")
def crea(body: NuovaScheda, p: Principal = Depends(current_user)):
    lst = db.query_one("SELECT id FROM listati WHERE codice = ? AND attivo = 1", (body.listato,))
    if not lst:
        raise HTTPException(404, "Listato inesistente")
    # Il catalogo mostra all'allievo solo le sue patenti, ma finora nulla
    # impediva di chiedere una scheda di un'altra categoria chiamando l'API
    # direttamente. Lo staff resta libero: gli serve per preparare le lezioni.
    if p.ruolo == "allievo" and body.listato not in codici_utente(p.utente_id):
        raise HTTPException(403, "Questa patente non e' fra le tue")
    try:
        return generatore.crea_scheda(
            p.utente_id, p.autoscuola_id, lst["id"], body.tipo,
            capitoli=body.capitoli, n_domande=body.n_domande)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.get("/schede/{scheda_id}")
def leggi(scheda_id: int, p: Principal = Depends(current_user)):
    try:
        return generatore.carica_scheda(scheda_id, p.utente_id)
    except LookupError as e:
        raise HTTPException(404, str(e))


class RispostaIn(BaseModel):
    posizione: int
    risposta: bool | None = None
    tempo_ms: int = Field(default=0, ge=0, le=600000)
    dubbio: bool = False


@router.post("/schede/{scheda_id}/rispondi")
def rispondi(scheda_id: int, body: RispostaIn, p: Principal = Depends(current_user)):
    """Scrittura atomica: risposta + aggregati + stato SRS + difficolta' globale.

    Tutto in una sola transazione, cosi' non esiste uno stato in cui la risposta
    e' registrata ma le statistiche no. E' l'invariante che rende affidabile la
    dashboard admin.
    """
    with db.transaction() as con:
        s = con.execute(
            "SELECT s.id, s.stato, s.listato_id, s.tipo FROM schede s "
            "WHERE s.id = ? AND s.utente_id = ?", (scheda_id, p.utente_id)).fetchone()
        if not s:
            raise HTTPException(404, "Scheda non trovata")
        if s["stato"] != "in_corso":
            raise HTTPException(409, "Scheda gia' conclusa")

        r = con.execute(
            "SELECT r.id, r.domanda_id, r.risposta_data, r.capitolo_id, r.argomento_id,"
            "       d.risposta FROM risposte r JOIN domande d ON d.id = r.domanda_id "
            "WHERE r.scheda_id = ? AND r.posizione = ?", (scheda_id, body.posizione)).fetchone()
        if not r:
            raise HTTPException(404, "Posizione inesistente nella scheda")

        gia_risposta = r["risposta_data"] is not None
        if body.risposta is None:
            con.execute("UPDATE risposte SET flag_dubbio = ? WHERE id = ?",
                        (1 if body.dubbio else 0, r["id"]))
            return {"ok": True, "salvata": False}

        corretta = 1 if bool(body.risposta) == bool(r["risposta"]) else 0
        con.execute(
            "UPDATE risposte SET risposta_data = ?, corretta = ?, tempo_ms = ?, flag_dubbio = ?,"
            " risposto_il = ? WHERE id = ?",
            (1 if body.risposta else 0, corretta, body.tempo_ms, 1 if body.dubbio else 0,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), r["id"]))

        if not gia_risposta:
            con.execute(
                "UPDATE schede SET n_risposte = n_risposte + 1,"
                " n_errori = n_errori + ? WHERE id = ?", (0 if corretta else 1, scheda_id))
            analytics.registra_risposta(con, p.utente_id, s["listato_id"], r["capitolo_id"],
                                        r["argomento_id"], bool(corretta), body.tempo_ms)
            analytics.aggiorna_difficolta_domanda(con, r["domanda_id"], bool(corretta))
            srs.aggiorna_stato(con, p.utente_id, r["domanda_id"], r["argomento_id"], bool(corretta))
        else:
            # L'allievo ha cambiato idea su una domanda gia' risposta. Il
            # conteggio errori della scheda deve seguirlo, altrimenti il
            # verdetto finale (superata / non superata, calcolato su n_errori)
            # non corrisponde alle risposte che si vedono in griglia.
            # Gli aggregati di lungo periodo restano fermi alla prima risposta:
            # e' quella che misura davvero cosa sapeva l'allievo.
            era_corretta = 1 if bool(r["risposta_data"]) == bool(r["risposta"]) else 0
            if era_corretta != corretta:
                con.execute(
                    "UPDATE schede SET n_errori = MAX(0, n_errori + ?) WHERE id = ?",
                    (-1 if corretta else 1, scheda_id))
                analytics.correggi_risposta(con, p.utente_id, r["argomento_id"],
                                            bool(era_corretta), bool(corretta))

    # La correttezza non viene rivelata durante la simulazione: il client
    # riceve solo la conferma di salvataggio, come all'esame reale.
    if s["tipo"] == "simulazione":
        return {"ok": True, "salvata": True}
    return {"ok": True, "salvata": True, "corretta": bool(corretta)}


class ChiusuraIn(BaseModel):
    durata_sec: int = Field(default=0, ge=0)
    motivo: str = Field(default="completata", pattern="^(completata|scaduta|annullata)$")


@router.post("/schede/{scheda_id}/chiudi")
def chiudi(scheda_id: int, body: ChiusuraIn, p: Principal = Depends(current_user)):
    with db.transaction() as con:
        s = con.execute("SELECT s.*, l.errori_max FROM schede s JOIN listati l ON l.id = s.listato_id "
                        "WHERE s.id = ? AND s.utente_id = ?", (scheda_id, p.utente_id)).fetchone()
        if not s:
            raise HTTPException(404, "Scheda non trovata")
        if s["stato"] != "in_corso":
            return {"gia_chiusa": True, "esito": s["esito"], "n_errori": s["n_errori"]}

        # Le domande lasciate in bianco valgono come errore, esattamente come in sede d'esame.
        non_risposte = con.execute(
            "SELECT COUNT(*) FROM risposte WHERE scheda_id = ? AND risposta_data IS NULL",
            (scheda_id,)).fetchone()[0]
        errori = s["n_errori"] + non_risposte
        superata = 1 if errori <= s["errori_max"] else 0

        con.execute(
            "UPDATE schede SET stato = ?, esito = ?, n_errori = ?, durata_sec = ?,"
            " conclusa_il = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (body.motivo, superata, errori, body.durata_sec, scheda_id))
        analytics.registra_scheda_conclusa(con, p.utente_id, s["tipo"], bool(superata))

    ris = generatore.carica_scheda(scheda_id, p.utente_id, con_soluzioni=True)
    ris["riepilogo"] = {"n_errori": errori, "non_risposte": non_risposte,
                        "errori_max": s["errori_max"], "superata": bool(superata),
                        "durata_sec": body.durata_sec, "n_domande": s["n_domande"],
                        "punteggio_pct": round(100 * (s["n_domande"] - errori) / s["n_domande"], 1)}
    ris["per_capitolo"] = db.rows_to_dicts(db.query(
        "SELECT c.titolo AS capitolo, COUNT(*) AS n,"
        "       SUM(CASE WHEN r.corretta = 0 OR r.corretta IS NULL THEN 1 ELSE 0 END) AS errori "
        "FROM risposte r LEFT JOIN capitoli c ON c.id = r.capitolo_id "
        "WHERE r.scheda_id = ? GROUP BY c.id ORDER BY errori DESC", (scheda_id,)))
    return ris


@router.get("/storico")
def storico(limite: int = 20, p: Principal = Depends(current_user)):
    return db.rows_to_dicts(db.query(
        "SELECT s.id, s.tipo, s.stato, s.esito, s.n_domande, s.n_errori, s.durata_sec,"
        "       s.iniziata_il, s.conclusa_il, l.codice AS listato "
        "FROM schede s JOIN listati l ON l.id = s.listato_id "
        "WHERE s.utente_id = ? ORDER BY s.iniziata_il DESC LIMIT ?", (p.utente_id, limite)))


@router.get("/da-ripassare")
def da_ripassare(p: Principal = Depends(current_user)):
    return {"n_domande": srs.domande_da_ripassare(p.utente_id)}
