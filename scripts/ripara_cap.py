"""Rimette in sesto il listato CAP: la domanda torna leggibile, le alternative
cancellate tornano al loro posto.

DUE GUASTI, UNA SOLA ORIGINE
    Nel CAP il quesito ministeriale e' fatto di una domanda ("GLI PNEUMATICI
    CON LESIONI SUI FIANCHI CHE INTERESSANO LE TELE:") e di tre alternative
    ("si devono sostituire", "debbono essere ricostruiti", ...). L'importazione
    salvava le alternative come domande e buttava via la domanda vera:

    1. `quesiti.tronco` restava NULL, e l'app mostra "tronco + testo". Risultato:
       all'allievo compariva "DEBBONO ESSERE RICOSTRUITI - vero o falso?", senza
       sapere di cosa si parlasse. Tutte e 1.389 le righe CAP, non alcune.

    2. La deduplica sul solo testo cancellava le alternative ripetute sotto
       quesiti diversi: 1.508 alternative nel PDF, 1.389 nel database, 119
       sparite. Quando a sparire era quella esatta, il quesito restava senza
       risposta giusta - i "98 quesiti ambigui" gia' annotati sono questo.

    La correzione a monte e' in etl/build_db.py (tronco salvato, deduplica per
    quesito). Questo script porta la stessa correzione nei database gia' in uso,
    reimportando il CAP da data/raw/cap.json.

COSA TOCCA
    Solo il catalogo CAP. Se qualche allievo ha gia' risposto a domande CAP, la
    reimportazione non parte (cancellerebbe quelle risposte) e lo script si
    limita a rimettere i tronchi, che e' il guasto piu' grave dei due.

    python3 scripts/ripara_cap.py --prova
    python3 scripts/ripara_cap.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE))

from etl.build_db import (Loader, LISTATI_A_TRONCO, import_pdf_json,  # noqa: E402
                          norm_testo)

CODICE = "CAP"
SORGENTE = RADICE / "data" / "raw" / "cap.json"


def stato(con: sqlite3.Connection, lid: int) -> dict:
    q = lambda s: con.execute(s, (lid,)).fetchone()[0]
    return {
        "domande": q("SELECT COUNT(*) FROM domande WHERE listato_id = ?"),
        "senza_tronco": q("SELECT COUNT(*) FROM domande d LEFT JOIN quesiti q ON q.id = d.quesito_id "
                          "WHERE d.listato_id = ? AND (q.tronco IS NULL OR trim(q.tronco) = '')"),
        "capitoli": q("SELECT COUNT(*) FROM capitoli WHERE listato_id = ?"),
    }


def quesiti_senza_risposta_giusta(con: sqlite3.Connection, lid: int) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM (SELECT d.quesito_id FROM domande d WHERE d.listato_id = ? "
        "AND d.quesito_id IS NOT NULL GROUP BY d.quesito_id HAVING SUM(d.risposta) = 0)",
        (lid,)).fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Ripara il catalogo CAP.")
    ap.add_argument("--prova", action="store_true", help="mostra cosa farebbe, senza scrivere")
    ap.add_argument("--db", default=str(RADICE / "data" / "autoscuola.db"))
    args = ap.parse_args()

    percorso = Path(args.db)
    if not percorso.exists():
        print(f"! non trovo {percorso}")
        return 1
    if CODICE not in LISTATI_A_TRONCO:
        print("! etl/build_db.py non ha la correzione del tronco: aggiornare prima il codice")
        return 1

    con = sqlite3.connect(percorso)
    con.execute("PRAGMA foreign_keys = ON")
    riga = con.execute("SELECT id FROM listati WHERE codice = ?", (CODICE,)).fetchone()
    if not riga:
        print(f"! nessun listato {CODICE} in questo database")
        return 0
    lid = riga[0]

    prima = stato(con, lid)
    print(f"Listato {CODICE}: {prima['domande']} righe, {prima['capitoli']} capitoli, "
          f"{prima['senza_tronco']} righe senza la domanda che le regge")
    print(f"Quesiti rimasti senza nessuna risposta esatta: {quesiti_senza_risposta_giusta(con, lid)}")

    # Chi tocca queste domande? Se c'e' lavoro degli allievi, non si cancella nulla.
    legami = 0
    for tab, col in (("risposte", "domanda_id"), ("srs_stato", "domanda_id"),
                     ("ai_spiegazioni", "domanda_id"), ("ai_conversazioni", "domanda_id")):
        try:
            legami += con.execute(
                f"SELECT COUNT(*) FROM {tab} t JOIN domande d ON d.id = t.{col} "
                f"WHERE d.listato_id = ?", (lid,)).fetchone()[0]
        except sqlite3.Error:
            pass

    if not SORGENTE.exists():
        print(f"! manca {SORGENTE.relative_to(RADICE)}: posso solo rimettere i tronchi")
        legami = 1        # forza la strada non distruttiva

    if args.prova:
        if legami:
            print(f"\n--prova: ci sono {legami} legami di allievi (o manca il sorgente): "
                  f"rimetterei solo i tronchi, senza cancellare nulla.")
        else:
            attesi = json.loads(SORGENTE.read_text(encoding="utf-8"))["quesiti"]
            righe = sum(len(q["domande"]) for q in attesi)
            print(f"\n--prova: reimporterei {len(attesi)} quesiti / {righe} alternative "
                  f"da {SORGENTE.name} (oggi ce ne sono {prima['domande']}).")
        return 0

    rotti_prima = set(map(tuple, con.execute("PRAGMA foreign_key_check").fetchall()))
    if rotti_prima:
        print(f"\nNota: il database ha gia' {len(rotti_prima)} riferimenti rotti "
              f"({', '.join(sorted({r[0] for r in rotti_prima}))}), precedenti a questa riparazione. Non li tocco.")

    copia = (RADICE / "data" / "backup" /
             f"{percorso.stem}.prima-di-CAP.{datetime.now():%Y%m%d-%H%M%S}.db")
    copia.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(percorso, copia)
    print(f"\nCopia di sicurezza: {copia.relative_to(RADICE)}")

    if legami:
        # Strada prudente: si aggiunge la domanda mancante, non si cancella niente.
        # Il tronco si prende dal titolo del capitolo, che nel CAP e' la domanda.
        n = con.execute(
            "UPDATE quesiti SET tronco = (SELECT c.titolo FROM capitoli c WHERE c.id = quesiti.capitolo_id) "
            "WHERE listato_id = ? AND (tronco IS NULL OR trim(tronco) = '') "
            "AND capitolo_id IS NOT NULL", (lid,)).rowcount
        con.commit()
        print(f"Rimessa la domanda su {n} quesiti (nessuna riga cancellata: "
              f"ci sono risposte di allievi da conservare).")
        print("Restano le alternative perse in importazione: per recuperarle serve una "
              "reimportazione, da fare quando non ci sono risposte CAP in gioco.")
    else:
        # Strada completa: si rifa' il catalogo CAP dal PDF ministeriale.
        con.execute("DELETE FROM domande WHERE listato_id = ?", (lid,))
        con.execute("DELETE FROM quesiti WHERE listato_id = ?", (lid,))
        con.execute("DELETE FROM argomenti WHERE capitolo_id IN "
                    "(SELECT id FROM capitoli WHERE listato_id = ?)", (lid,))
        con.execute("DELETE FROM capitoli WHERE listato_id = ?", (lid,))
        con.commit()
        n = import_pdf_json(Loader(con), SORGENTE, CODICE)
        con.commit()
        print(f"Catalogo CAP reimportato: {n} righe.")

        # Il PDF ministeriale da cui nasce cap.json non e' venuto su intero: per
        # una manciata di quesiti l'estrazione ha perso delle alternative, e ne
        # resta una sola o nessuna segnata esatta. Non c'e' modo di indovinare
        # quelle mancanti, e somministrare un quesito monco e' peggio che non
        # somministrarlo: si spengono. Restano nel database, pronti a tornare il
        # giorno in cui si riparte dal PDF.
        spenti = con.execute(
            "UPDATE domande SET attiva = 0 WHERE listato_id = ? AND quesito_id IN ("
            "  SELECT quesito_id FROM domande WHERE listato_id = ? AND quesito_id IS NOT NULL"
            "  GROUP BY quesito_id HAVING COUNT(*) < 2 OR SUM(risposta) = 0)",
            (lid, lid)).rowcount
        con.commit()
        n_quesiti = con.execute(
            "SELECT COUNT(DISTINCT quesito_id) FROM domande WHERE listato_id = ? AND attiva = 0",
            (lid,)).fetchone()[0]
        print(f"Spenti {n_quesiti} quesiti incompleti nel PDF di partenza ({spenti} righe): "
              f"restano nel database ma non escono piu' nelle schede.")

    dopo = stato(con, lid)
    orfani = quesiti_senza_risposta_giusta(con, lid)
    print(f"\nOra: {dopo['domande']} righe, {dopo['capitoli']} capitoli, "
          f"{dopo['senza_tronco']} righe senza domanda, {orfani} quesiti senza risposta esatta")

    problemi = []
    if dopo["senza_tronco"]:
        problemi.append(f"{dopo['senza_tronco']} righe ancora senza la domanda che le regge")
    if not legami:
        if dopo["domande"] <= prima["domande"]:
            problemi.append(f"nessuna riga recuperata ({prima['domande']} -> {dopo['domande']})")
        attivi_orfani = con.execute(
            "SELECT COUNT(*) FROM (SELECT quesito_id FROM domande WHERE listato_id = ? "
            "AND attiva = 1 AND quesito_id IS NOT NULL GROUP BY quesito_id "
            "HAVING SUM(risposta) = 0 OR COUNT(*) < 2)", (lid,)).fetchone()[0]
        if attivi_orfani:
            problemi.append(f"{attivi_orfani} quesiti monchi ancora attivi")
    vuoto = con.execute(
        "SELECT COUNT(*) FROM domande d JOIN quesiti q ON q.id = d.quesito_id "
        "WHERE d.listato_id = ? AND trim(COALESCE(q.tronco,'')) = trim(d.testo)", (lid,)).fetchone()[0]
    if vuoto:
        problemi.append(f"{vuoto} righe in cui domanda e risposta sono lo stesso testo")
    nuovi_rotti = set(map(tuple, con.execute("PRAGMA foreign_key_check").fetchall())) - rotti_prima
    if nuovi_rotti:
        problemi.append(f"{len(nuovi_rotti)} vincoli rotti da questa riparazione "
                        f"(prima tabella: {sorted(nuovi_rotti)[0][0]})")

    if problemi:
        print("\n! VERIFICHE FALLITE:")
        for x in problemi:
            print("   -", x)
        print(f"  Il database precedente e' in {copia.relative_to(RADICE)}")
        return 1

    esempio = con.execute(
        "SELECT q.tronco, d.testo, d.risposta FROM domande d JOIN quesiti q ON q.id = d.quesito_id "
        "WHERE d.listato_id = ? AND q.tronco IS NOT NULL AND d.attiva = 1 LIMIT 1", (lid,)).fetchone()
    if esempio:
        print(f"\nCome la vede ora l'allievo:\n   «{esempio[0]} {esempio[1]}»  -> "
              f"{'VERO' if esempio[2] else 'FALSO'}")
    print("\nVerifiche superate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
