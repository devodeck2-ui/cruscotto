"""Statistiche personali dell'allievo. Ogni query e' vincolata al proprio id."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..rbac import Principal, current_user
from ..services import analytics

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


@router.get("/andamento")
def andamento(giorni: int = 30, p: Principal = Depends(current_user)):
    return analytics.serie_temporale(p.utente_id, giorni)


@router.get("/criticita")
def criticita(limite: int = 12, p: Principal = Depends(current_user)):
    return analytics.criticita_utente(p.utente_id, limite)
