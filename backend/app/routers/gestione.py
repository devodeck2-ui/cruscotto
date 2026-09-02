"""Gestione dell'autoscuola: orario, presenze, anagrafica, videocorsi, analisi.

Questo modulo copre tutto cio' che fa l'amministratore/istruttore e che non
riguarda i quiz. E' volutamente separato da amministrazione.py, che resta
dedicato alle statistiche didattiche.

Aree:
    /api/gestione/orario     orario settimanale ricorrente (gli "slot")
    /api/gestione/lezioni    lezioni concrete in calendario + presenze
    /api/gestione/allievi    anagrafica, iscrizione con credenziali generate
    /api/gestione/video      videocorsi (link o file) e lezioni live
    /api/gestione/analisi    confronto fra fasce orarie e categorie

NOTA SULLA FREQUENZA LIBERA
    Gli allievi non hanno obbligo di frequenza e possono presentarsi a una
    fascia oraria diversa ogni volta. Di conseguenza la presenza NON e' legata
    a un'iscrizione fissa a un corso: e' un fatto puntuale (questo allievo,
    questa lezione). Tutte le analisi partono da li'.
"""
from __future__ import annotations

import csv
import io
import re
import secrets
import unicodedata
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import db
from ..config import settings
from ..rbac import Principal, require_admin, require_staff
from ..security import hash_password
from ..services import notifiche
from .patenti import codici_utente, imposta, utenti_con_patente

router = APIRouter(prefix="/api/gestione", tags=["gestione"])

GIORNI = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
ORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _ora_valida(v: str, campo: str) -> str:
    if not ORA_RE.match(v or ""):
        raise HTTPException(422, f"{campo}: usa il formato HH:MM (es. 08:30)")
    return v


def _adesso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =========================================================================== #
# 1. ORARIO SETTIMANALE
# =========================================================================== #

class SlotIn(BaseModel):
    giorno: int = Field(ge=0, le=6, description="0 = lunedi ... 6 = domenica")
    ora_inizio: str
    ora_fine: str
    listato: str = "B"
    aula: str | None = None
    docente: str | None = None
    note: str | None = None
    attivo: bool = True


@router.get("/orario")
def orario(p: Principal = Depends(require_staff)):
    """Orario settimanale completo, raggruppato per giorno."""
    righe = db.rows_to_dicts(db.query(
        "SELECT s.*, "
        "  (SELECT COUNT(*) FROM aula_lezione l WHERE l.slot_id = s.id) AS lezioni_generate "
        "FROM aula_slot s WHERE s.autoscuola_id = ? "
        "ORDER BY s.giorno, s.ora_inizio", (p.autoscuola_id,)))
    settimana = []
    for g in range(7):
        settimana.append({"giorno": g, "nome": GIORNI[g],
                          "slot": [r for r in righe if r["giorno"] == g]})
    return {"settimana": settimana, "totale_slot": len(righe)}


@router.post("/orario")
def crea_slot(body: SlotIn, p: Principal = Depends(require_admin)):
    _ora_valida(body.ora_inizio, "ora_inizio")
    _ora_valida(body.ora_fine, "ora_fine")
    if body.ora_fine <= body.ora_inizio:
        raise HTTPException(422, "L'ora di fine deve essere successiva a quella di inizio")

    # Sovrapposizione sullo stesso giorno: due lezioni non possono occupare la
    # stessa fascia se si tengono nella stessa aula.
    conflitto = db.query_one(
        "SELECT id, ora_inizio, ora_fine FROM aula_slot "
        "WHERE autoscuola_id = ? AND giorno = ? AND attivo = 1 "
        "AND IFNULL(aula,'') = IFNULL(?,'') "
        "AND ora_inizio < ? AND ora_fine > ?",
        (p.autoscuola_id, body.giorno, body.aula, body.ora_fine, body.ora_inizio))
    if conflitto:
        raise HTTPException(409, f"Si sovrappone alla lezione {conflitto['ora_inizio']}"
                                 f"-{conflitto['ora_fine']} nella stessa aula")

    cur = db.execute(
        "INSERT INTO aula_slot(autoscuola_id, giorno, ora_inizio, ora_fine, listato,"
        " aula, docente, note, attivo, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (p.autoscuola_id, body.giorno, body.ora_inizio, body.ora_fine, body.listato,
         body.aula, body.docente, body.note, 1 if body.attivo else 0, _adesso()))
    return {"id": cur.lastrowid}


@router.put("/orario/{slot_id}")
def modifica_slot(slot_id: int, body: SlotIn, p: Principal = Depends(require_admin)):
    _ora_valida(body.ora_inizio, "ora_inizio")
    _ora_valida(body.ora_fine, "ora_fine")
    cur = db.execute(
        "UPDATE aula_slot SET giorno=?, ora_inizio=?, ora_fine=?, listato=?, aula=?,"
        " docente=?, note=?, attivo=? WHERE id=? AND autoscuola_id=?",
        (body.giorno, body.ora_inizio, body.ora_fine, body.listato, body.aula,
         body.docente, body.note, 1 if body.attivo else 0, slot_id, p.autoscuola_id))
    if not cur.rowcount:
        raise HTTPException(404, "Fascia oraria non trovata")
    return {"ok": True}


@router.delete("/orario/{slot_id}")
def elimina_slot(slot_id: int, p: Principal = Depends(require_admin)):
    """Disattiva la fascia. Le lezioni gia' svolte restano nello storico:
    cancellarle farebbe sparire le presenze registrate."""
    cur = db.execute("UPDATE aula_slot SET attivo = 0 WHERE id = ? AND autoscuola_id = ?",
                     (slot_id, p.autoscuola_id))
    if not cur.rowcount:
        raise HTTPException(404, "Fascia oraria non trovata")
    return {"ok": True}


# =========================================================================== #
# 2. LEZIONI E PRESENZE
# =========================================================================== #

