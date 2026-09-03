"""Popola il database con classi, allievi e attivita' credibile.

A cosa serve: mostrare l'applicazione a qualcuno. Dashboard, statistiche,
grafici e indice di prontezza su un database appena installato sono tutti a
zero, e un prodotto vuoto non si riesce a raccontare. Questo script riempie un
mese di vita di un'autoscuola.

COSA CREA
  * tre classi ("Serale B", "Diurno B", "CQC sabato") e ci mette dentro gli
    allievi gia' presenti, secondo la patente che stanno preparando;
  * assegna alle classi le videolezioni gia' caricate - senza, dopo
    l'introduzione delle classi non le vedrebbe piu' nessuno;
  * per ogni allievo un mese di esercitazioni e simulazioni d'esame, con un
    profilo diverso a testa: chi e' pronto, chi ci sta arrivando, chi va male,
    chi ha smesso di esercitarsi da due settimane. Servono tutti e quattro:
    una dashboard in cui sono tutti bravi non dimostra niente.

COSA NON TOCCA
  Il catalogo domande e gli allievi veri: non ne crea e non ne cancella. Le
  attivita' generate sono riconoscibili - hanno `parametri` con "demo": true -
  e `--pulisci` le toglie tutte senza toccare quelle vere.

    python3 scripts/genera_demo.py --prova
    python3 scripts/genera_demo.py
    python3 scripts/genera_demo.py --pulisci
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone

from _comune import log

# Profili: quanto e' bravo, quanto spesso studia, e per quanti giorni indietro.
# "molla" = giorni fa in cui ha smesso, per far comparire lo stato
# "non si esercita" (piu' di 7 giorni di silenzio) senza inventarne uno finto.
PROFILI = {
    "pronto":        dict(bravura=0.90, giorni_su_30=22, molla=0),
    "quasi":         dict(bravura=0.80, giorni_su_30=16, molla=0),
    "in_difficolta": dict(bravura=0.62, giorni_su_30=14, molla=0),
    "fermo":         dict(bravura=0.74, giorni_su_30=9,  molla=12),
}

CLASSI = [
    ("Serale B - ottobre", "Lezioni martedì e giovedì, 19:00-21:00", "B", "#e8543f"),
    ("Diurno B - ottobre", "Lezioni lunedì e mercoledì, 15:00-17:00", "B", "#3f7fe8"),
    ("CQC sabato", "Sabato mattina, 9:00-13:00", "CQC", "#2fa36b"),
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def main() -> int:
    ap = argparse.ArgumentParser(description="Genera dati dimostrativi.")
    ap.add_argument("--prova", action="store_true", help="dice cosa farebbe, senza scrivere")
    ap.add_argument("--pulisci", action="store_true", help="cancella solo le attivita' generate qui")
    ap.add_argument("--giorni", type=int, default=30, help="quanti giorni indietro (default 30)")
    ap.add_argument("--seed", type=int, default=20260904, help="per risultati ripetibili")
    args = ap.parse_args()

    from app import db

    rnd = random.Random(args.seed)

    if args.pulisci:
        schede = [r["id"] for r in db.query(
            "SELECT id FROM schede WHERE json_extract(parametri, '$.demo') = 1")]
        for sid in schede:
            db.execute("DELETE FROM risposte WHERE scheda_id = ?", (sid,))
            db.execute("DELETE FROM schede WHERE id = ?", (sid,))
        db.execute("DELETE FROM stat_utente_giorno")
        db.execute("DELETE FROM stat_utente_argomento")
        log(f"tolte {len(schede)} schede dimostrative; rilancia ricostruisci_aggregati.py")
        return 0

    scuola = db.query_one("SELECT id, ragione_sociale FROM autoscuole ORDER BY id LIMIT 1")
    if not scuola:
        log("nessuna autoscuola nel database")
        return 1
    aid = scuola["id"]

    allievi = db.rows_to_dicts(db.query(
        "SELECT u.id, u.nome, u.cognome, u.username, u.listato_target FROM utenti u "
        "JOIN ruoli r ON r.id = u.ruolo_id WHERE u.autoscuola_id = ? AND r.codice = 'allievo' "
        "  AND u.attivo = 1 ORDER BY u.id", (aid,)))
    if not allievi:
        log("nessun allievo da popolare")
        return 1

    # Profili distribuiti in giro, non tutti uguali: il primo e' pronto,
    # l'ultimo si e' fermato, gli altri stanno in mezzo.
    nomi_profilo = list(PROFILI)
    assegnazioni = {a["id"]: nomi_profilo[i % len(nomi_profilo)] for i, a in enumerate(allievi)}

    if args.prova:
        log(f"autoscuola: {scuola['ragione_sociale']}")
        log(f"classi da creare: {', '.join(c[0] for c in CLASSI)}")
        for a in allievi:
            log(f"  {a['username']:<12} {a['nome']} {a['cognome']:<10} "
                f"patente {a['listato_target']:<4} profilo {assegnazioni[a['id']]}")
        log(f"attivita': ultimi {args.giorni} giorni. Nessuna scrittura eseguita (--prova).")
        return 0

    # ---------------------------------------------------------------- classi
    id_classe = {}
    for nome, descr, listato, colore in CLASSI:
        r = db.query_one("SELECT id FROM classi WHERE autoscuola_id = ? AND nome = ?", (aid, nome))
        if r:
            id_classe[nome] = r["id"]
        else:
            id_classe[nome] = db.execute(
                "INSERT INTO classi(autoscuola_id, nome, descrizione, listato_target, colore) "
                "VALUES(?,?,?,?,?)", (aid, nome, descr, listato, colore)).lastrowid
    log(f"classi pronte: {len(id_classe)}")

    # Gli allievi B si dividono fra serale e diurno a turno, cosi' nessuna
    # classe resta vuota; chi fa CQC va nella sua.
    turno = 0
    for a in allievi:
        if a["listato_target"] == "CQC":
            cid = id_classe["CQC sabato"]
        else:
            cid = id_classe["Serale B - ottobre" if turno % 2 == 0 else "Diurno B - ottobre"]
            turno += 1
        db.execute("UPDATE utenti SET classe_id = ? WHERE id = ?", (cid, a["id"]))
        a["classe_id"] = cid
    log("allievi assegnati alle classi")

    # ------------------------------------------------------------ videocorsi
    # Ogni lezione gia' caricata va data a qualcuno: dopo l'introduzione delle
    # classi, una lezione senza classe non la vede nessun allievo.
    n_video = 0
    for v in db.query(
            "SELECT v.id, l.codice AS listato FROM lezioni_video v "
            "JOIN corsi c ON c.id = v.corso_id JOIN listati l ON l.id = c.listato_id "
            "WHERE c.autoscuola_id = ?", (aid,)):
        destinatarie = ([id_classe["CQC sabato"]] if v["listato"] == "CQC"
                        else [id_classe["Serale B - ottobre"], id_classe["Diurno B - ottobre"]])
        for cid in destinatarie:
            db.execute("INSERT INTO video_classe(lezione_id, classe_id) VALUES(?,?) "
                       "ON CONFLICT DO NOTHING", (v["id"], cid))
        n_video += 1

    # Una classe senza nemmeno una lezione non si puo' mostrare: l'allievo apre
    # Videocorsi e trova il vuoto. Si aggiunge il minimo indispensabile dove
    # manca, piu' una diretta in programma per far vedere il countdown e
    # l'avviso agli allievi.
    domani = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=18, minute=30, second=0, microsecond=0)
    for nome, cid in id_classe.items():
        listato = next(c[2] for c in CLASSI if c[0] == nome)
        quante = db.query_one("SELECT COUNT(*) AS n FROM video_classe WHERE classe_id = ?",
                              (cid,))["n"]
        corso = db.query_one(
            "SELECT c.id FROM corsi c JOIN listati l ON l.id = c.listato_id "
            "WHERE c.autoscuola_id = ? AND l.codice = ? ORDER BY c.id LIMIT 1", (aid, listato))
        if not corso:
            lid = db.query_one("SELECT id FROM listati WHERE codice = ?", (listato,))["id"]
            corso = {"id": db.execute(
                "INSERT INTO corsi(autoscuola_id, listato_id, titolo, descrizione, pubblicato) "
                "VALUES(?,?,?,?,1)", (aid, lid, f"Teoria {listato} - Corso completo",
                                      f"Videolezioni del listato {listato}")).lastrowid}
        if not quante:
            vid = db.execute(
                "INSERT INTO lezioni_video(corso_id, titolo, descrizione, tipo, url,"
                " durata_sec, ordine, pubblicata) VALUES(?,?,?, 'registrata', ?,?,?,1)",
                (corso["id"], f"Introduzione al corso {listato}",
                 "Prima lezione registrata del corso.",
                 "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", 1200, 1)).lastrowid
            db.execute("INSERT INTO video_classe(lezione_id, classe_id) VALUES(?,?)", (vid, cid))
            n_video += 1
        vid = db.execute(
            "INSERT INTO lezioni_video(corso_id, titolo, descrizione, tipo, url, inizio_live,"
            " stato_live, ordine, pubblicata) VALUES(?,?,?, 'live', ?,?, 'programmata', 999, 1)",
            (corso["id"], f"Ripasso dal vivo - {nome}",
             "Domande e risposte prima dell'esame.",
             "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
             domani.strftime("%Y-%m-%dT%H:%M:%S"))).lastrowid
        db.execute("INSERT INTO video_classe(lezione_id, classe_id) VALUES(?,?)", (vid, cid))
        n_video += 1
    log(f"videolezioni e dirette assegnate alle classi: {n_video}")

    # ------------------------------------------------------------- attivita'
    oggi = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    totale_schede = totale_risposte = 0

    for a in allievi:
        prof = PROFILI[assegnazioni[a["id"]]]
        listato = db.query_one("SELECT id, codice, domande_esame, errori_max, minuti_esame "
                               "FROM listati WHERE codice = ?", (a["listato_target"],))
        if not listato:
            continue
        pool = [r["id"] for r in db.query(
            "SELECT d.id FROM domande d WHERE d.listato_id = ? AND d.attiva = 1 "
            "ORDER BY RANDOM() LIMIT 900", (listato["id"],))]
        if len(pool) < 40:
            log(f"  {a['username']}: catalogo {listato['codice']} troppo piccolo, salto")
            continue

        giorni = sorted(rnd.sample(range(prof["molla"], args.giorni), 
                                   min(prof["giorni_su_30"], args.giorni - prof["molla"])))
        for i, quanti_giorni_fa in enumerate(giorni):
            quando = oggi - timedelta(days=quanti_giorni_fa,
                                      hours=-rnd.randint(9, 21), minutes=-rnd.randint(0, 59))
            # Man mano che si avvicina all'esame migliora: chi si esercita
            # impara, e un grafico piatto non racconterebbe niente.
            progresso = (len(giorni) - i) / max(len(giorni), 1)
            bravura = min(0.97, prof["bravura"] + 0.12 * (1 - progresso))
            simulazione = (i % 3 == 2)
            tipo = "simulazione" if simulazione else "esercitazione"
            n_dom = listato["domande_esame"] if simulazione else rnd.choice([10, 15, 20])

            scelte = rnd.sample(pool, n_dom)
            risposte = []
            errori = 0
            for pos, did in enumerate(scelte):
                giusta = rnd.random() < bravura
                if not giusta:
                    errori += 1
                risposte.append((pos, did, giusta))

            durata = sum(rnd.randint(4, 18) for _ in scelte)
            esito = 1 if (simulazione and errori <= listato["errori_max"]) else (0 if simulazione else None)
            sid = db.execute(
                "INSERT INTO schede(utente_id, autoscuola_id, listato_id, tipo, parametri, stato,"
                " n_domande, n_errori, n_risposte, esito, durata_sec, limite_sec, iniziata_il,"
                " conclusa_il) VALUES(?,?,?,?,?, 'completata', ?,?,?,?,?,?,?,?)",
                (a["id"], aid, listato["id"], tipo, json.dumps({"demo": True}), n_dom, errori,
                 n_dom, esito, durata,
                 listato["minuti_esame"] * 60 if simulazione else None,
                 _iso(quando), _iso(quando + timedelta(seconds=durata)))).lastrowid

            for pos, did, giusta in risposte:
                d = db.query_one("SELECT capitolo_id, argomento_id, risposta FROM domande WHERE id = ?",
                                 (did,))
                data_risposta = d["risposta"] if giusta else (1 - d["risposta"])
                db.execute(
                    "INSERT INTO risposte(scheda_id, domanda_id, utente_id, capitolo_id,"
                    " argomento_id, posizione, risposta_data, corretta, tempo_ms, risposto_il) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, did, a["id"], d["capitolo_id"], d["argomento_id"], pos,
                     data_risposta, 1 if giusta else 0, rnd.randint(3000, 20000),
                     _iso(quando + timedelta(seconds=pos * 8))))
                totale_risposte += 1
            totale_schede += 1

        ultimo = oggi - timedelta(days=giorni[0] if giorni else 0)
        db.execute("UPDATE utenti SET ultimo_accesso = ? WHERE id = ?", (_iso(ultimo), a["id"]))
        log(f"  {a['username']:<12} {assegnazioni[a['id']]:<14} {len(giorni)} giornate di studio")

    log(f"generate {totale_schede} schede e {totale_risposte} risposte")
    log("ora lancia: python3 scripts/ricostruisci_aggregati.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
