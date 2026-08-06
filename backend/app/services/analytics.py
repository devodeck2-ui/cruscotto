"""Motore analitico: aggregazione write-time e letture O(#argomenti).

PERCHE' NON CALCOLARE AL VOLO
    La query "tasso di errore per argomento di tutti gli allievi del tenant"
    su `risposte` e' una scansione con GROUP BY che cresce linearmente con lo
    storico: a 500 allievi x 6 mesi sono milioni di righe. La dashboard admin
    verrebbe aperta decine di volte al giorno, ricalcolando ogni volta lo
    stesso risultato.

STRATEGIA
    Aggregazione incrementale nella stessa transazione della scrittura
    (write-time rollup). Ogni risposta aggiorna due contatori:
        stat_utente_argomento  (per la mappa delle criticita' e per l'SRS)
        stat_utente_giorno     (per le serie temporali e i tempi d'uso)
    Costo: due UPSERT su chiave primaria, ~0.05 ms. Le letture diventano
    scansioni di poche centinaia di righe.

INTEGRITA'
    Gli aggregati sono derivati: `risposte` resta l'unica fonte di verita'.
    `ricostruisci_aggregati()` li rigenera da zero ed e' idempotente - va
    eseguita dopo import massivi, migrazioni o in caso di sospetta deriva.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .. import db
from .srs import ALPHA


def registra_risposta(con, utente_id: int, listato_id: int, capitolo_id: int | None,
                      argomento_id: int | None, corretta: bool, tempo_ms: int) -> None:
    ora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    err = 0 if corretta else 1

    if argomento_id:
        con.execute(
            "INSERT INTO stat_utente_argomento(utente_id, argomento_id, capitolo_id, listato_id,"
            " n_risposte, n_errori, tempo_tot_ms, ultimo_errore, ultima_att, ema_errore) "
            "VALUES(?,?,?,?,1,?,?,?,?,?) "
            "ON CONFLICT(utente_id, argomento_id) DO UPDATE SET "
            "  n_risposte    = n_risposte + 1,"
            "  n_errori      = n_errori + ?,"
            "  tempo_tot_ms  = tempo_tot_ms + ?,"
            "  ultimo_errore = CASE WHEN ? = 1 THEN ? ELSE ultimo_errore END,"
            "  ultima_att    = ?,"
            f"  ema_errore    = {ALPHA} * ? + (1 - {ALPHA}) * ema_errore",
            (utente_id, argomento_id, capitolo_id, listato_id, err, tempo_ms,
             ora if err else None, ora, float(err),
             err, tempo_ms, err, ora, ora, float(err)))

    giorno = date.today().isoformat()
    con.execute(
        "INSERT INTO stat_utente_giorno(utente_id, giorno, n_risposte, n_errori) "
        "VALUES(?,?,1,?) ON CONFLICT(utente_id, giorno) DO UPDATE SET "
        " n_risposte = n_risposte + 1, n_errori = n_errori + ?",
        (utente_id, giorno, err, err))


def registra_scheda_conclusa(con, utente_id: int, tipo: str, superata: bool) -> None:
    col = {"esercitazione": "schede_eserc", "simulazione": "schede_simul",
           "recupero": "schede_recup"}[tipo]
    giorno = date.today().isoformat()
    sup = 1 if (tipo == "simulazione" and superata) else 0
    con.execute(
        f"INSERT INTO stat_utente_giorno(utente_id, giorno, {col}, simul_superate) "
        f"VALUES(?,?,1,?) ON CONFLICT(utente_id, giorno) DO UPDATE SET "
        f" {col} = {col} + 1, simul_superate = simul_superate + ?",
        (utente_id, giorno, sup, sup))


def aggiorna_difficolta_domanda(con, domanda_id: int, corretta: bool) -> None:
    """Difficolta' globale = media mobile della probabilita' d'errore su tutta
    la popolazione. Alimenta il ranking delle 'domande piu' sbagliate' e il
    riempimento delle schede di recupero per gli allievi senza storico."""
    con.execute(
        "UPDATE domande SET n_somministr = n_somministr + 1,"
        " difficolta = ((difficolta * n_somministr) + ?) / (n_somministr + 1) "
        "WHERE id = ?", (0.0 if corretta else 1.0, domanda_id))


# --------------------------------------------------------------------------- #
# Letture
# --------------------------------------------------------------------------- #

def criticita_utente(utente_id: int, limite: int = 12) -> list[dict]:
    return db.rows_to_dicts(db.query(
        "SELECT * FROM v_criticita_argomento WHERE utente_id = ? AND n_risposte >= 3 "
        "ORDER BY ema_errore DESC, tasso_errore_pct DESC LIMIT ?", (utente_id, limite)))


def riepilogo_capitoli(utente_id: int, listato_id: int) -> list[dict]:
    return db.rows_to_dicts(db.query(
        "SELECT c.id AS capitolo_id, c.titolo, "
        "       COALESCE(SUM(s.n_risposte),0) AS n_risposte,"
        "       COALESCE(SUM(s.n_errori),0)   AS n_errori,"
        "       ROUND(100.0 * COALESCE(SUM(s.n_errori),0) / NULLIF(SUM(s.n_risposte),0), 1) AS tasso_errore_pct,"
        "       (SELECT COUNT(*) FROM domande d WHERE d.capitolo_id = c.id AND d.attiva = 1) AS n_domande "
        "FROM capitoli c "
        "LEFT JOIN stat_utente_argomento s ON s.capitolo_id = c.id AND s.utente_id = ? "
        "WHERE c.listato_id = ? GROUP BY c.id ORDER BY c.ordine", (utente_id, listato_id)))


def serie_temporale(utente_id: int, giorni: int = 30) -> list[dict]:
    return db.rows_to_dicts(db.query(
        "SELECT giorno, secondi_app, n_risposte, n_errori, schede_eserc, schede_simul,"
        "       schede_recup, simul_superate,"
        "       ROUND(100.0 * n_errori / NULLIF(n_risposte,0), 1) AS tasso_errore_pct "
        "FROM stat_utente_giorno WHERE utente_id = ? "
        "ORDER BY giorno DESC LIMIT ?", (utente_id, giorni)))


def panoramica_tenant(autoscuola_id: int) -> dict:
    tot = db.query_one(
        "SELECT COUNT(*) AS allievi,"
        "       COALESCE(SUM(v.secondi_totali),0) AS secondi,"
        "       COALESCE(SUM(v.schede_esercitazione),0) AS eserc,"
        "       COALESCE(SUM(v.schede_simulazione),0)   AS simul,"
        "       COALESCE(SUM(v.simulazioni_superate),0) AS superate,"
        "       COALESCE(SUM(v.risposte_totali),0)      AS risposte,"
        "       COALESCE(SUM(v.errori_totali),0)        AS errori "
        "FROM v_progresso_allievo v JOIN utenti u ON u.id = v.utente_id "
        "JOIN ruoli r ON r.id = u.ruolo_id "
        "WHERE v.autoscuola_id = ? AND r.codice = 'allievo'", (autoscuola_id,))
    d = dict(tot) if tot else {}
    d["tasso_errore_pct"] = round(100.0 * d.get("errori", 0) / d["risposte"], 2) if d.get("risposte") else None
    d["ore"] = round(d.get("secondi", 0) / 3600, 1)
    return d


def colli_di_bottiglia(autoscuola_id: int, limite: int = 10) -> list[dict]:
    """Argomenti in cui l'INTERA classe fatica: e' il segnale che dice
    all'autoscuola quale lezione in aula va rifatta."""
    return db.rows_to_dicts(db.query(
        "SELECT a.id AS argomento_id, a.titolo AS argomento, c.titolo AS capitolo,"
        "       SUM(s.n_risposte) AS n_risposte, SUM(s.n_errori) AS n_errori,"
        "       COUNT(DISTINCT s.utente_id) AS allievi_coinvolti,"
        "       ROUND(100.0 * SUM(s.n_errori) / NULLIF(SUM(s.n_risposte),0), 1) AS tasso_errore_pct "
        "FROM stat_utente_argomento s "
        "JOIN utenti u    ON u.id = s.utente_id AND u.autoscuola_id = ? "
        "JOIN argomenti a ON a.id = s.argomento_id "
        "JOIN capitoli  c ON c.id = s.capitolo_id "
        "GROUP BY a.id HAVING SUM(s.n_risposte) >= 10 "
        "ORDER BY tasso_errore_pct DESC, n_risposte DESC LIMIT ?", (autoscuola_id, limite)))


