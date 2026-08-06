"""Autenticazione: login, refresh rotante, logout, profilo."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from .. import db
from ..config import settings
from ..rbac import Principal, current_user
from ..security import create_token, hash_opaque, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=4, max_length=128)
    autoscuola: str | None = None      # slug, per email presenti su piu' tenant


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    utente: dict


def _emetti(utente: dict, request: Request) -> TokenOut:
    access = create_token({"sub": utente["id"], "typ": "access", "ruolo": utente["ruolo"],
                           "ten": utente["autoscuola_id"]}, settings.access_ttl_min * 60)
    refresh = create_token({"sub": utente["id"], "typ": "refresh"},
                           settings.refresh_ttl_days * 86400)
    db.execute(
        "INSERT INTO refresh_token(utente_id, token_hash, scade_il, device_info) VALUES(?,?,?,?)",
        (utente["id"], hash_opaque(refresh),
         (datetime.now(timezone.utc) + timedelta(days=settings.refresh_ttl_days)).isoformat(),
         '{"ua": "%s"}' % (request.headers.get("user-agent", "")[:120].replace('"', "'"))))
    db.execute("UPDATE utenti SET ultimo_accesso = ? WHERE id = ?",
               (datetime.now(timezone.utc).isoformat(timespec="seconds"), utente["id"]))
    return TokenOut(access_token=access, refresh_token=refresh, utente=utente)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request):
    sql = ("SELECT u.id, u.autoscuola_id, u.email, u.nome, u.cognome, u.listato_target,"
           "       u.password_hash, u.attivo, r.codice AS ruolo, a.ragione_sociale, a.slug "
           "FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
           "JOIN autoscuole a ON a.id = u.autoscuola_id WHERE u.email = ?")
    par = [body.email.lower()]
    if body.autoscuola:
        sql += " AND a.slug = ?"
        par.append(body.autoscuola)
    row = db.query_one(sql, par)

    # Confronto sempre eseguito anche a utente inesistente: evita di rivelare
    # l'esistenza dell'account tramite differenze nei tempi di risposta.
    stored = row["password_hash"] if row else hash_password("dummy")
    if not verify_password(body.password, stored) or not row or not row["attivo"]:
        raise HTTPException(status_code=401, detail="Email o password non corretti")

    utente = {k: row[k] for k in row.keys() if k != "password_hash"}
    return _emetti(utente, request)


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request):
    from ..security import decode_token
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("typ") != "refresh":
        raise HTTPException(status_code=401, detail="Refresh token non valido")
    rec = db.query_one("SELECT id, revocato FROM refresh_token WHERE token_hash = ?",
                       (hash_opaque(body.refresh_token),))
    if not rec or rec["revocato"]:
        # Riuso di un token gia' speso: possibile furto -> si revoca l'intera famiglia.
        db.execute("UPDATE refresh_token SET revocato = 1 WHERE utente_id = ?", (payload["sub"],))
        raise HTTPException(status_code=401, detail="Sessione compromessa, effettua di nuovo il login")
    db.execute("UPDATE refresh_token SET revocato = 1 WHERE id = ?", (rec["id"],))  # rotazione

    row = db.query_one(
        "SELECT u.id, u.autoscuola_id, u.email, u.nome, u.cognome, u.listato_target,"
        " r.codice AS ruolo, a.ragione_sociale, a.slug FROM utenti u "
        "JOIN ruoli r ON r.id = u.ruolo_id JOIN autoscuole a ON a.id = u.autoscuola_id "
        "WHERE u.id = ? AND u.attivo = 1", (payload["sub"],))
    if not row:
        raise HTTPException(status_code=401, detail="Utente non attivo")
    return _emetti(dict(row), request)


@router.post("/logout")
def logout(p: Principal = Depends(current_user)):
    db.execute("UPDATE refresh_token SET revocato = 1 WHERE utente_id = ?", (p.utente_id,))
    db.execute("UPDATE sessioni_app SET fine = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
               " chiusa_da = 'logout' WHERE utente_id = ? AND fine IS NULL", (p.utente_id,))
    return {"ok": True}


@router.get("/me")
def me(p: Principal = Depends(current_user)):
    row = db.query_one(
        "SELECT u.id, u.email, u.nome, u.cognome, u.listato_target, u.data_esame,"
        " u.preferenze, r.codice AS ruolo, a.ragione_sociale, a.slug FROM utenti u "
        "JOIN ruoli r ON r.id = u.ruolo_id JOIN autoscuole a ON a.id = u.autoscuola_id "
        "WHERE u.id = ?", (p.utente_id,))
    return dict(row)