def _genera_lezioni(autoscuola_id: int, dal: date, al: date) -> int:
    """Crea le lezioni del periodo a partire dall'orario settimanale.

    Idempotente: la UNIQUE su (autoscuola, data, ora, listato) fa si' che
    rigenerare lo stesso intervallo non produca duplicati.
    """
    slot = db.query("SELECT * FROM aula_slot WHERE autoscuola_id = ? AND attivo = 1",
                    (autoscuola_id,))
    creati = 0
    giorno = dal
    while giorno <= al:
        for s in slot:
            if s["giorno"] != giorno.weekday():
                continue
            cur = db.execute(
                "INSERT INTO aula_lezione(autoscuola_id, slot_id, data, ora_inizio,"
                " ora_fine, listato, docente, aula, stato) "
                "VALUES(?,?,?,?,?,?,?,?, 'programmata') "
                "ON CONFLICT(autoscuola_id, data, ora_inizio, listato) DO NOTHING",
                (autoscuola_id, s["id"], giorno.isoformat(), s["ora_inizio"],
                 s["ora_fine"], s["listato"], s["docente"], s["aula"]))
            creati += cur.rowcount
        giorno += timedelta(days=1)
    return creati


class GeneraIn(BaseModel):
    dal: str
    al: str


@router.post("/lezioni/genera")
def genera(body: GeneraIn, p: Principal = Depends(require_admin)):
    try:
        dal, al = date.fromisoformat(body.dal), date.fromisoformat(body.al)
    except ValueError:
        raise HTTPException(422, "Date nel formato AAAA-MM-GG")
    if al < dal:
        raise HTTPException(422, "La data finale precede quella iniziale")
    if (al - dal).days > 120:
        raise HTTPException(422, "Genera al massimo quattro mesi per volta")
    return {"create": _genera_lezioni(p.autoscuola_id, dal, al)}


@router.get("/lezioni")
def lezioni(dal: str | None = None, al: str | None = None,
            listato: str | None = None, p: Principal = Depends(require_staff)):
    oggi = date.today()
    dal = dal or (oggi - timedelta(days=oggi.weekday())).isoformat()
    al = al or (date.fromisoformat(dal) + timedelta(days=13)).isoformat()

    sql = ("SELECT l.*, "
           "  (SELECT COUNT(*) FROM aula_presenza pr WHERE pr.lezione_id = l.id "
           "     AND pr.stato = 'presente') AS presenti "
           "FROM aula_lezione l WHERE l.autoscuola_id = ? AND l.data BETWEEN ? AND ?")
    par = [p.autoscuola_id, dal, al]
    if listato:
        sql += " AND l.listato = ?"
        par.append(listato)
    sql += " ORDER BY l.data, l.ora_inizio"
    return {"dal": dal, "al": al, "lezioni": db.rows_to_dicts(db.query(sql, par))}


@router.get("/lezioni/{lezione_id}")
def dettaglio_lezione(lezione_id: int, p: Principal = Depends(require_staff)):
    """Lezione + elenco allievi con la presenza gia' spuntata.

    L'elenco comprende tutti gli allievi attivi della categoria, perche' la
    frequenza e' libera: chiunque studi per quella patente puo' presentarsi.
    Gli allievi di altre categorie che risultano comunque presenti (capita:
    un CQC che segue un ripasso di base) restano visibili in fondo.
    """
    lez = db.query_one("SELECT * FROM aula_lezione WHERE id = ? AND autoscuola_id = ?",
                       (lezione_id, p.autoscuola_id))
    if not lez:
        raise HTTPException(404, "Lezione non trovata")

    righe = db.rows_to_dicts(db.query(
        "SELECT u.id AS utente_id, u.nome, u.cognome, u.listato_target, u.telefono,"
        "       u.ore_acquistate, pr.stato, pr.registrato_il,"
        "       (SELECT COUNT(*) FROM aula_presenza p2 JOIN aula_lezione l2 ON l2.id = p2.lezione_id"
        "         WHERE p2.utente_id = u.id AND p2.stato = 'presente') AS presenze_totali "
        "FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
        "LEFT JOIN aula_presenza pr ON pr.utente_id = u.id AND pr.lezione_id = ? "
        "WHERE u.autoscuola_id = ? AND r.codice = 'allievo' AND u.attivo = 1 "
        "  AND (u.listato_target = ? OR pr.stato IS NOT NULL) "
        "ORDER BY (u.listato_target = ?) DESC, u.cognome, u.nome",
        (lezione_id, p.autoscuola_id, lez["listato"], lez["listato"])))
    return {"lezione": dict(lez), "allievi": righe}


class PresenzaIn(BaseModel):
    utente_id: int
    stato: str = Field(default="presente", pattern="^(presente|assente|giustificato)$")


@router.post("/lezioni/{lezione_id}/presenza")
def segna_presenza(lezione_id: int, body: PresenzaIn, p: Principal = Depends(require_staff)):
    lez = db.query_one("SELECT id FROM aula_lezione WHERE id = ? AND autoscuola_id = ?",
                       (lezione_id, p.autoscuola_id))
    if not lez:
        raise HTTPException(404, "Lezione non trovata")
    # L'allievo deve appartenere alla stessa autoscuola: senza questo controllo
    # un admin potrebbe segnare presente un utente di un altro istituto.
    if not db.query_one("SELECT 1 FROM utenti WHERE id = ? AND autoscuola_id = ?",
                        (body.utente_id, p.autoscuola_id)):
        raise HTTPException(404, "Allievo non trovato")

    if body.stato == "assente":
        db.execute("DELETE FROM aula_presenza WHERE lezione_id = ? AND utente_id = ?",
                   (lezione_id, body.utente_id))
    else:
        db.execute(
            "INSERT INTO aula_presenza(lezione_id, utente_id, stato, registrato_il, registrato_da) "
            "VALUES(?,?,?,?,?) ON CONFLICT(lezione_id, utente_id) DO UPDATE SET "
            " stato = excluded.stato, registrato_il = excluded.registrato_il,"
            " registrato_da = excluded.registrato_da",
            (lezione_id, body.utente_id, body.stato, _adesso(), p.utente_id))
    db.execute("UPDATE aula_lezione SET stato = 'svolta' WHERE id = ? AND stato = 'programmata'",
               (lezione_id,))
    return {"ok": True}


class LezioneIn(BaseModel):
    data: str
    ora_inizio: str
    ora_fine: str
    listato: str = "B"
    argomento: str | None = None
    docente: str | None = None
    aula: str | None = None
    note: str | None = None


