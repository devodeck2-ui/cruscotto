"""Test delle correzioni: ogni verifica qui sotto fallirebbe prima del fix.

Stesso impianto di test_e2e.py (TestClient in-process su una copia del
database), ma centrato sui difetti trovati durante la revisione del codice.

    python3 tests/test_correzioni.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp()) / "test.db"
shutil.copy2(ROOT / "data" / "autoscuola.db", TMP)
os.environ["AC_DB"] = str(TMP)
os.environ["AC_MEDIA"] = str(ROOT / "data" / "media")
os.environ["AC_FRONTEND"] = str(ROOT / "frontend")
sys.path.insert(0, str(ROOT / "backend"))

import sqlite3                                      # noqa: E402
from fastapi.testclient import TestClient           # noqa: E402
from app.main import app                            # noqa: E402

c = TestClient(app)
esiti: list[tuple[bool, str]] = []


def verifica(cond, descrizione, extra=""):
    esiti.append((bool(cond), descrizione))
    print(f"  {'OK  ' if cond else 'FALLITO'} {descrizione}" + (f"  [{extra}]" if extra and not cond else ""))


def login(email, pwd="demo1234"):
    r = c.post("/api/auth/login", json={"email": email, "password": pwd})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}, r.json()["utente"]


allievo, u_allievo = login("marco@demo.it")
admin, u_admin = login("admin@demo.it")


print("\n== Ricerca full-text con caratteri di sintassi FTS5 ==")
for termine in ["precedenza-", "prece)denza", "segnale OR", "sorpasso AND", 'virgo"lette']:
    r = c.get(f"/api/catalogo/cerca?q={termine}&listato=B", headers=allievo)
    verifica(r.status_code == 200, f"la ricerca «{termine}» non fa cadere l'endpoint",
             f"HTTP {r.status_code}")
r = c.get("/api/catalogo/cerca?q=precedenza&listato=B", headers=allievo).json()
verifica(len(r) > 0, f"la ricerca normale continua a trovare risultati ({len(r)})")


print("\n== Ricorrezione di una risposta gia' data ==")
cap = c.get("/api/catalogo/capitoli?listato=B", headers=allievo).json()
ex = c.post("/api/quiz/schede", headers=allievo,
            json={"tipo": "esercitazione", "listato": "B",
                  "capitoli": [cap[0]["id"]], "n_domande": 5}).json()
dati = c.get(f"/api/quiz/schede/{ex['scheda_id']}", headers=allievo).json()
prima = dati["domande"][0]

con = sqlite3.connect(TMP)
esatta = bool(con.execute("SELECT risposta FROM domande WHERE id=?",
                          (prima["domanda_id"],)).fetchone()[0])
con.close()


def errori_scheda():
    con = sqlite3.connect(TMP)
    n = con.execute("SELECT n_errori FROM schede WHERE id=?", (ex["scheda_id"],)).fetchone()[0]
    con.close()
    return n


# Prima sbaglia di proposito...
r1 = c.post(f"/api/quiz/schede/{ex['scheda_id']}/rispondi", headers=allievo,
            json={"posizione": prima["posizione"], "risposta": not esatta, "tempo_ms": 2000}).json()
verifica(r1["corretta"] is False, "risposta sbagliata registrata come tale")
verifica(errori_scheda() == 1, f"un errore contato ({errori_scheda()})")

# ...poi cambia idea e risponde giusto: l'errore deve sparire.
r2 = c.post(f"/api/quiz/schede/{ex['scheda_id']}/rispondi", headers=allievo,
            json={"posizione": prima["posizione"], "risposta": esatta, "tempo_ms": 2000}).json()
verifica(r2["corretta"] is True, "la correzione registra la nuova risposta")
verifica(errori_scheda() == 0, f"l'errore viene tolto dal conteggio ({errori_scheda()})")

con = sqlite3.connect(TMP)
reali = con.execute(
    "SELECT COUNT(*), SUM(CASE WHEN corretta=0 THEN 1 ELSE 0 END) FROM risposte "
    "WHERE utente_id=? AND corretta IS NOT NULL AND argomento_id IS NOT NULL",
    (u_allievo["id"],)).fetchone()
aggr = con.execute("SELECT SUM(n_risposte), SUM(n_errori) FROM stat_utente_argomento "
                   "WHERE utente_id=?", (u_allievo["id"],)).fetchone()
con.close()
verifica(reali == aggr, f"aggregati allineati anche dopo la ricorrezione ({aggr} = {reali})")


print("\n== Un allievo non genera schede di patenti non sue ==")
r = c.post("/api/quiz/schede", headers=allievo, json={"tipo": "esercitazione", "listato": "CQC"})
verifica(r.status_code == 403, f"scheda CQC negata a un allievo iscritto solo alla B (HTTP {r.status_code})")
r = c.post("/api/quiz/schede", headers=admin, json={"tipo": "esercitazione", "listato": "CQC"})
verifica(r.status_code == 200, "lo staff resta libero di aprire qualsiasi listato", r.text[:120])


print("\n== Anagrafica: svuotare un campo lo cancella davvero ==")
nuovo = c.post("/api/gestione/allievi", headers=admin,
               json={"nome": "Prova", "cognome": "Svuotamento", "telefono": "3331112223",
                     "patenti": ["B"]})
verifica(nuovo.status_code == 200, "allievo di prova creato", nuovo.text[:160])
uid = nuovo.json()["id"]
c.put(f"/api/gestione/allievi/{uid}", headers=admin, json={"telefono": None})
con = sqlite3.connect(TMP)
tel = con.execute("SELECT telefono FROM utenti WHERE id=?", (uid,)).fetchone()[0]
con.close()
verifica(tel is None, f"il telefono e' stato azzerato (valore attuale: {tel!r})")

c.put(f"/api/gestione/allievi/{uid}", headers=admin, json={"note": "solo le note"})
con = sqlite3.connect(TMP)
riga = con.execute("SELECT nome, cognome, note_admin FROM utenti WHERE id=?", (uid,)).fetchone()
con.close()
verifica(riga[0] == "Prova" and riga[1] == "Svuotamento",
         "i campi non inviati restano intatti")
verifica(riga[2] == "solo le note", "il campo inviato viene scritto")


print("\n== Fasce orarie: la modifica valida come la creazione ==")
s1 = c.post("/api/gestione/orario", headers=admin,
            json={"giorno": 1, "ora_inizio": "09:00", "ora_fine": "10:30",
                  "listato": "B", "aula": "Aula test"})
verifica(s1.status_code == 200, "fascia creata", s1.text[:160])
s2 = c.post("/api/gestione/orario", headers=admin,
            json={"giorno": 1, "ora_inizio": "11:00", "ora_fine": "12:00",
                  "listato": "B", "aula": "Aula test"})
verifica(s2.status_code == 200, "seconda fascia creata", s2.text[:160])

r = c.put(f"/api/gestione/orario/{s2.json()['id']}", headers=admin,
          json={"giorno": 1, "ora_inizio": "12:00", "ora_fine": "11:00",
                "listato": "B", "aula": "Aula test"})
verifica(r.status_code == 422, f"orario invertito respinto anche in modifica (HTTP {r.status_code})")

r = c.put(f"/api/gestione/orario/{s2.json()['id']}", headers=admin,
          json={"giorno": 1, "ora_inizio": "09:30", "ora_fine": "10:00",
                "listato": "B", "aula": "Aula test"})
verifica(r.status_code == 409, f"sovrapposizione respinta anche in modifica (HTTP {r.status_code})")

r = c.put(f"/api/gestione/orario/{s2.json()['id']}", headers=admin,
          json={"giorno": 1, "ora_inizio": "11:00", "ora_fine": "12:30",
                "listato": "B", "aula": "Aula test"})
verifica(r.status_code == 200, "una modifica legittima passa", r.text[:160])


print("\n== Regole d'esame allineate alle schede ministeriali ==")
attese = {"B": (30, 20, 3), "AM": (30, 25, 3), "SUP": (40, 40, 4),
          "CQC": (70, 90, 7), "CAP": (20, 30, 2), "REV_AB": (30, 20, 3),
          "REV_AM": (30, 25, 3), "REV_SUP": (40, 40, 4), "REV_CQC": (70, 90, 7)}
tutti = {l["codice"]: (l["domande_esame"], l["minuti_esame"], l["errori_max"])
         for l in c.get("/api/catalogo/listati", headers=admin).json()}
for codice, atteso in attese.items():
    verifica(tutti.get(codice) == atteso,
             f"{codice}: {atteso[0]} domande / {atteso[1]} min / max {atteso[2]} errori",
             f"trovato {tutti.get(codice)}")


print("\n== Prontezza: tre stati, nessuna data d'esame calcolata ==")
rie = c.get("/api/statistiche/riepilogo", headers=allievo).json()
stato = rie["prontezza"].get("stato")
verifica(stato in ("pronto", "non_pronto", "non_si_esercita"),
         f"stato fra i tre ammessi ({stato})")
verifica("data_esame" not in rie["prontezza"],
         "l'indice di prontezza non propone nessuna data d'esame")

# La data dell'esame la scrive solo l'admin, e resta esattamente quella.
c.put(f"/api/gestione/allievi/{uid}", headers=admin, json={"data_esame": "2026-11-20"})
con = sqlite3.connect(TMP)
d = con.execute("SELECT data_esame FROM utenti WHERE id=?", (uid,)).fetchone()[0]
con.close()
verifica(d == "2026-11-20", f"la data scritta dall'admin resta invariata ({d})")


print("\n== Frontend: difetti di resa corretti ==")
css = (ROOT / "frontend" / "animazioni.css").read_text(encoding="utf-8")
blocco = css.split("@media (prefers-reduced-motion: reduce)")[1]
verifica(re.search(r"\.rivela-riga\s*\{[^}]*opacity:\s*1", blocco) is not None,
         "con «riduci movimento» le righe delle tabelle restano visibili")

js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
verifica("d.risposta_corretta = r.corretta ? (valore ? 1 : 0) : (valore ? 0 : 1);" in js,
         "il feedback immediato indica la risposta esatta giusta")
verifica(re.search(r"^const put\s*=", js, re.M) is not None,
         "put() definita: «Modifica scheda» della scuola puo' salvare")
verifica("barra-prontezza" not in js, "nessuna percentuale di prontezza mostrata all'allievo")

anim = (ROOT / "frontend" / "animazioni.js").read_text(encoding="utf-8")
verifica("^-?[\\d.,]+%?$" in anim,
         "i contatori animati non troncano piu' i valori tipo «23/45»")


print("\n== Catalogo: capitoli e argomenti reali, non un blocco unico ==")
con = sqlite3.connect(TMP)
con.row_factory = sqlite3.Row
struttura = {r["codice"]: (r["cap"], r["arg"], r["dom"]) for r in con.execute("""
    SELECT l.codice,
           (SELECT COUNT(*) FROM capitoli c WHERE c.listato_id = l.id) cap,
           (SELECT COUNT(*) FROM argomenti a JOIN capitoli c ON c.id = a.capitolo_id
            WHERE c.listato_id = l.id) arg,
           (SELECT COUNT(*) FROM domande d WHERE d.listato_id = l.id) dom
    FROM listati l ORDER BY l.codice""")}
for codice, (cap, arg, dom) in struttura.items():
    print(f"       {codice:8} {cap:4} capitoli  {arg:5} argomenti  {dom:6} domande")

verifica(struttura["CQC"][0] >= 16,
         f"la CQC ha i suoi raggruppamenti ministeriali ({struttura['CQC'][0]} capitoli)")
verifica(struttura["CQC"][1] >= 300,
         f"la CQC ha argomenti veri su cui misurare gli errori ({struttura['CQC'][1]})")
degeneri = [c for c, (cap, arg, _) in struttura.items() if arg <= cap]
verifica(not degeneri, f"nessun listato con un argomento solo per capitolo ({degeneri})")

verifica(con.execute("SELECT COUNT(*) FROM domande").fetchone()[0] == 27737,
         "nessuna domanda persa nella ricatalogazione")
verifica(con.execute("""SELECT COUNT(*) FROM domande d
    WHERE (d.capitolo_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM capitoli c WHERE c.id=d.capitolo_id))
       OR (d.argomento_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM argomenti a WHERE a.id=d.argomento_id))
    """).fetchone()[0] == 0, "nessuna domanda orfana")
verifica(con.execute("""SELECT COUNT(*) FROM domande d JOIN argomenti a ON a.id = d.argomento_id
    WHERE a.capitolo_id <> d.capitolo_id""").fetchone()[0] == 0,
         "ogni domanda sta nel capitolo del suo argomento")
verifica(con.execute("""SELECT COUNT(*) FROM risposte r WHERE r.argomento_id IS NOT NULL
    AND NOT EXISTS(SELECT 1 FROM argomenti a WHERE a.id = r.argomento_id)""").fetchone()[0] == 0,
         "le risposte gia' date puntano ad argomenti esistenti")
con.close()

# Una simulazione CQC deve ora pescare da piu' capitoli, non da uno solo.
sim = c.post("/api/quiz/schede", headers=admin, json={"tipo": "simulazione", "listato": "CQC"})
verifica(sim.status_code == 200, "simulazione CQC creata", sim.text[:160])
if sim.status_code == 200:
    dom = c.get(f"/api/quiz/schede/{sim.json()['scheda_id']}", headers=admin).json()["domande"]
    capitoli_estratti = {d["capitolo"] for d in dom}
    verifica(len(capitoli_estratti) >= 5,
             f"la simulazione CQC spazia su {len(capitoli_estratti)} capitoli")
    verifica(len(dom) == 70, f"scheda CQC da 70 domande ({len(dom)})")


ok = sum(1 for e, _ in esiti if e)
print(f"\n{'='*58}\nRISULTATO: {ok}/{len(esiti)} verifiche superate")
for e, d in esiti:
    if not e:
        print("  FALLITA:", d)
sys.exit(0 if ok == len(esiti) else 1)
