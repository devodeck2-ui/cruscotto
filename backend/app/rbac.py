"""Autenticazione e isolamento dati (RBAC + tenant scoping).

Modello di sicurezza a tre livelli, difesa in profondita':

  1. AUTENTICAZIONE  - JWT firmato, access token a vita breve + refresh rotante.
  2. AUTORIZZAZIONE  - dipendenza FastAPI che verifica il ruolo sull'endpoint.
  3. ISOLAMENTO DATI - ogni query di dominio riceve utente_id/autoscuola_id dal
     token, MAI da parametri controllati dal client. Un allievo che chiama
     /api/admin/allievi/42/statistiche riceve 403 anche se 42 e' del suo tenant;
     un admin di un tenant che chiede un allievo di un altro tenant riceve 404
     perche' il filtro autoscuola_id e' inchiodato nella WHERE.

Questo replica, a livello applicativo, cio' che in Postgres si otterrebbe con
Row Level Security. La regola operativa e': nessuna funzione del layer dati
accetta un identificativo di tenant proveniente dal corpo della richiesta.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from . import db
from .security import decode_token


@dataclass(frozen=True)
class Principal:
    utente_id: int
    autoscuola_id: int
    ruolo: str
    email: str
    nome: str
    listato_target: str

    @property
    def is_staff(self) -> bool:
        return self.ruolo in ("admin", "istruttore", "superadmin")


def _unauth(msg: str = "Credenziali non valide"):
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=msg,
                         headers={"WWW-Authenticate": "Bearer"})


async def current_user(authorization: str = Header(default="")) -> Principal:
    if not authorization.startswith("Bearer "):
        raise _unauth("Token assente")
    payload = decode_token(authorization[7:])
    if not payload or payload.get("typ") != "access":
        raise _unauth("Token scaduto o non valido")
    row = db.query_one(
        "SELECT u.id, u.autoscuola_id, u.email, u.nome, u.listato_target, u.attivo, r.codice AS ruolo "
        "FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id WHERE u.id = ?",
        (payload["sub"],))
    if not row or not row["attivo"]:
        raise _unauth("Utente disabilitato")
    return Principal(row["id"], row["autoscuola_id"], row["ruolo"],
                     row["email"], row["nome"], row["listato_target"])


def require_roles(*ruoli: str):
    async def _dep(p: Principal = Depends(current_user)) -> Principal:
        if p.ruolo not in ruoli:
            raise HTTPException(status_code=403,
                                detail="Permessi insufficienti per questa risorsa")
        return p
    return _dep


require_staff = require_roles("admin", "istruttore", "superadmin")
require_admin = require_roles("admin", "superadmin")
