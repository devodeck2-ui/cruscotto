"""Dashboard amministratore: CRM didattico sul proprio tenant.

Ogni endpoint filtra su p.autoscuola_id preso dal token. Un admin non puo'
in alcun modo raggiungere allievi di un'altra autoscuola: non esiste un
parametro che glielo consenta.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from .. import db
from ..rbac import Principal, require_admin, require_staff
from ..security import hash_password
from ..services import analytics

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _verifica_appartenenza(utente_id: int, autoscuola_id: int) -> None:
    r = db.query_one("SELECT 1 FROM utenti WHERE id = ? AND autoscuola_id = ?",
                     (utente_id, autoscuola_id))
    if not r:
        raise HTTPException(404, "Allievo non trovato")


@router.get("/panoramica")
def panoramica(p: Principal = Depends(require_staff)):
    return {
        "totali": analytics.panoramica_tenant(p.autoscuola_id),
        "colli_di_bottiglia": analytics.colli_di_bottiglia(p.autoscuola_id),
        "domande_critiche": analytics.domande_piu_sbagliate(p.autoscuola_id, 15),
        "attivita_recente": db.rows_to_dicts(db.query(
            "SELECT s.id, s.tipo, s.esito, s.n_errori, s.n_domande, s.conclusa_il,"
            "       u.nome || ' ' || u.cognome AS allievo "
            "FROM schede s JOIN utenti u ON u.id = s.utente_id "
            "WHERE s.autoscuola_id = ? AND s.stato = 'completata' "
            "ORDER BY s.conclusa_il DESC LIMIT 15", (p.autoscuola_id,))),
        "consumo_ai": db.rows_to_dicts(db.query(
            "SELECT finestra, n_chiamate, token_in, token_out FROM ai_consumo "
            "WHERE autoscuola_id = ? ORDER BY finestra DESC LIMIT 24", (p.autoscuola_id,))),
    }


@router.get("/allievi")
def allievi(ordina: str = "prontezza", p: Principal = Depends(require_staff)):
    righe = db.rows_to_dicts(db.query(
        "SELECT v.*, u.ultimo_accesso, "
        "  (SELECT COUNT(*) FROM sessioni_app sa WHERE sa.utente_id = v.utente_id) AS n_sessioni "
        "FROM v_progresso_allievo v JOIN utenti u ON u.id = v.utente_id "
        "JOIN ruoli r ON r.id = u.ruolo_id "
        "WHERE v.autoscuola_id = ? AND r.codice = 'allievo' AND u.attivo = 1",
        (p.autoscuola_id,)))
    for r in righe:
        r["prontezza"] = analytics.indice_prontezza(r["utente_id"])
        r["ore"] = round((r.get("secondi_totali") or 0) / 3600, 1)
    chiavi = {"prontezza": lambda x: -x["prontezza"]["punteggio"],
              "errori": lambda x: -(x.get("tasso_errore_pct") or 0),
              "tempo": lambda x: -(x.get("secondi_totali") or 0),
              "nome": lambda x: x["nominativo"]}
    righe.sort(key=chiavi.get(ordina, chiavi["prontezza"]))
    return righe


@router.get("/allievi/{utente_id}")
def dettaglio_allievo(utente_id: int, p: Principal = Depends(require_staff)):
    _verifica_appartenenza(utente_id, p.autoscuola_id)
    u = db.query_one("SELECT * FROM v_progresso_allievo WHERE utente_id = ?", (utente_id,))
    lst = db.query_one("SELECT l.id FROM listati l JOIN utenti u ON u.listato_target = l.codice "
                       "WHERE u.id = ?", (utente_id,))
    return {
        "profilo": dict(u) if u else {},
        "prontezza": analytics.indice_prontezza(utente_id),
        "criticita": analytics.criticita_utente(utente_id, 15),
        "capitoli": analytics.riepilogo_capitoli(utente_id, lst["id"]) if lst else [],
        "serie": analytics.serie_temporale(utente_id, 60),
        "schede": db.rows_to_dicts(db.query(
            "SELECT id, tipo, stato, esito, n_domande, n_errori, durata_sec, iniziata_il "
            "FROM schede WHERE utente_id = ? ORDER BY iniziata_il DESC LIMIT 30", (utente_id,))),
        "sessioni": db.rows_to_dicts(db.query(
            "SELECT inizio, fine, durata_sec, piattaforma, breakdown FROM sessioni_app "
            "WHERE utente_id = ? ORDER BY inizio DESC LIMIT 20", (utente_id,))),
    }


class NuovoAllievo(BaseModel):
    email: EmailStr
    nome: str = Field(min_length=1, max_length=60)
    cognome: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=8, max_length=128)
    listato_target: str = "B"
    data_esame: str | None = None


@router.post("/allievi")
def crea_allievo(body: NuovoAllievo, p: Principal = Depends(require_admin)):
    # Lo username si genera anche qui: senza, l'allievo creato da questa via
    # non avrebbe con cosa entrare. Le due funzioni stanno in gestione.py e si
    # importano dentro la funzione per non incrociare gli import fra router.
    from .gestione import _slug_nome, _username_libero
    ruolo = db.query_one("SELECT id FROM ruoli WHERE codice = 'allievo'")["id"]
    username = _username_libero(_slug_nome(body.nome, body.cognome))
    try:
        cur = db.execute(
            "INSERT INTO utenti(autoscuola_id, ruolo_id, email, username, password_hash, nome,"
            " cognome, listato_target, data_esame) VALUES(?,?,?,?,?,?,?,?,?)",
            (p.autoscuola_id, ruolo, body.email.lower(), username, hash_password(body.password),
             body.nome, body.cognome, body.listato_target, body.data_esame))
    except Exception:
        raise HTTPException(409, "Email gia' registrata in questa autoscuola")
    db.execute("INSERT INTO audit_log(utente_id, autoscuola_id, azione, entita, entita_id) "
               "VALUES(?,?,'crea_allievo','utenti',?)",
               (p.utente_id, p.autoscuola_id, cur.lastrowid))
    return {"id": cur.lastrowid, "username": username}


@router.delete("/allievi/{utente_id}")
def disattiva_allievo(utente_id: int, p: Principal = Depends(require_admin)):
    _verifica_appartenenza(utente_id, p.autoscuola_id)
    # Soft delete: lo storico didattico va conservato per obblighi e statistiche.
    db.execute("UPDATE utenti SET attivo = 0 WHERE id = ?", (utente_id,))
    db.execute("UPDATE refresh_token SET revocato = 1 WHERE utente_id = ?", (utente_id,))
    db.execute("INSERT INTO audit_log(utente_id, autoscuola_id, azione, entita, entita_id) "
               "VALUES(?,?,'disattiva_allievo','utenti',?)",
               (p.utente_id, p.autoscuola_id, utente_id))
    return {"ok": True}


@router.post("/manutenzione/ricostruisci-aggregati")
def ricostruisci(p: Principal = Depends(require_admin)):
    return analytics.ricostruisci_aggregati()
