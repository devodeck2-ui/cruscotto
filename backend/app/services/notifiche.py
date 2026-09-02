"""Notifiche push (Web Push) e storico in-app.

Due canali che si completano a vicenda:
  - push:    arriva come popup del sistema operativo anche a sito chiuso, ma
             richiede che l'utente abbia dato il permesso del browser e abbia
             gia' una "iscrizione" salvata (tabella push_subscription) per
             almeno un dispositivo.
  - storico: la tabella `notifica` viene scritta comunque, a prescindere dal
             permesso push, cosi' il campanellino nell'interfaccia funziona
             anche per chi ha rifiutato le notifiche o e' su iPhone prima di
             aver installato il sito come app (dove il push non esiste).

Le chiavi VAPID (AC_VAPID_PUBLIC_KEY / AC_VAPID_PRIVATE_KEY) si generano una
sola volta e non sono legate a nessun servizio esterno a pagamento - vedi
scripts/genera_chiavi_vapid.py. Se non sono configurate, il push viene
semplicemente saltato: lo storico in-app resta comunque disponibile.
"""
from __future__ import annotations

import json
import logging

from .. import db
from ..config import settings

logger = logging.getLogger(__name__)


def notifica_utenti(utente_ids: list[int], tipo: str, titolo: str,
                    corpo: str = "", url: str = "/") -> int:
    """Scrive una notifica per ciascun utente e prova a spingerla via push.

    Ritorna quante notifiche sono state create. Il fallimento della sola
    push (dispositivo offline, permesso mai dato, chiavi non configurate)
    non fa fallire la chiamata: lo storico in-app resta comunque scritto.
    """
    ids = sorted(set(utente_ids or []))
    if not ids:
        return 0

    for uid in ids:
        cur = db.execute(
            "INSERT INTO notifica(utente_id, tipo, titolo, corpo, url) VALUES(?,?,?,?,?)",
            (uid, tipo, titolo, corpo, url))
        if settings.vapid_public_key and settings.vapid_private_key:
            _invia_push(uid, cur.lastrowid, titolo, corpo, url)
    return len(ids)


def _invia_push(utente_id: int, notifica_id: int, titolo: str, corpo: str, url: str) -> None:
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return  # libreria non installata: si resta al solo storico in-app

    righe = db.rows_to_dicts(db.query(
        "SELECT id, endpoint, p256dh, auth FROM push_subscription WHERE utente_id = ?",
        (utente_id,)))
    if not righe:
        return

    payload = json.dumps({"titolo": titolo, "corpo": corpo, "url": url})
    inviata_almeno_una = False
    for r in righe:
        try:
            webpush(
                subscription_info={"endpoint": r["endpoint"],
                                   "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}},
                data=payload,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
            inviata_almeno_una = True
        except WebPushException as e:
            codice = getattr(e.response, "status_code", None)
            if codice in (404, 410):
                # Il browser ha revocato l'iscrizione (disinstallata, cache
                # svuotata): si toglie, non ha senso riprovare all'infinito
                # su un dispositivo che non la riconosce piu'.
                db.execute("DELETE FROM push_subscription WHERE id = ?", (r["id"],))
            else:
                logger.warning("push fallita per utente %s: %s", utente_id, e)
        except Exception as e:
            logger.warning("push fallita per utente %s: %s", utente_id, e)

    if inviata_almeno_una:
        db.execute("UPDATE notifica SET inviata_push = 1 WHERE id = ?", (notifica_id,))