@router.post("/lezioni")
def crea_lezione(body: LezioneIn, p: Principal = Depends(require_admin)):
    """Lezione straordinaria, fuori dall'orario ricorrente."""
    _ora_valida(body.ora_inizio, "ora_inizio")
    _ora_valida(body.ora_fine, "ora_fine")
    try:
        cur = db.execute(
            "INSERT INTO aula_lezione(autoscuola_id, slot_id, data, ora_inizio, ora_fine,"
            " listato, argomento, docente, aula, note, stato) "
            "VALUES(?,NULL,?,?,?,?,?,?,?,?, 'programmata')",
            (p.autoscuola_id, body.data, body.ora_inizio, body.ora_fine, body.listato,
             body.argomento, body.docente, body.aula, body.note))
    except Exception:
        raise HTTPException(409, "Esiste gia' una lezione in quella data e ora")
    return {"id": cur.lastrowid}


class ArgomentoIn(BaseModel):
    argomento: str | None = None
    note: str | None = None
    stato: str = Field(default="svolta", pattern="^(programmata|svolta|annullata)$")


@router.put("/lezioni/{lezione_id}")
def aggiorna_lezione(lezione_id: int, body: ArgomentoIn, p: Principal = Depends(require_staff)):
    cur = db.execute(
        "UPDATE aula_lezione SET argomento = ?, note = ?, stato = ? "
        "WHERE id = ? AND autoscuola_id = ?",
        (body.argomento, body.note, body.stato, lezione_id, p.autoscuola_id))
    if not cur.rowcount:
        raise HTTPException(404, "Lezione non trovata")
    return {"ok": True}


# =========================================================================== #
# 3. ANAGRAFICA ALLIEVI
# =========================================================================== #

def _slug_nome(nome: str, cognome: str) -> str:
    testo = f"{nome[:1]}.{cognome}" if nome else cognome
    testo = unicodedata.normalize("NFKD", testo).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9.]+", "", testo).lower()[:24] or "allievo"


def _username_libero(base: str) -> str:
    """Aggiunge un numero progressivo finche' lo username non e' libero.

    L'unicita' e' globale e non per autoscuola: cosi' l'allievo puo' accedere
    scrivendo solo il proprio username, senza dover indicare anche la scuola.
    """
    candidato, n = base, 1
    while db.query_one("SELECT 1 FROM utenti WHERE username = ?", (candidato,)):
        n += 1
        candidato = f"{base}{n}"
    return candidato


def _password_leggibile(n: int = 10) -> str:
    """Password casuale ma dettabile al telefono: niente caratteri ambigui
    come l/1/I oppure O/0, che generano sempre una telefonata di richiamo."""
    alfabeto = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(n))


class AllievoIn(BaseModel):
    nome: str = Field(min_length=1, max_length=60)
    cognome: str = Field(min_length=1, max_length=60)
    telefono: str | None = None
    email: str | None = None
    indirizzo: str | None = None
    codice_fiscale: str | None = None
    listato_target: str = "B"
    patenti: list[str] | None = None
    ore_acquistate: int = Field(default=0, ge=0, le=500)
    importo_pagato: float | None = None
    data_esame: str | None = None
    note: str | None = None


@router.post("/allievi")
def iscrivi_allievo(body: AllievoIn, p: Principal = Depends(require_admin)):
    """Iscrizione: registra l'anagrafica e genera le credenziali di accesso.

    La password viene restituita UNA SOLA VOLTA, in chiaro, perche' nel
    database ne resta solo l'impronta: e' il momento di consegnarla o
    stamparla. Se si perde, l'admin ne rigenera un'altra.
    """
    ruolo = db.query_one("SELECT id FROM ruoli WHERE codice = 'allievo'")
    if not ruolo:
        raise HTTPException(500, "Ruolo allievo mancante nel database")

    username = _username_libero(_slug_nome(body.nome, body.cognome))
    password = _password_leggibile()
    # L'email di contatto puo' mancare o ripetersi (fratelli, genitori): la
    # colonna email resta la chiave di accesso, quindi se non c'e' si usa lo
    # username come indirizzo interno.
    email_login = (body.email or "").strip().lower() or f"{username}@locale"

    if db.query_one("SELECT 1 FROM utenti WHERE autoscuola_id = ? AND email = ?",
                    (p.autoscuola_id, email_login)):
        raise HTTPException(409, "Esiste gia' un allievo con questa email")

    cur = db.execute(
        "INSERT INTO utenti(autoscuola_id, ruolo_id, email, username, password_hash, nome,"
        " cognome, telefono, codice_fiscale, indirizzo, listato_target, ore_acquistate,"
        " importo_pagato, data_esame, note_admin, data_iscrizione, attivo) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        (p.autoscuola_id, ruolo["id"], email_login, username, hash_password(password),
         body.nome.strip(), body.cognome.strip(), body.telefono, body.codice_fiscale,
         body.indirizzo, body.listato_target, body.ore_acquistate, body.importo_pagato,
         body.data_esame, body.note, date.today().isoformat()))

    if body.patenti:
        imposta(cur.lastrowid, body.patenti)

    db.execute("INSERT INTO audit_log(utente_id, autoscuola_id, azione, entita, entita_id) "
               "VALUES(?,?,'iscrizione_allievo','utenti',?)",
               (p.utente_id, p.autoscuola_id, cur.lastrowid))

    return {"id": cur.lastrowid, "username": username, "password": password,
            "email_accesso": email_login,
            "avviso": "Annota la password: non sara' piu' visibile."}


