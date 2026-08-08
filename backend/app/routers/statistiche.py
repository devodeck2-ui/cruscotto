"""Statistiche personali dell'allievo. Ogni query e' vincolata al proprio id."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..rbac import Principal, current_user
from ..services import analytics
from .patenti import codici_utente

router = APIRouter(prefix="/api/statistiche", tags=["statistiche"])


@router.get("/riepilogo")
def riepilogo(p: Principal = Depends(current_user)):
    v = db.query_one("SELECT * FROM v_progresso_allievo WHERE utente_id = ?", (p.utente_id,))
    lst = db.query_one("SELECT id FROM listati WHERE codice = ?", (p.listato_target,))
    return {
        "profilo": dict(v) if v else {},
        "prontezza": analytics.indice_prontezza(p.utente_id),
        "criticita": analytics.criticita_utente(p.utente_id),
        "capitoli": analytics.riepilogo_capitoli(p.utente_id, lst["id"]) if lst else [],
        "serie": analytics.serie_temporale(p.utente_id, 30),
    }


@router.get("/capitoli")
def capitoli(listato: str | None = None, p: Principal = Depends(current_user)):
    """Copertura per capitolo di UNA patente scelta tra quelle dell'allievo.

    Chi prepara piu' patenti insieme (es. AM a 14 anni, B a 18) vede sempre
    la principale nel riepilogo generale: qui puo' guardare anche le altre,
    senza doverle rendere principali solo per controllare a che punto e'.
    Il codice richiesto viene accettato solo se e' tra le sue: altrimenti si
    ignora e si torna alla principale, cosi' non si puo' sbirciare in un
    listato che non gli appartiene.
    """
    proprie = codici_utente(p.utente_id) or ([p.listato_target] if p.listato_target else [])
    scelto = listato if listato in proprie else (proprie[0] if proprie else p.listato_target)
    lst = db.query_one("SELECT id FROM listati WHERE codice = ?", (scelto,)) if scelto else None
    return {"listato": scelto, "capitoli": analytics.riepilogo_capitoli(p.utente_id, lst["id"]) if lst else []}


@router.get("/andamento")
def andamento(giorni: int = 30, p: Principal = Depends(current_user)):
    return analytics.serie_temporale(p.utente_id, giorni)


@router.get("/criticita")
def criticita(limite: int = 12, p: Principal = Depends(current_user)):
    return analytics.criticita_utente(p.utente_id, limite)
