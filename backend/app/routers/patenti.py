"""Piu' patenti per lo stesso allievo.

Un ragazzo che a quattordici anni prende il patentino AM e a diciotto la B e'
lo stesso allievo, iscritto a due percorsi. Prima l'app ne ammetteva uno solo
(utenti.listato_target) e per seguirne due bisognava creare due account, con
statistiche spezzate in due e due password da ricordare.

COME E' FATTO
    Resta listato_target come patente PRINCIPALE, quella che comanda la
    simulazione d'esame e il conto alla rovescia. Si aggiunge la colonna
    listati_extra con le altre, separate da virgola.

    Poteva essere una tabella di collegamento, piu' ortodossa. Ho scelto la
    colonna perche' le patenti per allievo sono due o tre, non venti, e
    perche' cosi' l'aggiornamento non tocca nessuna delle query esistenti:
    chi legge solo listato_target continua a funzionare come prima.

COSA CAMBIA PER L'ALLIEVO
    Vede i videocorsi e puo' esercitarsi su tutte le patenti che ha, e solo
    su quelle: fino a ieri l'elenco dei listati mostrava tutti i nove
    disponibili, compresi CQC e CAP che a un diciottenne non servono.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..rbac import Principal, current_user

router = APIRouter(prefix="/api/mie-patenti", tags=["patenti"])


def codici_utente(utente_id: int) -> list[str]:
    """Tutte le patenti dell'allievo, la principale per prima."""
    r = db.query_one("SELECT listato_target, listati_extra FROM utenti WHERE id = ?",
                     (utente_id,))
    if not r:
        return []
    codici = [r["listato_target"]] if r["listato_target"] else []
    for c in (r["listati_extra"] or "").split(","):
        c = c.strip()
        if c and c not in codici:
            codici.append(c)
    return codici


def filtro_sql(utente_id: int, colonna: str) -> tuple[str, list]:
    """Frammento di WHERE per limitare una query alle patenti dell'allievo.

    Ritorna ('' , []) se non ce ne sono: meglio non filtrare affatto che
    restituire una lista vuota e far sembrare l'app rotta.
    """
    codici = codici_utente(utente_id)
    if not codici:
        return "", []
    segnaposto = ",".join("?" * len(codici))
    return f" AND {colonna} IN ({segnaposto})", codici


def imposta(utente_id: int, patenti: list[str]) -> list[str]:
    """Salva l'elenco: la prima diventa la principale, le altre le aggiuntive."""
    pulite = []
    for c in patenti or []:
        c = (c or "").strip().upper()
        if c and c not in pulite:
            pulite.append(c)
    if not pulite:
        return codici_utente(utente_id)
    db.execute("UPDATE utenti SET listato_target = ?, listati_extra = ? WHERE id = ?",
               (pulite[0], ",".join(pulite[1:]), utente_id))
    return pulite


@router.get("")
def mie_patenti(p: Principal = Depends(current_user)):
    """Elenco per l'interfaccia: codice, nome e regole d'esame di ciascuna."""
    codici = codici_utente(p.utente_id)
    if not codici:
        return {"codici": [], "listati": []}
    segnaposto = ",".join("?" * len(codici))
    righe = db.rows_to_dicts(db.query(
        f"SELECT id, codice, nome, domande_esame, minuti_esame, errori_max "
        f"FROM listati WHERE codice IN ({segnaposto}) AND attivo = 1", codici))
    # Si rispetta l'ordine dell'allievo, non quello del database: la prima
    # e' la patente che sta preparando adesso.
    ordinate = sorted(righe, key=lambda r: codici.index(r["codice"]))
    return {"codici": codici, "listati": ordinate}