@router.get("/allievi")
def elenco_allievi(categoria: str | None = None, cerca: str | None = None,
                   includi_disattivi: bool = False, p: Principal = Depends(require_staff)):
    """Anagrafica raggruppata per categoria di patente."""
    sql = ("SELECT u.id, u.nome, u.cognome, u.email, u.username, u.telefono, u.indirizzo,"
           "       u.codice_fiscale, u.listato_target, u.ore_acquistate, u.importo_pagato,"
           "       u.data_iscrizione, u.data_esame, u.attivo, u.ultimo_accesso, u.note_admin,"
           "       u.listati_extra,"
           "  (SELECT COUNT(*) FROM aula_presenza pr JOIN aula_lezione l ON l.id = pr.lezione_id"
           "    WHERE pr.utente_id = u.id AND pr.stato = 'presente') AS ore_frequentate,"
           "  (SELECT COUNT(*) FROM schede s WHERE s.utente_id = u.id AND s.stato = 'completata') AS schede,"
           "  (SELECT COUNT(*) FROM schede s WHERE s.utente_id = u.id AND s.tipo = 'simulazione'"
           "    AND s.esito = 1) AS simulazioni_superate "
           "FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
           "WHERE u.autoscuola_id = ? AND r.codice = 'allievo'")
    par: list = [p.autoscuola_id]
    if not includi_disattivi:
        sql += " AND u.attivo = 1"
    if categoria:
        sql += " AND u.listato_target = ?"
        par.append(categoria)
    if cerca:
        sql += " AND (u.nome LIKE ? OR u.cognome LIKE ? OR u.telefono LIKE ? OR u.email LIKE ?)"
        par += [f"%{cerca}%"] * 4
    sql += " ORDER BY u.listato_target, u.cognome, u.nome"

    righe = db.rows_to_dicts(db.query(sql, par))
    categorie: dict[str, list] = {}
    for r in righe:
        categorie.setdefault(r["listato_target"], []).append(r)
    return {
        "totale": len(righe),
        "categorie": [{"codice": c, "allievi": v, "n": len(v),
                       "ore_vendute": sum(x["ore_acquistate"] or 0 for x in v),
                       "incasso": round(sum(x["importo_pagato"] or 0 for x in v), 2)}
                      for c, v in sorted(categorie.items())],
    }


@router.get("/allievi/esporta")
def esporta_allievi(includi_disattivi: bool = False, p: Principal = Depends(require_admin)):
    """Elenco allievi come foglio di calcolo, generato al momento.

    Deliberatamente non esiste nessun file permanente con le anagrafiche: ogni
    copia in piu' e' un posto in piu' da cui i dati possono uscire e da tenere
    aggiornato. Il foglio si crea quando l'autoscuola lo chiede (commercialista,
    controllo, cambio gestionale) e vive nel computer di chi l'ha scaricato.

    Formato CSV con punto e virgola e BOM: e' quello che Excel in italiano apre
    con un doppio clic senza chiedere nulla e senza rovinare gli accenti.
    Riservato all'admin: un istruttore non ha motivo di portarsi via l'elenco.
    """
    sql = ("SELECT u.id, u.cognome, u.nome, u.email, u.username, u.telefono, u.codice_fiscale,"
           "       u.indirizzo, u.listato_target, u.listati_extra, u.ore_acquistate,"
           "       u.importo_pagato, u.data_iscrizione, u.data_esame, u.attivo, u.note_admin,"
           "  (SELECT COUNT(*) FROM aula_presenza pr JOIN aula_lezione l ON l.id = pr.lezione_id"
           "    WHERE pr.utente_id = u.id AND pr.stato = 'presente') AS ore_frequentate "
           "FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
           "WHERE u.autoscuola_id = ? AND r.codice = 'allievo'")
    par: list = [p.autoscuola_id]
    if not includi_disattivi:
        sql += " AND u.attivo = 1"
    sql += " ORDER BY u.cognome, u.nome"

    intestazioni = ["Cognome", "Nome", "Email", "Utente", "Telefono", "Codice fiscale",
                    "Indirizzo", "Patenti", "Ore acquistate", "Ore frequentate",
                    "Importo pagato", "Data iscrizione", "Data esame", "Stato", "Note"]

    buf = io.StringIO()
    scrittore = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL,
                           lineterminator="\r\n")
    scrittore.writerow(intestazioni)
    n = 0
    for r in db.query(sql, par):
        patenti = ", ".join(codici_utente(r["id"]))
        # La virgola decimale e' quella che Excel in italiano riconosce come numero.
        importo = "" if r["importo_pagato"] is None else f"{r['importo_pagato']:.2f}".replace(".", ",")
        scrittore.writerow([
            r["cognome"], r["nome"], r["email"], r["username"], r["telefono"] or "",
            r["codice_fiscale"] or "", r["indirizzo"] or "", patenti,
            r["ore_acquistate"] or 0, r["ore_frequentate"] or 0, importo,
            r["data_iscrizione"] or "", r["data_esame"] or "",
            "attivo" if r["attivo"] else "disattivato", (r["note_admin"] or "").replace("\n", " "),
        ])
        n += 1

    # Un export porta fuori l'anagrafica completa: resta traccia di chi l'ha
    # fatto e quando, cosi' l'autoscuola puo' rispondere a un allievo che
    # chiede chi ha visto i suoi dati.
    db.execute("INSERT INTO audit_log(utente_id, autoscuola_id, azione, entita, entita_id) "
               "VALUES(?,?,'esporta_allievi','utenti',NULL)", (p.utente_id, p.autoscuola_id))

    nome_file = f"allievi-{date.today().isoformat()}.csv"
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome_file}"',
                 "X-Righe-Esportate": str(n)})

class ModificaAllievoIn(BaseModel):
    nome: str | None = None
    cognome: str | None = None
    telefono: str | None = None
    email: str | None = None
    indirizzo: str | None = None
    codice_fiscale: str | None = None
    listato_target: str | None = None
    patenti: list[str] | None = None
    ore_acquistate: int | None = None
    importo_pagato: float | None = None
    data_esame: str | None = None
    note: str | None = None
    attivo: bool | None = None


@router.put("/allievi/{utente_id}")
def modifica_allievo(utente_id: int, body: ModificaAllievoIn,
                     p: Principal = Depends(require_admin)):
    if not db.query_one("SELECT 1 FROM utenti WHERE id = ? AND autoscuola_id = ?",
                        (utente_id, p.autoscuola_id)):
        raise HTTPException(404, "Allievo non trovato")

    campi = {"nome": body.nome, "cognome": body.cognome, "telefono": body.telefono,
             "email": (body.email or "").strip().lower() or None,
             "indirizzo": body.indirizzo, "codice_fiscale": body.codice_fiscale,
             "listato_target": body.listato_target, "ore_acquistate": body.ore_acquistate,
             "importo_pagato": body.importo_pagato, "data_esame": body.data_esame,
             "note_admin": body.note,
             "attivo": None if body.attivo is None else (1 if body.attivo else 0)}
    campi = {k: v for k, v in campi.items() if v is not None}
    if body.patenti:
        imposta(utente_id, body.patenti)
        campi.pop("listato_target", None)
    if not campi:
        return {"ok": True, "modificati": 0}

    sql = "UPDATE utenti SET " + ", ".join(f"{k} = ?" for k in campi) + \
          ", updated_at = ? WHERE id = ? AND autoscuola_id = ?"
    db.execute(sql, [*campi.values(), _adesso(), utente_id, p.autoscuola_id])
    return {"ok": True, "modificati": len(campi)}


