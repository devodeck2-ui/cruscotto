"""Autenticazione: login, refresh rotante, logout, profilo."""
from __future__ import annotations

import time
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


# --------------------------------------------------------------------------- #
# Freno ai tentativi ripetuti
# --------------------------------------------------------------------------- #
# Le password sono conservate con PBKDF2 a 260.000 iterazioni, quindi provarle
# a raffica costa comunque caro. Ma senza un freno esplicito nulla impedisce a
# uno script di macinare le password piu' comuni contro l'email di un allievo
# per giorni. Si contano i fallimenti per coppia (indirizzo di rete, email):
#   * dopo TENTATIVI_PRIMA_ATTESA falliti si risponde comunque, ma con un
#     ritardo crescente, che rende la forza bruta impraticabile;
#   * dopo TENTATIVI_MAX si chiude la porta per BLOCCO_MINUTI minuti.
# Il conteggio si azzera al primo accesso riuscito, cosi' chi sbaglia due
# volte e poi entra non si porta dietro nulla.
#
# La memoria e' del processo: e' la scelta giusta per un'installazione con un
# solo processo per autoscuola (come la nostra) e non tocca il database ad
# ogni tentativo fallito. Se un giorno si scalasse a piu' processi, questo
# stato andrebbe spostato su una memoria condivisa.
TENTATIVI_PRIMA_ATTESA = 3
TENTATIVI_MAX = 8
BLOCCO_MINUTI = 15
FINESTRA_MINUTI = 15

_tentativi: dict[tuple[str, str], list] = {}      # chiave -> [quanti, ultimo_tentativo]


def _chiave(request: Request, email: str) -> tuple[str, str]:
    ip = (request.client.host if request.client else "?")
    return (ip, email.lower())


def _controlla_freno(chiave: tuple[str, str]) -> None:
    """Solleva 429 se questa coppia ha esagerato; altrimenti rallenta e basta."""
    voce = _tentativi.get(chiave)
    if not voce:
        return
    quanti, ultimo = voce
    fermo_da = time.time() - ultimo
    if fermo_da > FINESTRA_MINUTI * 60:
        _tentativi.pop(chiave, None)               # finestra scaduta: si riparte puliti
        return
    if quanti >= TENTATIVI_MAX:
        if fermo_da < BLOCCO_MINUTI * 60:
            attesa = int((BLOCCO_MINUTI * 60 - fermo_da) / 60) + 1
            raise HTTPException(
                status_code=429,
                detail=f"Troppi tentativi. Riprova fra {attesa} minuti "
                       f"o chiedi una nuova password in segreteria.",
                headers={"Retry-After": str(int(BLOCCO_MINUTI * 60 - fermo_da))})
        _tentativi.pop(chiave, None)
        return
    if quanti >= TENTATIVI_PRIMA_ATTESA:
        # Ritardo che raddoppia ad ogni errore, con un tetto: fastidioso per uno
        # script, impercettibile per una persona che ha sbagliato a digitare.
        time.sleep(min(2 ** (quanti - TENTATIVI_PRIMA_ATTESA), 8))


def _registra_fallimento(chiave: tuple[str, str]) -> None:
    quanti, _ = _tentativi.get(chiave, (0, 0.0))
    _tentativi[chiave] = [quanti + 1, time.time()]
    # Pulizia opportunistica: senza, la memoria crescerebbe con ogni indirizzo
    # che ha sbagliato una volta sola mesi fa.
    if len(_tentativi) > 5000:
        limite = time.time() - FINESTRA_MINUTI * 60
        for k in [k for k, v in _tentativi.items() if v[1] < limite]:
            _tentativi.pop(k, None)

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
    chiave = _chiave(request, body.email)
    _controlla_freno(chiave)

    row = db.query_one(sql, par)

    # Confronto sempre eseguito anche a utente inesistente: evita di rivelare
    # l'esistenza dell'account tramite differenze nei tempi di risposta.
    stored = row["password_hash"] if row else hash_password("dummy")
    if not verify_password(body.password, stored) or not row or not row["attivo"]:
        _registra_fallimento(chiave)
        raise HTTPException(status_code=401, detail="Email o password non corretti")

    _tentativi.pop(chiave, None)      # accesso riuscito: la lavagna si pulisce
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
