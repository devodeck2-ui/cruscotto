"""Ripetizione mirata (spaced repetition).

Il problema reale dell'allievo non e' "rivedere tutte le domande", ma
"tornare sulle domande che sta per dimenticare, nei capitoli in cui e' debole".
L'algoritmo lavora quindi su due livelli:

  LIVELLO 1 - DOMANDA (SM-2 semplificato)
      Ogni coppia (utente, domanda) ha un intervallo di ripasso che si allunga
      dopo ogni successo e si azzera dopo ogni errore. La facilita' (E-Factor)
      modula la crescita: le domande soggettivamente difficili tornano prima.
      Costo: O(1) per risposta, una UPSERT su chiave primaria.

  LIVELLO 2 - ARGOMENTO (EMA del tasso d'errore)
      Il tasso di errore grezzo (errori/risposte) e' una metrica pigra: un
      allievo che sbagliava tutto a gennaio resta "critico" a marzo anche se
      nel frattempo ha imparato. Si usa quindi una media mobile esponenziale
          ema <- alpha * errore + (1 - alpha) * ema      con alpha = 0.30
      aggiornata a ogni risposta. Reagisce in ~10 risposte, non richiede di
      leggere lo storico, e sta in una sola colonna REAL.

  COMPOSIZIONE DELLA SCHEDA DI RECUPERO
      60% domande scadute secondo SM-2 (prossima_il <= oggi)
      30% domande mai viste appartenenti agli argomenti con EMA piu' alta
      10% domande gia' superate, come controllo di ritenzione
      La quota mancante viene compensata dalle categorie rimanenti, cosi' la
      scheda e' sempre piena anche per un allievo al primo accesso.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from .. import db

ALPHA = 0.30           # reattivita' della media mobile
EF_MIN = 1.3
INTERVALLI = [0, 1, 3, 7, 16, 35]   # giorni per le prime ripetizioni


def aggiorna_stato(con, utente_id: int, domanda_id: int, argomento_id: int | None,
                   corretta: bool) -> None:
    """UPSERT dello stato SM-2. Eseguita nella stessa transazione della risposta."""
    oggi = date.today()
    r = con.execute("SELECT ripetizioni, facilita, intervallo_g, n_errori "
                    "FROM srs_stato WHERE utente_id = ? AND domanda_id = ?",
                    (utente_id, domanda_id)).fetchone()
    rip, ef, n_err = (r["ripetizioni"], r["facilita"], r["n_errori"]) if r else (0, 2.5, 0)

    if corretta:
        rip += 1
        ef = max(EF_MIN, ef + 0.1)
        intervallo = INTERVALLI[rip] if rip < len(INTERVALLI) else \
            int(round(INTERVALLI[-1] * (ef ** (rip - len(INTERVALLI) + 1))))
    else:
        rip = 0
        ef = max(EF_MIN, ef - 0.25)
        intervallo = 0            # ritorna nel pool immediato
        n_err += 1

    prossima = (oggi + timedelta(days=intervallo)).isoformat()
    con.execute(
        "INSERT INTO srs_stato(utente_id, domanda_id, argomento_id, ripetizioni, facilita,"
        " intervallo_g, prossima_il, n_errori, ultima_il) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(utente_id, domanda_id) DO UPDATE SET "
        " ripetizioni=excluded.ripetizioni, facilita=excluded.facilita,"
        " intervallo_g=excluded.intervallo_g, prossima_il=excluded.prossima_il,"
        " n_errori=excluded.n_errori, ultima_il=excluded.ultima_il",
        (utente_id, domanda_id, argomento_id, rip, ef, intervallo, prossima, n_err,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))


def seleziona_recupero(utente_id: int, listato_id: int, n: int) -> list[int]:
    oggi = date.today().isoformat()
    quota_scadute = int(n * 0.6)
    quota_nuove = int(n * 0.3)

    # 1) Domande scadute, le piu' sbagliate prima (range scan su ix_srs_due)
    scadute = [r[0] for r in db.query(
        "SELECT s.domanda_id FROM srs_stato s JOIN domande d ON d.id = s.domanda_id "
        "WHERE s.utente_id = ? AND d.listato_id = ? AND s.prossima_il <= ? AND d.attiva = 1 "
        "ORDER BY s.n_errori DESC, s.prossima_il ASC LIMIT ?",
        (utente_id, listato_id, oggi, quota_scadute * 3))]
    random.shuffle(scadute)
    scelte = scadute[:quota_scadute]

    # 2) Argomenti critici per EMA -> domande mai somministrate
    critici = [r[0] for r in db.query(
        "SELECT argomento_id FROM stat_utente_argomento "
        "WHERE utente_id = ? AND listato_id = ? AND n_risposte >= 3 "
        "ORDER BY ema_errore DESC, n_errori DESC LIMIT 8",
        (utente_id, listato_id))]
    if critici:
        ph = ",".join("?" * len(critici))
        nuove = [r[0] for r in db.query(
            f"SELECT d.id FROM domande d "
            f"LEFT JOIN srs_stato s ON s.domanda_id = d.id AND s.utente_id = ? "
            f"WHERE d.argomento_id IN ({ph}) AND d.attiva = 1 AND s.domanda_id IS NULL "
            f"LIMIT ?", [utente_id, *critici, quota_nuove * 4])]
        random.shuffle(nuove)
        scelte += nuove[:quota_nuove]

    # 3) Riempimento: controllo di ritenzione + campionamento libero
    if len(scelte) < n:
        esclusi = set(scelte)
        extra = [r[0] for r in db.query(
            "SELECT d.id FROM domande d WHERE d.listato_id = ? AND d.attiva = 1 "
            "ORDER BY d.difficolta DESC LIMIT ?", (listato_id, (n - len(scelte)) * 6))]
        random.shuffle(extra)
        for i in extra:
            if i not in esclusi:
                scelte.append(i)
                esclusi.add(i)
            if len(scelte) >= n:
                break

    random.shuffle(scelte)
    return scelte[:n]


def domande_da_ripassare(utente_id: int) -> int:
    r = db.query_one("SELECT COUNT(*) AS n FROM srs_stato "
                     "WHERE utente_id = ? AND prossima_il <= ?",
                     (utente_id, date.today().isoformat()))
    return r["n"] if r else 0