@router.post("/allievi/{utente_id}/password")
def rigenera_password(utente_id: int, p: Principal = Depends(require_admin)):
    """Nuova password. Tutte le sessioni attive vengono invalidate: se la si
    rigenera perche' l'accesso e' finito nelle mani sbagliate, lasciare vivi i
    token vanificherebbe l'operazione."""
    u = db.query_one("SELECT username FROM utenti WHERE id = ? AND autoscuola_id = ?",
                     (utente_id, p.autoscuola_id))
    if not u:
        raise HTTPException(404, "Allievo non trovato")
    password = _password_leggibile()
    db.execute("UPDATE utenti SET password_hash = ? WHERE id = ?",
               (hash_password(password), utente_id))
    db.execute("UPDATE refresh_token SET revocato = 1 WHERE utente_id = ?", (utente_id,))
    return {"username": u["username"], "password": password}


@router.get("/allievi/{utente_id}/presenze")
def presenze_allievo(utente_id: int, p: Principal = Depends(require_staff)):
    if not db.query_one("SELECT 1 FROM utenti WHERE id = ? AND autoscuola_id = ?",
                        (utente_id, p.autoscuola_id)):
        raise HTTPException(404, "Allievo non trovato")
    return db.rows_to_dicts(db.query(
        "SELECT l.data, l.ora_inizio, l.ora_fine, l.listato, l.argomento, pr.stato "
        "FROM aula_presenza pr JOIN aula_lezione l ON l.id = pr.lezione_id "
        "WHERE pr.utente_id = ? ORDER BY l.data DESC, l.ora_inizio DESC LIMIT 100",
        (utente_id,)))


# =========================================================================== #
# 4. VIDEOCORSI E DIRETTE
# =========================================================================== #

ESTENSIONI_VIDEO = {".mp4", ".webm", ".m4v", ".mov", ".m3u8"}
LIMITE_UPLOAD = 2 * 1024 * 1024 * 1024      # 2 GB per file


def _corso_predefinito(autoscuola_id: int, listato: str) -> int:
    """Ogni categoria ha un contenitore: cosi' l'admin non deve creare un
    corso prima di poter caricare la prima lezione."""
    lst = db.query_one("SELECT id FROM listati WHERE codice = ?", (listato,))
    if not lst:
        raise HTTPException(422, f"Listato {listato} inesistente")
    c = db.query_one("SELECT id FROM corsi WHERE autoscuola_id = ? AND listato_id = ?",
                     (autoscuola_id, lst["id"]))
    if c:
        return c["id"]
    cur = db.execute(
        "INSERT INTO corsi(autoscuola_id, listato_id, titolo, descrizione, pubblicato) "
        "VALUES(?,?,?,?,1)",
        (autoscuola_id, lst["id"], f"Videocorso {listato}",
         f"Lezioni teoriche per la categoria {listato}"))
    return cur.lastrowid


class VideoIn(BaseModel):
    titolo: str = Field(min_length=2, max_length=160)
    listato: str = "B"
    url: str | None = None
    descrizione: str | None = None
    durata_min: int = Field(default=0, ge=0, le=600)
    pubblicata: bool = True


def _normalizza_url(url: str) -> str:
    """Converte i link 'da guardare' nei link 'da incorporare'.

    Incollare l'indirizzo della pagina di YouTube in un tag <video> non
    funziona: serve la forma /embed/. Stessa cosa per Drive. Farlo qui evita
    all'admin di doverci pensare.
    """
    u = (url or "").strip()
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/live/)([\w-]{11})", u)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", u)
    if m:
        return f"https://drive.google.com/file/d/{m.group(1)}/preview"
    m = re.search(r"vimeo\.com/(\d+)", u)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    return u


@router.post("/video")
def crea_video(body: VideoIn, p: Principal = Depends(require_admin)):
    """Registra una lezione video a partire da un link esterno."""
    if not body.url:
        raise HTTPException(422, "Serve il link del video")
    corso_id = _corso_predefinito(p.autoscuola_id, body.listato)
    ordine = db.query_one("SELECT COALESCE(MAX(ordine), 0) + 1 AS n FROM lezioni_video "
                          "WHERE corso_id = ?", (corso_id,))["n"]
    cur = db.execute(
        "INSERT INTO lezioni_video(corso_id, titolo, descrizione, tipo, url, durata_sec,"
        " ordine, pubblicata) VALUES(?,?,?, 'registrata', ?,?,?,?)",
        (corso_id, body.titolo, body.descrizione, _normalizza_url(body.url),
         body.durata_min * 60, ordine, 1 if body.pubblicata else 0))

    # Stessa logica delle dirette: se la lezione nasce gia' pubblicata, gli
    # allievi di quella patente vengono avvisati subito, invece di doverla
    # scoprire per caso riaprendo i Videocorsi. Se nasce nascosta non si
    # avvisa nessuno: l'avviso partira' quando verra' pubblicata.
    if body.pubblicata:
        notifiche.notifica_utenti(
            utenti_con_patente(p.autoscuola_id, body.listato),
            "lezione_programmata", f"Nuova videolezione: {body.titolo}",
            "Disponibile ora nella sezione Videocorsi.", "/#/video")
    return {"id": cur.lastrowid, "url": _normalizza_url(body.url)}