def domande_piu_sbagliate(autoscuola_id: int, limite: int = 20) -> list[dict]:
    return db.rows_to_dicts(db.query(
        "SELECT d.id, d.testo, c.titolo AS capitolo, i.percorso AS immagine,"
        "       COUNT(*) AS somministrazioni,"
        "       SUM(CASE WHEN r.corretta = 0 THEN 1 ELSE 0 END) AS errori,"
        "       ROUND(100.0 * SUM(CASE WHEN r.corretta = 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS tasso_errore_pct "
        "FROM risposte r "
        "JOIN schede s        ON s.id = r.scheda_id AND s.autoscuola_id = ? "
        "JOIN domande d       ON d.id = r.domanda_id "
        "LEFT JOIN capitoli c ON c.id = d.capitolo_id "
        "LEFT JOIN immagini i ON i.id = d.immagine_id "
        "WHERE r.corretta IS NOT NULL "
        "GROUP BY d.id HAVING COUNT(*) >= 5 "
        "ORDER BY tasso_errore_pct DESC, somministrazioni DESC LIMIT ?", (autoscuola_id, limite)))


def indice_prontezza(utente_id: int) -> dict:
    """Punteggio 0-100 che stima la probabilita' di superare l'esame.

    Combina quattro segnali normalizzati, pesati per rilevanza predittiva:
        45%  esito delle ultime 5 simulazioni (il proxy piu' diretto)
        25%  copertura del listato (quante domande distinte affrontate)
        20%  tasso d'errore recente (EMA media sugli argomenti)
        10%  costanza (giorni attivi nelle ultime due settimane)
    Il peso non e' arbitrario: la simulazione replica le condizioni d'esame,
    la copertura previene il falso positivo di chi ripete sempre le stesse
    domande, la costanza intercetta chi ha studiato solo una volta.
    """
    sim = db.query(
        "SELECT esito, n_errori FROM schede WHERE utente_id = ? AND tipo = 'simulazione' "
        "AND stato = 'completata' ORDER BY conclusa_il DESC LIMIT 5", (utente_id,))
    s_sim = (sum(1 for r in sim if r["esito"]) / len(sim)) if sim else 0.0

    cop = db.query_one(
        "SELECT (SELECT COUNT(DISTINCT domanda_id) FROM risposte WHERE utente_id = ? "
        "        AND corretta IS NOT NULL) AS viste,"
        "       (SELECT COUNT(*) FROM domande d JOIN utenti u ON u.id = ? "
        "        JOIN listati l ON l.codice = u.listato_target "
        "        WHERE d.listato_id = l.id AND d.attiva = 1) AS totali",
        (utente_id, utente_id))
    s_cop = min(1.0, (cop["viste"] / cop["totali"])) if cop and cop["totali"] else 0.0
    s_cop = min(1.0, s_cop / 0.35)      # coprire il 35% del listato vale gia' 100%

    ema = db.query_one("SELECT AVG(ema_errore) AS e FROM stat_utente_argomento "
                       "WHERE utente_id = ? AND n_risposte >= 3", (utente_id,))
    s_err = 1.0 - min(1.0, (ema["e"] or 0.5) / 0.35)

    att = db.query_one(
        "SELECT COUNT(*) AS g FROM stat_utente_giorno WHERE utente_id = ? "
        "AND giorno >= date('now','-14 day') AND n_risposte > 0", (utente_id,))
    s_cost = min(1.0, (att["g"] if att else 0) / 10.0)

    score = 100 * (0.45 * s_sim + 0.25 * s_cop + 0.20 * s_err + 0.10 * s_cost)
    livello = "pronto" if score >= 75 else "quasi" if score >= 50 else "in formazione"
    return {"punteggio": round(score, 1), "livello": livello,
            "dettaglio": {"simulazioni": round(s_sim, 2), "copertura": round(s_cop, 2),
                          "accuratezza": round(s_err, 2), "costanza": round(s_cost, 2)}}


def ricostruisci_aggregati() -> dict:
    """Rigenerazione completa degli aggregati dalle tabelle sorgente."""
    with db.transaction() as con:
        con.execute("DELETE FROM stat_utente_argomento")
        con.execute(
            "INSERT INTO stat_utente_argomento(utente_id, argomento_id, capitolo_id, listato_id,"
            " n_risposte, n_errori, tempo_tot_ms, ultima_att, ema_errore) "
            "SELECT r.utente_id, r.argomento_id, r.capitolo_id, s.listato_id, COUNT(*),"
            "       SUM(CASE WHEN r.corretta = 0 THEN 1 ELSE 0 END), SUM(r.tempo_ms),"
            "       MAX(r.risposto_il),"
            "       1.0 * SUM(CASE WHEN r.corretta = 0 THEN 1 ELSE 0 END) / COUNT(*) "
            "FROM risposte r JOIN schede s ON s.id = r.scheda_id "
            "WHERE r.corretta IS NOT NULL AND r.argomento_id IS NOT NULL "
            "GROUP BY r.utente_id, r.argomento_id")
        n = con.execute("SELECT COUNT(*) FROM stat_utente_argomento").fetchone()[0]
    return {"righe_ricostruite": n}
