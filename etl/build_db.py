"""
ETL - Costruzione del database applicativo.

Sorgenti:
  1. quizPatenteB2023.json  -> listato B (capitolo > argomento > domanda, con immagini)
  2. data/raw/*.json        -> listati estratti dai PDF ministeriali (parse_pdf.py)

Il caricamento e' idempotente: si puo' rieseguire senza duplicare nulla grazie
alle UNIQUE (listato, hash_testo) e agli INSERT ... ON CONFLICT DO UPDATE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(__import__("os").getenv("AC_DB", str(ROOT / "data" / "autoscuola.db")))
SCHEMA = ROOT / "backend" / "app" / "schema.sql"
MEDIA = Path(__import__("os").getenv("AC_MEDIA_BUILD", str(ROOT / "data" / "media")))

# Regole d'esame per listato (fonte: DM 40T/2011 e successive circolari MIT)
# Codice, nome, domande della scheda, minuti, errori ammessi.
# I valori seguono le regole d'esame ministeriali vigenti: CQC 70/90/7,
# CAP (KA/KB) 20/30/2 come da scheda del MIT, AM 30 domande in 25 minuti.
LISTATI = [
    ("B",      "Patente B",                          30, 20, 3),
    ("AM",     "Patentino AM (ciclomotori)",         30, 25, 3),
    ("SUP",    "Patenti superiori C-D-E",            40, 40, 4),
    ("CQC",    "Carta di Qualificazione del Conducente", 70, 90, 7),
    ("CAP",    "Certificato di Abilitazione Professionale", 20, 30, 2),
    ("REV_AB", "Revisione patente A/B",              30, 20, 3),
    ("REV_AM", "Revisione patentino AM",             30, 25, 3),
    ("REV_SUP","Revisione patenti superiori",        40, 40, 4),
    ("REV_CQC","Revisione CQC",                      70, 90, 7),
]

RUOLI = [
    ("allievo",    "Allievo dell'autoscuola", ["quiz:play", "stat:self", "video:watch", "ai:ask"]),
    ("istruttore", "Istruttore di teoria",    ["quiz:play", "stat:tenant", "video:manage"]),
    ("admin",      "Amministratore autoscuola", ["*:tenant"]),
    ("superadmin", "Gestore piattaforma",     ["*:*"]),
]


def slugify(s: str, maxlen: int = 80) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:maxlen] or "n-d"


def norm_testo(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def hash_testo(s: str) -> str:
    return hashlib.sha1(norm_testo(s).lower().encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------- #

class Loader:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self._cap: dict[tuple[int, str], int] = {}
        self._arg: dict[tuple[int, str], int] = {}
        self._img: dict[str, int] = {}

    # -- cataloghi -------------------------------------------------------- #
    def listato(self, codice: str) -> int:
        r = self.con.execute("SELECT id FROM listati WHERE codice=?", (codice,)).fetchone()
        return r[0]

    def capitolo(self, listato_id: int, titolo: str, ordine: int = 0) -> int:
        slug = slugify(titolo)
        key = (listato_id, slug)
        if key in self._cap:
            return self._cap[key]
        self.con.execute(
            "INSERT INTO capitoli(listato_id, slug, titolo, ordine) VALUES(?,?,?,?) "
            "ON CONFLICT(listato_id, slug) DO UPDATE SET titolo=excluded.titolo",
            (listato_id, slug, titolo, ordine))
        cid = self.con.execute(
            "SELECT id FROM capitoli WHERE listato_id=? AND slug=?", (listato_id, slug)).fetchone()[0]
        self._cap[key] = cid
        return cid

    def argomento(self, capitolo_id: int, titolo: str, ordine: int = 0,
                  slug: str | None = None) -> int:
        # Lo slug puo' essere imposto dal chiamante: per i quesiti dei PDF si
        # usa il codice ministeriale, cosi' due gruppi che iniziano con la
        # stessa frase restano due argomenti distinti invece di fondersi.
        slug = slug or slugify(titolo)
        key = (capitolo_id, slug)
        if key in self._arg:
            return self._arg[key]
        self.con.execute(
            "INSERT INTO argomenti(capitolo_id, slug, titolo, ordine) VALUES(?,?,?,?) "
            "ON CONFLICT(capitolo_id, slug) DO UPDATE SET titolo=excluded.titolo",
            (capitolo_id, slug, titolo, ordine))
        aid = self.con.execute(
            "SELECT id FROM argomenti WHERE capitolo_id=? AND slug=?", (capitolo_id, slug)).fetchone()[0]
        self._arg[key] = aid
        return aid

    def immagine(self, percorso: str | None) -> int | None:
        if not percorso:
            return None
        if percorso in self._img:
            return self._img[percorso]
        self.con.execute(
            "INSERT INTO immagini(percorso, hash) VALUES(?,?) ON CONFLICT(percorso) DO NOTHING",
            (percorso, Path(percorso).stem))
        iid = self.con.execute("SELECT id FROM immagini WHERE percorso=?", (percorso,)).fetchone()[0]
        self._img[percorso] = iid
        return iid

    def quesito(self, listato_id, capitolo_id, argomento_id, codice_min, tronco, img_id) -> int | None:
        if not codice_min:
            return None
        self.con.execute(
            "INSERT INTO quesiti(listato_id, capitolo_id, argomento_id, codice_min, tronco, immagine_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(listato_id, codice_min) DO UPDATE SET tronco=excluded.tronco",
            (listato_id, capitolo_id, argomento_id, codice_min, tronco, img_id))
        return self.con.execute(
            "SELECT id FROM quesiti WHERE listato_id=? AND codice_min=?",
            (listato_id, codice_min)).fetchone()[0]

    def domanda(self, listato_id, quesito_id, capitolo_id, argomento_id, img_id,
                codice_min, testo, risposta, chiave_dedup: str | None = None) -> None:
        """chiave_dedup: cosa rende due righe "la stessa domanda".

        Di norma e' il testo: la stessa affermazione ripetuta in due punti del
        listato e' la stessa domanda, e va tenuta una volta sola. Non vale
        pero' dove il testo e' solo un'alternativa di risposta e la domanda sta
        nel tronco: li' "DEBBONO ESSERE RICOSTRUITI" compare sotto quesiti
        diversi, e deduplicarlo sul solo testo cancella l'alternativa dal
        secondo quesito - a volte proprio quella esatta, lasciando un quesito
        senza risposta giusta. In quel caso la chiave include il quesito.
        """
        testo = norm_testo(testo)
        self.con.execute(
            "INSERT INTO domande(listato_id, quesito_id, capitolo_id, argomento_id, immagine_id,"
            " codice_min, testo, risposta, hash_testo) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(listato_id, hash_testo) DO NOTHING",
            (listato_id, quesito_id, capitolo_id, argomento_id, img_id,
             codice_min, testo, 1 if risposta else 0, hash_testo(chiave_dedup or testo)))


# --------------------------------------------------------------------------- #

def import_json_b(loader: Loader, path: Path, img_src: Path) -> int:
    """Listato B: {capitolo: {argomento: [{img, q, a}]}}"""
    data = json.loads(path.read_text(encoding="utf-8"))
    lid = loader.listato("B")
    dest = MEDIA / "b"
    dest.mkdir(parents=True, exist_ok=True)
    if img_src.exists():
        for f in img_src.iterdir():
            if f.is_file() and not (dest / f.name).exists():
                shutil.copy2(f, dest / f.name)

    n = 0
    for ic, (cap_slug, argomenti) in enumerate(data.items()):
        cid = loader.capitolo(lid, cap_slug.replace("-", " ").capitalize(), ic)
        # conserva lo slug originale del dataset (piu' leggibile nelle URL)
        loader.con.execute("UPDATE capitoli SET slug=? WHERE id=?", (cap_slug, cid))
        for ia, (arg_slug, domande) in enumerate(argomenti.items()):
            aid = loader.argomento(cid, arg_slug.replace("-", " ").capitalize(), ia)
            loader.con.execute("UPDATE argomenti SET slug=? WHERE id=?", (arg_slug, aid))
            for d in domande:
                img = d.get("img")
                img_id = loader.immagine("b/" + Path(img).name) if img else None
                loader.domanda(lid, None, cid, aid, img_id, None, d["q"], d["a"])
                n += 1
    return n


# I 16 raggruppamenti della CQC nel file hanno solo il numero di tipologia:
# dopo il prefisso resta "CQC - DL 30 LUGLIO 2021" per tutti. Questi nomi sono
# ricavati dal contenuto reale di ciascun gruppo. Copia identica in
# scripts/ricataloga_catalogo.py, che rimette a posto i database gia' esistenti.
TIPOLOGIE_CQC = {
    "01": "Motore, coppia e trasmissione",
    "02": "Sistemi elettronici di sicurezza",
    "03": "Uso del cambio e guida economica",
    "04": "Frenata, aderenza e ingombri del veicolo",
    "05": "Sicurezza sul lavoro e psicologia del traffico",
    "06": "Tempi di guida e di riposo",
    "07": "Prevenzione della criminalita' e del traffico di clandestini",
    "08": "Immagine dell'azienda e qualita' del servizio",
    "09": "Ergonomia, postura e salute del conducente",
    "10": "Alimentazione, alcol e condizioni psicofisiche",
    "11": "Merci, imballaggi e unita' di carico",
    "12": "Disciplina dell'autotrasporto di merci",
    "13": "Mercato dei trasporti, ADR e sostenibilita'",
    "14": "Guida sicura e comportamento del conducente",
    "15": "Forze, energia e dinamica del veicolo",
    "16": "Trasporto di persone e servizio alla clientela",
}
TIPOLOGIE = {"CQC": TIPOLOGIE_CQC, "REV_CQC": TIPOLOGIE_CQC}
PREFISSO_QUESITO = re.compile(r"^(\d+)##(\w+)\s*")


# Listati in cui il titolo del quesito non e' un argomento ma la DOMANDA vera e
# propria, e le righe sotto sono le sue alternative di risposta ("GLI PNEUMATICI
# CON LESIONI SUI FIANCHI: / si devono sostituire / debbono essere ricostruiti").
# Per questi il titolo va salvato come tronco - senza, l'allievo si vede
# chiedere "DEBBONO ESSERE RICOSTRUITI: vero o falso?" senza sapere cosa - e la
# deduplica delle righe deve tenere conto del quesito di appartenenza.
LISTATI_A_TRONCO = {"CAP"}


def etichetta_argomento(q: dict) -> str:
    """I quesiti dei PDF non hanno un titolo proprio: si usa la loro prima
    affermazione, accorciata. Senza questo l'argomento resterebbe uno solo per
    capitolo e le statistiche per argomento non direbbero nulla."""
    testo = re.sub(r"\s+", " ", (q["domande"][0]["testo"] if q.get("domande") else "")).strip()
    if len(testo) <= 70:
        return testo or f"Quesito {q.get('codice', '?')}"
    return testo[:70].rsplit(" ", 1)[0] + "..."


def import_pdf_json(loader: Loader, path: Path, codice_listato: str) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    lid = loader.listato(codice_listato)

    # Il prefisso "NN##tipologiaNN" si toglie solo se cio' che resta distingue
    # davvero i capitoli. Per la CQC no: 17 titoli grezzi diventano 2, e le
    # 5.416 domande finirebbero tutte in un capitolo unico, rendendo inutile la
    # ripetizione per capitolo degli errori. In quel caso comanda il numero.
    grezzi = {q["titolo"] for q in data["quesiti"]}
    puliti = {PREFISSO_QUESITO.sub("", t).strip() for t in grezzi}
    usa_numero = len(puliti) * 2 <= len(grezzi)
    nomi = TIPOLOGIE.get(codice_listato, {})

    n = 0
    for iq, q in enumerate(data["quesiti"]):
        m = PREFISSO_QUESITO.match(q["titolo"])
        if usa_numero and m:
            titolo = nomi.get(m.group(1)) or f"Tipologia {int(m.group(1))}"
            ordine_cap = int(m.group(1))
        else:
            titolo = PREFISSO_QUESITO.sub("", q["titolo"]).strip() or "Non classificato"
            ordine_cap = iq
        cid = loader.capitolo(lid, titolo, ordine_cap)
        aid = loader.argomento(cid, etichetta_argomento(q), iq, slug=f"q-{q['codice']}")
        img = next((d["immagine"] for d in q["domande"] if d.get("immagine")), None)
        img_id = loader.immagine("pdf/" + img) if img else None
        a_tronco = codice_listato in LISTATI_A_TRONCO
        tronco = norm_testo(q["titolo"]) if a_tronco else None
        qid = loader.quesito(lid, cid, aid, q["codice"], tronco, img_id)
        for d in q["domande"]:
            di = loader.immagine("pdf/" + d["immagine"]) if d.get("immagine") else img_id
            chiave = f"{q['codice']}|{norm_testo(d['testo'])}" if a_tronco else None
            loader.domanda(lid, qid, cid, aid, di, d.get("codice"), d["testo"],
                           d["corretta"], chiave)
            n += 1
    return n


# --------------------------------------------------------------------------- #

def seed_base(con: sqlite3.Connection) -> None:
    for cod, desc, perms in RUOLI:
        con.execute("INSERT INTO ruoli(codice, descrizione, permessi) VALUES(?,?,?) "
                    "ON CONFLICT(codice) DO NOTHING", (cod, desc, json.dumps(perms)))
    for cod, nome, nd, mm, err in LISTATI:
        con.execute(
            "INSERT INTO listati(codice, nome, domande_esame, minuti_esame, errori_max) "
            "VALUES(?,?,?,?,?) ON CONFLICT(codice) DO UPDATE SET nome=excluded.nome",
            (cod, nome, nd, mm, err))


def seed_demo(con: sqlite3.Connection) -> None:
    """Tenant dimostrativo con attivita' realistica: serve per validare la
    dashboard admin e gli algoritmi analitici senza attendere utenti reali."""
    import sys
    sys.path.insert(0, str(ROOT / "backend"))
    from app.security import hash_password

    con.execute("INSERT INTO autoscuole(ragione_sociale, slug, citta, piano) "
                "VALUES('Autoscuola Demo srl','demo','Milano','pro') "
                "ON CONFLICT(slug) DO NOTHING")
    tid = con.execute("SELECT id FROM autoscuole WHERE slug='demo'").fetchone()[0]
    ruoli = {r[0]: r[1] for r in con.execute("SELECT codice, id FROM ruoli")}

    utenti = [("admin@demo.it", "Giulia", "Ferrari", "admin", "B"),
              ("marco@demo.it", "Marco", "Rossi", "allievo", "B"),
              ("sara@demo.it", "Sara", "Bianchi", "allievo", "B"),
              ("luca@demo.it", "Luca", "Verdi", "allievo", "B"),
              ("anna@demo.it", "Anna", "Conti", "allievo", "AM"),
              ("paolo@demo.it", "Paolo", "Greco", "allievo", "CQC")]
    for email, nome, cog, ruolo, listato in utenti:
        con.execute(
            "INSERT INTO utenti(autoscuola_id, ruolo_id, email, password_hash, nome, cognome,"
            " listato_target, data_esame) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(autoscuola_id, email) DO NOTHING",
            (tid, ruoli[ruolo], email, hash_password("demo1234"), nome, cog, listato,
             (datetime.now(timezone.utc) + timedelta(days=random.randint(10, 60))).strftime("%Y-%m-%d")))

    # Corso video dimostrativo
    lid_b = con.execute("SELECT id FROM listati WHERE codice='B'").fetchone()[0]
    con.execute("INSERT INTO corsi(autoscuola_id, listato_id, titolo, descrizione, pubblicato) "
                "VALUES(?,?,?,?,1)", (tid, lid_b, "Teoria Patente B - Corso completo",
                                      "Videolezioni sui 25 capitoli del listato ministeriale"))
    corso_id = con.execute("SELECT id FROM corsi ORDER BY id DESC LIMIT 1").fetchone()[0]
    caps = con.execute("SELECT id, titolo FROM capitoli WHERE listato_id=? ORDER BY ordine LIMIT 8",
                       (lid_b,)).fetchall()
    for i, (cid, tit) in enumerate(caps):
        con.execute(
            "INSERT INTO lezioni_video(corso_id, capitolo_id, titolo, descrizione, tipo,"
            " url, durata_sec, ordine) VALUES(?,?,?,?,'registrata',?,?,?)",
            (corso_id, cid, f"Lezione {i+1} - {tit}",
             f"Spiegazione teorica del capitolo: {tit}",
             "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", 900 + i * 120, i))
    con.execute(
        "INSERT INTO lezioni_video(corso_id, titolo, descrizione, tipo, url, inizio_live,"
        " stato_live, ordine) VALUES(?,?,?,'live',?,?, 'programmata', 99)",
        (corso_id, "Live: ripasso pre-esame", "Sessione dal vivo di domande e risposte",
         "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
         (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-b", type=Path, help="quizPatenteB2023.json")
    ap.add_argument("--img-b", type=Path, help="cartella img_sign")
    ap.add_argument("--raw-dir", type=Path, default=ROOT / "data" / "raw")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if args.reset and DB_PATH.exists():
        DB_PATH.unlink()

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    seed_base(con)
    con.commit()

    loader = Loader(con)
    tot = 0
    if args.json_b and args.json_b.exists():
        n = import_json_b(loader, args.json_b, args.img_b or Path("."))
        print(f"  B       importate {n:6d} domande")
        tot += n
        con.commit()

    mappa = {"am.json": "AM", "sup.json": "SUP", "cqc.json": "CQC", "cap.json": "CAP",
             "rev_ab.json": "REV_AB", "rev_am.json": "REV_AM",
             "rev_sup.json": "REV_SUP", "rev_cqc.json": "REV_CQC"}
    for fname, cod in mappa.items():
        p = args.raw_dir / fname
        if p.exists():
            n = import_pdf_json(loader, p, cod)
            print(f"  {cod:7s} importate {n:6d} domande")
            tot += n
            con.commit()

    if args.demo:
        seed_demo(con)
        con.commit()

    con.execute("ANALYZE")
    con.commit()
    reali = con.execute("SELECT COUNT(*) FROM domande").fetchone()[0]
    print(f"\nTotale righe processate: {tot} | domande univoche in DB: {reali}")
    for cod, n in con.execute(
            "SELECT l.codice, COUNT(d.id) FROM listati l LEFT JOIN domande d ON d.listato_id=l.id "
            "GROUP BY l.codice ORDER BY 2 DESC"):
        print(f"   {cod:8s} {n:6d}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
