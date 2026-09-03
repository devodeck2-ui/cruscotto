"""Rimette il listato B in un database che l'ha perso.

Sintomo che risolve: l'allievo con patente B apre una scheda e legge
"Nessuna domanda disponibile per i filtri selezionati". La riga del listato B
c'e' in `listati`, ma non ha nemmeno un capitolo: il generatore cerca fra zero
domande e si ferma. Gli altri listati funzionano, quindi il guasto passa
inosservato finche' non si iscrive il primo allievo di patente B - cioe' quasi
subito.

Da dove si ripescano le domande: da `data/autoscuola.demo.db`, la copia
versionata del catalogo (25 capitoli, 656 argomenti, 6.849 domande). Non da
`data/raw/`, dove il listato B non c'e' proprio: e' l'unico che manca fra i
nove, ed e' il motivo per cui una ricostruzione con l'ETL non lo riporterebbe.

Cosa tocca e cosa no. Aggiunge SOLO righe di catalogo per il listato B -
capitoli, argomenti, domande e le immagini che servono loro. Non sfiora
utenti, password, schede svolte, risposte, statistiche, presenze: il database
di lavoro resta quello di prima con in piu' il catalogo mancante. Gli id
vengono riassegnati sopra il massimo gia' presente, cosi' nulla di esistente
viene sovrascritto.

    python3 scripts/ripristina_listato_b.py --prova    # dice cosa farebbe
    python3 scripts/ripristina_listato_b.py            # lo fa

Il server va fermato prima: SQLite regge piu' lettori, ma qui si scrive.
Una copia di sicurezza finisce in data/backup/ prima di ogni modifica.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
LAVORO = RADICE / "data" / "autoscuola.db"
SORGENTE = RADICE / "data" / "autoscuola.demo.db"
CODICE = "B"


def conta(con: sqlite3.Connection, listato_id: int) -> tuple[int, int, int]:
    c = con.execute("SELECT COUNT(*) FROM capitoli WHERE listato_id = ?", (listato_id,)).fetchone()[0]
    a = con.execute("SELECT COUNT(*) FROM argomenti a JOIN capitoli c ON c.id = a.capitolo_id "
                    "WHERE c.listato_id = ?", (listato_id,)).fetchone()[0]
    d = con.execute("SELECT COUNT(*) FROM domande WHERE listato_id = ?", (listato_id,)).fetchone()[0]
    return c, a, d


def main() -> int:
    ap = argparse.ArgumentParser(description="Ripristina il listato B dal catalogo demo.")
    ap.add_argument("--prova", action="store_true", help="mostra cosa farebbe, senza scrivere")
    ap.add_argument("--db", default=str(LAVORO), help="database da riparare")
    ap.add_argument("--sorgente", default=str(SORGENTE), help="database da cui prendere il catalogo")
    args = ap.parse_args()

    lavoro, sorgente = Path(args.db), Path(args.sorgente)
    for p in (lavoro, sorgente):
        if not p.exists():
            print(f"! non trovo {p}")
            return 1

    con = sqlite3.connect(lavoro)
    con.execute("PRAGMA foreign_keys = ON")
    src = sqlite3.connect(f"file:{sorgente}?mode=ro", uri=True)

    riga = con.execute("SELECT id FROM listati WHERE codice = ?", (CODICE,)).fetchone()
    if not riga:
        print(f"! il database non ha nemmeno la riga del listato {CODICE}: qui serve un ripristino completo")
        return 1
    id_lavoro = riga[0]
    id_src = src.execute("SELECT id FROM listati WHERE codice = ?", (CODICE,)).fetchone()[0]

    prima = conta(con, id_lavoro)
    print(f"Listato {CODICE} nel database da riparare: "
          f"{prima[0]} capitoli, {prima[1]} argomenti, {prima[2]} domande")
    attesi = conta(src, id_src)
    print(f"Listato {CODICE} nel catalogo demo:        "
          f"{attesi[0]} capitoli, {attesi[1]} argomenti, {attesi[2]} domande")

    if prima != (0, 0, 0):
        print("\nIl listato B non e' vuoto: non tocco nulla, per non creare doppioni.")
        print("Se lo si vuole rifare da zero, va prima svuotato a mano.")
        return 1
    if args.prova:
        print("\n--prova: nessuna scrittura eseguita.")
        return 0

    # Fotografia dei vincoli gia' rotti PRIMA di toccare qualcosa: un database
    # che arriva da una cancellazione fatta a vincoli spenti puo' averne, e non
    # sarebbe onesto attribuirli a questo ripristino.
    rotti_prima = set(map(tuple, con.execute("PRAGMA foreign_key_check").fetchall()))
    if rotti_prima:
        tabelle = sorted({r[0] for r in rotti_prima})
        print(f"\nNota: il database ha gia' {len(rotti_prima)} riferimenti rotti "
              f"({', '.join(tabelle)}). Non li tocco: sono precedenti a questo ripristino.")

    RADICE.joinpath("data", "backup").mkdir(parents=True, exist_ok=True)
    copia = RADICE / "data" / "backup" / f"autoscuola.prima-di-B.{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(lavoro, copia)
    print(f"\nCopia di sicurezza: {copia.relative_to(RADICE)}")

    # Gli id nuovi partono sopra il massimo esistente: nessuna riga di lavoro
    # rischia di essere sovrascritta.
    def prossimo(tabella: str) -> int:
        return con.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {tabella}").fetchone()[0]

    # --- immagini: si riusano quelle gia' presenti (percorso e' UNIQUE) ---
    mappa_img: dict[int, int] = {}
    esistenti = {r[1]: r[0] for r in con.execute("SELECT id, percorso FROM immagini")}
    id_img = prossimo("immagini")
    nuove_img = 0
    for iid, percorso, h, larg, alt, testo_alt in src.execute(
            "SELECT DISTINCT i.id, i.percorso, i.hash, i.larghezza, i.altezza, i.alt_text "
            "FROM immagini i JOIN domande d ON d.immagine_id = i.id WHERE d.listato_id = ?", (id_src,)):
        if percorso in esistenti:
            mappa_img[iid] = esistenti[percorso]
            continue
        con.execute("INSERT INTO immagini(id, percorso, hash, larghezza, altezza, alt_text) "
                    "VALUES(?,?,?,?,?,?)", (id_img, percorso, h, larg, alt, testo_alt))
        mappa_img[iid] = id_img
        id_img += 1
        nuove_img += 1

    # --- capitoli ---
    mappa_cap: dict[int, int] = {}
    id_cap = prossimo("capitoli")
    for cid, slug, titolo, ordine in src.execute(
            "SELECT id, slug, titolo, ordine FROM capitoli WHERE listato_id = ? ORDER BY ordine, id", (id_src,)):
        con.execute("INSERT INTO capitoli(id, listato_id, slug, titolo, ordine) VALUES(?,?,?,?,?)",
                    (id_cap, id_lavoro, slug, titolo, ordine))
        mappa_cap[cid] = id_cap
        id_cap += 1

    # --- argomenti ---
    mappa_arg: dict[int, int] = {}
    id_arg = prossimo("argomenti")
    for aid, cap_id, slug, titolo, ordine in src.execute(
            "SELECT a.id, a.capitolo_id, a.slug, a.titolo, a.ordine FROM argomenti a "
            "JOIN capitoli c ON c.id = a.capitolo_id WHERE c.listato_id = ? ORDER BY a.ordine, a.id", (id_src,)):
        con.execute("INSERT INTO argomenti(id, capitolo_id, slug, titolo, ordine) VALUES(?,?,?,?,?)",
                    (id_arg, mappa_cap[cap_id], slug, titolo, ordine))
        mappa_arg[aid] = id_arg
        id_arg += 1

    # --- domande (i trigger riempiono da soli l'indice full-text) ---
    id_dom = prossimo("domande")
    n_dom = 0
    for (cap_id, arg_id, img_id, codice_min, testo, risposta, hash_testo,
         difficolta, attiva) in src.execute(
            "SELECT capitolo_id, argomento_id, immagine_id, codice_min, testo, risposta, "
            "       hash_testo, difficolta, attiva FROM domande WHERE listato_id = ? ORDER BY id", (id_src,)):
        con.execute(
            "INSERT INTO domande(id, listato_id, quesito_id, capitolo_id, argomento_id, immagine_id,"
            " codice_min, testo, risposta, hash_testo, difficolta, n_somministr, attiva) "
            "VALUES(?,?,NULL,?,?,?,?,?,?,?,?,0,?)",
            (id_dom, id_lavoro, mappa_cap.get(cap_id), mappa_arg.get(arg_id),
             mappa_img.get(img_id), codice_min, testo, risposta, hash_testo, difficolta, attiva))
        id_dom += 1
        n_dom += 1

    con.commit()

    dopo = conta(con, id_lavoro)
    print(f"\nInserite: {dopo[0]} capitoli, {dopo[1]} argomenti, {dopo[2]} domande, "
          f"{nuove_img} immagini nuove ({len(mappa_img) - nuove_img} gia' presenti)")

    # --- verifiche, prima di dichiarare fatto ---
    problemi = []
    if dopo != attesi:
        problemi.append(f"conteggi diversi dal catalogo demo: {dopo} invece di {attesi}")
    orfane = con.execute(
        "SELECT COUNT(*) FROM domande d WHERE d.listato_id = ? AND (d.capitolo_id IS NULL "
        "OR NOT EXISTS(SELECT 1 FROM capitoli c WHERE c.id = d.capitolo_id))", (id_lavoro,)).fetchone()[0]
    if orfane:
        problemi.append(f"{orfane} domande senza capitolo")
    fuori = con.execute(
        "SELECT COUNT(*) FROM domande d JOIN argomenti a ON a.id = d.argomento_id "
        "WHERE d.listato_id = ? AND a.capitolo_id <> d.capitolo_id", (id_lavoro,)).fetchone()[0]
    if fuori:
        problemi.append(f"{fuori} domande in un capitolo diverso da quello del loro argomento")
    img_rotte = con.execute(
        "SELECT COUNT(*) FROM domande d WHERE d.listato_id = ? AND d.immagine_id IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM immagini i WHERE i.id = d.immagine_id)", (id_lavoro,)).fetchone()[0]
    if img_rotte:
        problemi.append(f"{img_rotte} domande con immagine inesistente")
    fts = con.execute("SELECT COUNT(*) FROM domande_fts f JOIN domande d ON d.id = f.rowid "
                      "WHERE d.listato_id = ?", (id_lavoro,)).fetchone()[0]
    if fts != dopo[2]:
        problemi.append(f"indice di ricerca incompleto: {fts} righe su {dopo[2]}")
    nuovi_rotti = set(map(tuple, con.execute("PRAGMA foreign_key_check").fetchall())) - rotti_prima
    if nuovi_rotti:
        problemi.append(f"{len(nuovi_rotti)} vincoli di integrita' rotti da questo ripristino "
                        f"(prima tabella: {sorted(nuovi_rotti)[0][0]})")

    if problemi:
        print("\n! VERIFICHE FALLITE:")
        for p in problemi:
            print("   -", p)
        print(f"  Il database precedente e' in {copia.relative_to(RADICE)}")
        return 1

    print("Verifiche superate: catalogo completo, nessuna domanda orfana, ricerca allineata.")
    print(f"\nFatto. Riavviare il server; l'allievo di patente {CODICE} ora puo' aprire le schede.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