@router.post("/video/carica")
async def carica_video(request: Request, titolo: str = Query(...), listato: str = "B",
                       nome_file: str = Query(...), descrizione: str | None = None,
                       p: Principal = Depends(require_admin)):
    """Caricamento di un file video dal PC.

    Il corpo della richiesta e' il file grezzo, non un form multipart: si
    evita cosi' la dipendenza python-multipart e soprattutto si scrive su
    disco a blocchi, senza caricare in memoria un file da un giga.
    Il file finisce sotto data/media/video/, che e' gia' servita staticamente.
    """
    from pathlib import Path
    est = Path(nome_file).suffix.lower()
    if est not in ESTENSIONI_VIDEO:
        raise HTTPException(422, "Formato non ammesso: usa mp4, webm, mov o m4v")

    cartella = settings.media_dir / "video"
    cartella.mkdir(parents=True, exist_ok=True)
    nome = f"{secrets.token_hex(8)}{est}"
    destinazione = cartella / nome

    scritti = 0
    try:
        with open(destinazione, "wb") as f:
            async for blocco in request.stream():
                scritti += len(blocco)
                if scritti > LIMITE_UPLOAD:
                    raise HTTPException(413, "File troppo grande (limite 2 GB)")
                f.write(blocco)
    except HTTPException:
        destinazione.unlink(missing_ok=True)
        raise
    except Exception as e:
        destinazione.unlink(missing_ok=True)
        raise HTTPException(500, f"Caricamento non riuscito: {e}")

    if scritti == 0:
        destinazione.unlink(missing_ok=True)
        raise HTTPException(422, "File vuoto")

    corso_id = _corso_predefinito(p.autoscuola_id, listato)
    ordine = db.query_one("SELECT COALESCE(MAX(ordine), 0) + 1 AS n FROM lezioni_video "
                          "WHERE corso_id = ?", (corso_id,))["n"]
    cur = db.execute(
        "INSERT INTO lezioni_video(corso_id, titolo, descrizione, tipo, url, ordine, pubblicata) "
        "VALUES(?,?,?, 'registrata', ?,?,1)",
        (corso_id, titolo, descrizione, f"/media/video/{nome}", ordine))

    notifiche.notifica_utenti(
        utenti_con_patente(p.autoscuola_id, listato),
        "lezione_programmata", f"Nuova videolezione: {titolo}",
        "Disponibile ora nella sezione Videocorsi.", "/#/video")
    return {"id": cur.lastrowid, "url": f"/media/video/{nome}",
            "megabyte": round(scritti / 1048576, 1)}


class LiveIn(BaseModel):
    titolo: str = Field(min_length=2, max_length=160)
    listato: str = "B"
    url: str
    inizio: str                      # AAAA-MM-GGTHH:MM
    descrizione: str | None = None


@router.post("/live")
def programma_live(body: LiveIn, p: Principal = Depends(require_admin)):
    """Programma una diretta. Il collegamento e' un link esterno (Meet, Zoom,
    YouTube Live): l'app mostra il countdown e apre la stanza al momento."""
    corso_id = _corso_predefinito(p.autoscuola_id, body.listato)
    cur = db.execute(
        "INSERT INTO lezioni_video(corso_id, titolo, descrizione, tipo, url, inizio_live,"
        " stato_live, ordine, pubblicata) VALUES(?,?,?, 'live', ?,?, 'programmata', 999, 1)",
        (corso_id, body.titolo, body.descrizione, body.url.strip(), body.inizio))

    # Si avvisano subito gli allievi di quella patente: la diretta e' appena
    # comparsa in agenda, prima che qualcuno la scopra per caso aprendo l'app.
    quando = body.inizio.replace("T", " alle ") if "T" in body.inizio else body.inizio
    notifiche.notifica_utenti(
        utenti_con_patente(p.autoscuola_id, body.listato),
        "lezione_programmata", f"Nuova diretta: {body.titolo}",
        f"Programmata per il {quando}.", "/#/video")
    return {"id": cur.lastrowid}


@router.get("/video")
def elenco_video(p: Principal = Depends(require_staff)):
    return db.rows_to_dicts(db.query(
        "SELECT v.id, v.titolo, v.descrizione, v.tipo, v.url, v.durata_sec, v.inizio_live,"
        "       v.stato_live, v.pubblicata, v.ordine, l.codice AS listato,"
        "  (SELECT COUNT(*) FROM visione_video vv WHERE vv.lezione_id = v.id) AS visualizzazioni,"
        "  (SELECT COUNT(*) FROM visione_video vv WHERE vv.lezione_id = v.id AND vv.completata = 1) AS completate "
        "FROM lezioni_video v JOIN corsi c ON c.id = v.corso_id "
        "JOIN listati l ON l.id = c.listato_id "
        "WHERE c.autoscuola_id = ? ORDER BY l.codice, v.tipo DESC, v.ordine",
        (p.autoscuola_id,)))


@router.delete("/video/{video_id}")
def elimina_video(video_id: int, p: Principal = Depends(require_admin)):
    """Nasconde la lezione. Il file caricato NON viene cancellato dal disco:
    se e' stato pubblicato per errore lo si ripubblica, e in ogni caso la
    cancellazione di un file grosso e' un'operazione da fare a mente lucida."""
    cur = db.execute(
        "UPDATE lezioni_video SET pubblicata = 0 WHERE id = ? AND corso_id IN "
        "(SELECT id FROM corsi WHERE autoscuola_id = ?)", (video_id, p.autoscuola_id))
    if not cur.rowcount:
        raise HTTPException(404, "Lezione video non trovata")
    return {"ok": True}


class StatoVideoIn(BaseModel):
    pubblicata: bool | None = None
    stato_live: str | None = Field(default=None, pattern="^(programmata|in_onda|conclusa)$")
    titolo: str | None = None
    url: str | None = None


@router.put("/video/{video_id}")
def modifica_video(video_id: int, body: StatoVideoIn, p: Principal = Depends(require_admin)):
    campi = {}
    if body.pubblicata is not None:
        campi["pubblicata"] = 1 if body.pubblicata else 0
    if body.stato_live:
        campi["stato_live"] = body.stato_live
    if body.titolo:
        campi["titolo"] = body.titolo
    if body.url:
        campi["url"] = _normalizza_url(body.url)
    if not campi:
        return {"ok": True}
    sql = ("UPDATE lezioni_video SET " + ", ".join(f"{k} = ?" for k in campi) +
           " WHERE id = ? AND corso_id IN (SELECT id FROM corsi WHERE autoscuola_id = ?)")
    cur = db.execute(sql, [*campi.values(), video_id, p.autoscuola_id])
    if not cur.rowcount:
        raise HTTPException(404, "Lezione video non trovata")

    # La diretta e' appena passata "in onda": si avvisano gli allievi della
    # patente interessata, non solo chi ha gia' l'app aperta in quel momento.
    if body.pubblicata is True:
        riga = db.query_one(
            "SELECT v.titolo, v.tipo, l.codice AS listato FROM lezioni_video v "
            "JOIN corsi c ON c.id = v.corso_id JOIN listati l ON l.id = c.listato_id "
            "WHERE v.id = ?", (video_id,))
        if riga and riga["tipo"] == "registrata":
            notifiche.notifica_utenti(
                utenti_con_patente(p.autoscuola_id, riga["listato"]),
                "lezione_programmata", f"Nuova videolezione: {riga['titolo']}",
                "Disponibile ora nella sezione Videocorsi.", "/#/video")

    if body.stato_live == "in_onda":
        riga = db.query_one(
            "SELECT v.titolo, l.codice AS listato FROM lezioni_video v "
            "JOIN corsi c ON c.id = v.corso_id JOIN listati l ON l.id = c.listato_id "
            "WHERE v.id = ?", (video_id,))
        if riga:
            notifiche.notifica_utenti(
                utenti_con_patente(p.autoscuola_id, riga["listato"]),
                "diretta_iniziata", f"E\' iniziata: {riga['titolo']}",
                "Collegati ora dalla sezione Videocorsi.", "/#/video")
    return {"ok": True}


# =========================================================================== #
# 5. ANALISI DELLE CLASSI
# =========================================================================== #
# La domanda a cui questa sezione risponde e': "perche' in certe fasce orarie
# le cose vanno diversamente?".
#
# Il punto delicato e' che gli allievi NON sono legati a una classe fissa:
# frequentano quando possono, e lo stesso allievo puo' comparire in fasce
# diverse. Non si puo' quindi dire "la classe delle 14 va male": si puo' dire
# "gli allievi che frequentano prevalentemente le 14 vanno male".
#
# Perche' l'attribuzione sia onesta, ogni allievo viene assegnato alla fascia
# in cui ha la maggioranza delle proprie presenze (fascia prevalente), e le
# fasce con pochissimi dati vengono marcate come non significative invece di
# essere mostrate come se lo fossero.

SOGLIA_SIGNIFICATIVITA = 3      # allievi minimi perche' il dato sia solido
MINIMO_CONFRONTO = 2            # sotto questa soglia non si confronta affatto


def _prestazioni_allievi(autoscuola_id: int) -> dict:
    righe = db.query(
        "SELECT u.id, u.nome || ' ' || u.cognome AS nominativo, u.listato_target,"
        "  COALESCE((SELECT SUM(n_risposte) FROM stat_utente_giorno g WHERE g.utente_id = u.id), 0) AS risposte,"
        "  COALESCE((SELECT SUM(n_errori)   FROM stat_utente_giorno g WHERE g.utente_id = u.id), 0) AS errori,"
        "  COALESCE((SELECT SUM(secondi_app) FROM stat_utente_giorno g WHERE g.utente_id = u.id), 0) AS secondi,"
        "  (SELECT COUNT(*) FROM schede s WHERE s.utente_id = u.id AND s.tipo = 'simulazione'"
        "     AND s.stato = 'completata') AS simulazioni,"
        "  (SELECT COUNT(*) FROM schede s WHERE s.utente_id = u.id AND s.tipo = 'simulazione'"
        "     AND s.esito = 1) AS simulazioni_superate "
        "FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
        "WHERE u.autoscuola_id = ? AND r.codice = 'allievo' AND u.attivo = 1",
        (autoscuola_id,))
    return {r["id"]: dict(r) for r in righe}


def _riassunto(allievi: list) -> dict:
    """Media delle metriche su un gruppo di allievi, ignorando chi non ha
    ancora fatto nulla: includerlo abbasserebbe le medie senza motivo."""
    attivi = [a for a in allievi if a["risposte"] > 0]
    risposte = sum(a["risposte"] for a in attivi)
    errori = sum(a["errori"] for a in attivi)
    simul = sum(a["simulazioni"] for a in attivi)
    superate = sum(a["simulazioni_superate"] for a in attivi)
    return {
        "allievi": len(allievi),
        "allievi_attivi": len(attivi),
        "tasso_errore_pct": round(100 * errori / risposte, 1) if risposte else None,
        "simulazioni": simul,
        "simulazioni_superate": superate,
        "percentuale_superate": round(100 * superate / simul, 1) if simul else None,
        "ore_studio_medie": round(sum(a["secondi"] for a in attivi) / 3600 / len(attivi), 1)
                            if attivi else 0.0,
        "risposte_medie": round(risposte / len(attivi)) if attivi else 0,
    }


@router.get("/analisi")
def analisi(dal: str | None = None, p: Principal = Depends(require_staff)):
    dal = dal or (date.today() - timedelta(days=90)).isoformat()

    slot = db.rows_to_dicts(db.query(
        "SELECT id, giorno, ora_inizio, ora_fine, listato, aula, docente, attivo "
        "FROM aula_slot WHERE autoscuola_id = ? ORDER BY giorno, ora_inizio",
        (p.autoscuola_id,)))

    presenze = db.query(
        "SELECT l.slot_id, l.id AS lezione_id, l.data, l.ora_inizio, l.listato,"
        "       pr.utente_id "
        "FROM aula_lezione l LEFT JOIN aula_presenza pr "
        "  ON pr.lezione_id = l.id AND pr.stato = 'presente' "
        "WHERE l.autoscuola_id = ? AND l.data >= ? AND l.stato != 'annullata'",
        (p.autoscuola_id, dal))

    prestazioni = _prestazioni_allievi(p.autoscuola_id)

    # Presenze per slot e per allievo
    lezioni_per_slot: dict = {}
    presenze_per_slot: dict = {}
    presenze_allievo_slot: dict = {}
    for r in presenze:
        sid = r["slot_id"]
        lezioni_per_slot.setdefault(sid, set()).add(r["lezione_id"])
        if r["utente_id"]:
            presenze_per_slot.setdefault(sid, []).append(r["utente_id"])
            presenze_allievo_slot.setdefault(r["utente_id"], {})
            presenze_allievo_slot[r["utente_id"]][sid] = \
                presenze_allievo_slot[r["utente_id"]].get(sid, 0) + 1

    # Fascia prevalente di ciascun allievo
    prevalente: dict = {}
    for utente_id, conteggi in presenze_allievo_slot.items():
        prevalente[utente_id] = max(conteggi.items(), key=lambda x: x[1])[0]

    risultati = []
    for s in slot:
        sid = s["id"]
        n_lezioni = len(lezioni_per_slot.get(sid, ()))
        tutte = presenze_per_slot.get(sid, [])
        fedeli = [u for u, sl in prevalente.items() if sl == sid and u in prestazioni]
        gruppo = [prestazioni[u] for u in fedeli]

        risultati.append({
            **s,
            "nome_giorno": GIORNI[s["giorno"]],
            "fascia": f"{s['ora_inizio']}-{s['ora_fine']}",
            "lezioni_svolte": n_lezioni,
            "presenze_totali": len(tutte),
            "allievi_distinti": len(set(tutte)),
            "media_presenti": round(len(tutte) / n_lezioni, 1) if n_lezioni else 0.0,
            "significativo": len(fedeli) >= SOGLIA_SIGNIFICATIVITA,
            "prestazioni": _riassunto(gruppo),
        })

    # Confronto per parte della giornata: e' la lettura che l'autoscuola usa
    # davvero quando deve decidere dove spostare un docente.
    def _parte(ora: str) -> str:
        h = int(ora[:2])
        return "mattina" if h < 13 else "pomeriggio" if h < 18 else "sera"

    parti: dict = {}
    for r in risultati:
        chiave = _parte(r["ora_inizio"])
        v = parti.setdefault(chiave, {"lezioni": 0, "presenze": 0, "allievi": set()})
        v["lezioni"] += r["lezioni_svolte"]
        v["presenze"] += r["presenze_totali"]
        v["allievi"].update(u for u, sl in prevalente.items() if sl == r["id"])

    per_parte = []
    for nome, v in parti.items():
        gruppo = [prestazioni[u] for u in v["allievi"] if u in prestazioni]
        per_parte.append({
            "parte": nome, "lezioni": v["lezioni"], "presenze": v["presenze"],
            "media_presenti": round(v["presenze"] / v["lezioni"], 1) if v["lezioni"] else 0.0,
            "prestazioni": _riassunto(gruppo),
        })

    return {
        "dal": dal,
        "slot": risultati,
        "per_parte_giornata": sorted(per_parte, key=lambda x: x["parte"]),
        "senza_presenze": sum(1 for r in risultati if r["presenze_totali"] == 0),
        "osservazioni": _osservazioni(risultati, per_parte),
        "nota_metodo": (
            "Gli allievi sono attribuiti alla fascia in cui hanno la maggior parte "
            "delle presenze, perche' la frequenza e' libera e lo stesso allievo puo' "
            f"cambiare orario. Le fasce con almeno {SOGLIA_SIGNIFICATIVITA} allievi "
            "stabili sono marcate come solide; sotto quella soglia il confronto viene "
            "mostrato ma dichiarato indicativo."),
    }


def _osservazioni(slot: list, parti: list) -> list:
    """Traduce i numeri in frasi utili.

    Serve perche' una tabella di percentuali non dice all'amministratore cosa
    fare: queste righe indicano dove guardare, sempre dichiarando su quanti
    dati si basano.
    """
    note = []
    # Si confrontano le fasce con almeno due allievi stabili. Sotto la soglia
    # di solidita' il confronto viene comunque mostrato, ma dichiarato come
    # indicativo: nascondere il dato a un'autoscuola piccola significherebbe
    # non dirle mai niente.
    validi = [s for s in slot
              if s["prestazioni"]["tasso_errore_pct"] is not None
              and s["prestazioni"]["allievi_attivi"] >= MINIMO_CONFRONTO]

    if len(validi) >= 2:
        peggiore = max(validi, key=lambda s: s["prestazioni"]["tasso_errore_pct"])
        migliore = min(validi, key=lambda s: s["prestazioni"]["tasso_errore_pct"])
        delta = (peggiore["prestazioni"]["tasso_errore_pct"]
                 - migliore["prestazioni"]["tasso_errore_pct"])
        if delta >= 5:
            solido = (peggiore["significativo"] and migliore["significativo"])
            note.append(
                f"Chi frequenta {peggiore['nome_giorno']} {peggiore['fascia']} sbaglia il "
                f"{peggiore['prestazioni']['tasso_errore_pct']}% contro il "
                f"{migliore['prestazioni']['tasso_errore_pct']}% di {migliore['nome_giorno']} "
                f"{migliore['fascia']}: {delta:.1f} punti di differenza su "
                f"{peggiore['prestazioni']['allievi_attivi']} e "
                f"{migliore['prestazioni']['allievi_attivi']} allievi."
                + ("" if solido else " Dato ancora indicativo: servono piu' allievi stabili."))

    frequentati = [s for s in slot if s["lezioni_svolte"] >= 2]
    if len(frequentati) >= 2:
        vuoto = min(frequentati, key=lambda s: s["media_presenti"])
        pieno = max(frequentati, key=lambda s: s["media_presenti"])
        if pieno["media_presenti"] - vuoto["media_presenti"] >= 2:
            note.append(
                f"{vuoto['nome_giorno']} {vuoto['fascia']} raccoglie in media "
                f"{vuoto['media_presenti']} allievi contro i {pieno['media_presenti']} di "
                f"{pieno['nome_giorno']} {pieno['fascia']}: valuta se accorpare o spostare.")

    validi_parti = [p for p in parti if p["prestazioni"]["tasso_errore_pct"] is not None
                    and p["prestazioni"]["allievi_attivi"] >= MINIMO_CONFRONTO]
    if len(validi_parti) >= 2:
        ordinati = sorted(validi_parti, key=lambda x: x["prestazioni"]["tasso_errore_pct"])
        note.append(
            f"Per parte della giornata il risultato migliore e' la {ordinati[0]['parte']} "
            f"({ordinati[0]['prestazioni']['tasso_errore_pct']}% di errori), il peggiore la "
            f"{ordinati[-1]['parte']} ({ordinati[-1]['prestazioni']['tasso_errore_pct']}%).")

    se_vuoti = [s for s in slot if s["lezioni_svolte"] > 0 and s["presenze_totali"] == 0]
    if se_vuoti:
        note.append(f"{len(se_vuoti)} fasce hanno lezioni generate ma nessuna presenza "
                    f"registrata: se le lezioni si sono tenute, mancano le spunte.")

    if not note:
        note.append("Servono piu' presenze registrate per un confronto affidabile: "
                    "l'analisi diventa utile dopo qualche settimana di lezioni.")
    return note
