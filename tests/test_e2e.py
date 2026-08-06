"""Test end-to-end: percorso completo dell'allievo + isolamento dei dati.

Si esegue in-process con il TestClient di Starlette su una copia del database,
quindi non richiede un server avviato ne' sporca i dati reali.

    python3 tests/test_e2e.py
"""
from __future__ import annotations

import os
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

from fastapi.testclient import TestClient          # noqa: E402
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


print("\n== 1. Salute e catalogo ==")
s = c.get("/api/salute").json()
verifica(s["database"]["domande"] > 25000, f"catalogo caricato ({s['database']['domande']} domande)")
verifica(s["database"]["listati"] == 9, "9 listati ministeriali presenti")

print("\n== 2. Autenticazione ==")
verifica(c.post("/api/auth/login", json={"email": "marco@demo.it", "password": "sbagliata"}).status_code == 401,
         "password errata respinta")
allievo, u_allievo = login("marco@demo.it")
admin, u_admin = login("admin@demo.it")
verifica(u_allievo["ruolo"] == "allievo" and u_admin["ruolo"] == "admin", "ruoli assegnati correttamente")
verifica(c.get("/api/auth/me").status_code == 401, "endpoint protetto senza token respinto")

print("\n== 3. Catalogo per l'allievo ==")
listati = c.get("/api/catalogo/listati", headers=allievo).json()
verifica(len(listati) == 9, "elenco listati")
cap = c.get("/api/catalogo/capitoli?listato=B", headers=allievo).json()
verifica(len(cap) >= 20, f"capitoli listato B ({len(cap)})")
ric = c.get("/api/catalogo/cerca?q=precedenza&listato=B", headers=allievo).json()
verifica(len(ric) > 0, f"ricerca full-text ({len(ric)} risultati)")

print("\n== 4. Simulazione d'esame ==")
r = c.post("/api/quiz/schede", headers=allievo, json={"tipo": "simulazione", "listato": "B"})
verifica(r.status_code == 200, "creazione simulazione", r.text)
sim = r.json()
verifica(sim["n_domande"] == 30, f"30 domande ({sim['n_domande']})")
verifica(sim["limite_sec"] == 1200, f"timer di 20 minuti ({sim['limite_sec']}s)")

dati = c.get(f"/api/quiz/schede/{sim['scheda_id']}", headers=allievo).json()
verifica(all("risposta_corretta" not in d for d in dati["domande"]),
         "soluzioni NON esposte durante la simulazione")
capitoli_estratti = {d["capitolo"] for d in dati["domande"]}
verifica(len(capitoli_estratti) >= 8, f"domande distribuite su {len(capitoli_estratti)} capitoli")
con_img = sum(1 for d in dati["domande"] if d["immagine"])
print(f"       ({con_img}/30 domande con figura)")

# Risponde: le prime 5 volutamente sbagliate, le altre corrette.
errori_attesi = 0
for i, d in enumerate(dati["domande"]):
    corretta_reale = None
    import sqlite3
    con = sqlite3.connect(TMP)
    corretta_reale = con.execute("SELECT risposta FROM domande WHERE id=?", (d["domanda_id"],)).fetchone()[0]
    con.close()
    risposta = (not corretta_reale) if i < 5 else bool(corretta_reale)
    if i < 5:
        errori_attesi += 1
    rr = c.post(f"/api/quiz/schede/{sim['scheda_id']}/rispondi", headers=allievo,
                json={"posizione": d["posizione"], "risposta": risposta, "tempo_ms": 3000})
    assert rr.status_code == 200, rr.text
    verifica("corretta" not in rr.json(), "esito non rivelato in simulazione") if i == 0 else None

esito = c.post(f"/api/quiz/schede/{sim['scheda_id']}/chiudi", headers=allievo,
               json={"durata_sec": 600, "motivo": "completata"}).json()
verifica(esito["riepilogo"]["n_errori"] == errori_attesi,
         f"conteggio errori esatto ({esito['riepilogo']['n_errori']} = {errori_attesi})")
verifica(esito["riepilogo"]["superata"] is False, "5 errori > 3 consentiti -> non superata")
verifica(all("risposta_corretta" in d for d in esito["domande"]), "soluzioni rivelate dopo la consegna")

print("\n== 5. Esercitazione per argomento ==")
scelti = [cap[0]["id"], cap[1]["id"]]
ex = c.post("/api/quiz/schede", headers=allievo,
            json={"tipo": "esercitazione", "listato": "B", "capitoli": scelti, "n_domande": 10}).json()
dati_ex = c.get(f"/api/quiz/schede/{ex['scheda_id']}", headers=allievo).json()
titoli = {cap[0]["titolo"], cap[1]["titolo"]}
verifica(all(d["capitolo"] in titoli for d in dati_ex["domande"]), "filtro per capitolo rispettato")
verifica(ex["limite_sec"] is None, "esercitazione senza timer")
r1 = c.post(f"/api/quiz/schede/{ex['scheda_id']}/rispondi", headers=allievo,
            json={"posizione": 1, "risposta": True, "tempo_ms": 2000}).json()
verifica("corretta" in r1, "correzione immediata in esercitazione")

print("\n== 6. Ripetizione mirata (SRS) ==")
rip = c.get("/api/quiz/da-ripassare", headers=allievo).json()
verifica(rip["n_domande"] >= 5, f"domande sbagliate in coda di ripasso ({rip['n_domande']})")
rec = c.post("/api/quiz/schede", headers=allievo, json={"tipo": "recupero", "listato": "B", "n_domande": 20})
verifica(rec.status_code == 200, "creazione scheda di recupero", rec.text)
dati_rec = c.get(f"/api/quiz/schede/{rec.json()['scheda_id']}", headers=allievo).json()
sbagliate = {d["domanda_id"] for d in esito["domande"] if d["corretta"] == 0}
riproposte = sbagliate & {d["domanda_id"] for d in dati_rec["domande"]}
verifica(len(riproposte) > 0, f"il recupero ripropone le domande sbagliate ({len(riproposte)})")

print("\n== 7. Analitiche ==")
st = c.get("/api/statistiche/riepilogo", headers=allievo).json()
verifica(st["profilo"]["risposte_totali"] >= 31, f"risposte aggregate ({st['profilo']['risposte_totali']})")
verifica(0 <= st["prontezza"]["punteggio"] <= 100, f"indice di prontezza {st['prontezza']['punteggio']}")
verifica(len(st["capitoli"]) >= 20, "riepilogo per capitolo")
crit = c.get("/api/statistiche/criticita", headers=allievo).json()
print(f"       argomenti critici rilevati: {len(crit)}")

print("\n== 8. Tracciamento del tempo ==")
ses = c.post("/api/sessioni/apri", headers=allievo, json={"piattaforma": "web"}).json()
verifica("sessione_id" in ses, "sessione aperta")
pg = c.post("/api/sessioni/ping", headers=allievo,
            json={"sessione_id": ses["sessione_id"], "sezione": "quiz", "delta_sec": 30}).json()
verifica(pg["conteggiati_sec"] <= 30, f"delta limitato al tempo reale ({pg['conteggiati_sec']}s)")
pg2 = c.post("/api/sessioni/ping", headers=allievo,
             json={"sessione_id": ses["sessione_id"], "sezione": "quiz", "delta_sec": 120}).json()
verifica(pg2["conteggiati_sec"] < 10, "tentativo di gonfiare il tempo neutralizzato")

print("\n== 9. RBAC e isolamento dati ==")
verifica(c.get("/api/admin/panoramica", headers=allievo).status_code == 403,
         "allievo NON accede alla dashboard admin")
verifica(c.get("/api/admin/allievi", headers=admin).status_code == 200,
         "admin accede all'elenco allievi")
altrui = c.get(f"/api/quiz/schede/{sim['scheda_id']}", headers=login('sara@demo.it')[0])
verifica(altrui.status_code == 404, "allievo NON legge le schede di un altro allievo")
det = c.get(f"/api/admin/allievi/{u_allievo['id']}", headers=admin)
verifica(det.status_code == 200, "admin ispeziona il proprio allievo")
verifica(c.get("/api/admin/allievi/999999", headers=admin).status_code == 404,
         "admin non raggiunge utenti fuori dal proprio tenant")

print("\n== 10. Dashboard autoscuola ==")
pan = c.get("/api/admin/panoramica", headers=admin).json()
verifica(pan["totali"]["allievi"] >= 4, f"allievi del tenant ({pan['totali']['allievi']})")
verifica("colli_di_bottiglia" in pan and "domande_critiche" in pan, "aggregati didattici presenti")
al = c.get("/api/admin/allievi?ordina=prontezza", headers=admin).json()
verifica(all("prontezza" in a for a in al), "indice di prontezza per ogni allievo")

print("\n== 11. Videocorsi ==")
corsi = c.get("/api/video/corsi", headers=allievo).json()
verifica(len(corsi) >= 1, "corso pubblicato visibile")
lez = c.get(f"/api/video/corsi/{corsi[0]['id']}/lezioni", headers=allievo).json()
verifica(len(lez) >= 8, f"lezioni del corso ({len(lez)})")
verifica(any(l["tipo"] == "live" for l in lez), "lezione live programmata presente")
c.post(f"/api/video/lezioni/{lez[0]['id']}/progresso", headers=allievo,
       json={"posizione_sec": 300, "delta_sec": 60})
lez2 = c.get(f"/api/video/corsi/{corsi[0]['id']}/lezioni", headers=allievo).json()
verifica(lez2[0]["riprendi_da"] == 300, "resume point salvato lato server")

print("\n== 12. AI Tutor ==")
dom_err = next(d["domanda_id"] for d in esito["domande"] if d["corretta"] == 0)
pv = c.get(f"/api/tutor/anteprima-prompt/{dom_err}", headers=allievo).json()
verifica("istruttore di teoria" in pv["system"], "system prompt caricato")
verifica("Risposta data dall'allievo" in pv["user"], "prompt utente compilato con la risposta data")
sp = c.post("/api/tutor/spiega", headers=allievo, json={"domanda_id": dom_err})
verifica(sp.status_code in (200, 503), "endpoint tutor raggiungibile")
if sp.status_code == 503:
    print("       (chiave API non configurata: risposta 503 attesa)")
nonvista = c.post("/api/tutor/spiega", headers=allievo, json={"domanda_id": 999999})
verifica(nonvista.status_code == 404, "tutor NON risponde su domande mai affrontate")

print("\n== 13. Coerenza degli aggregati ==")
import sqlite3
con = sqlite3.connect(TMP)
reali = con.execute(
    "SELECT COUNT(*), SUM(CASE WHEN corretta=0 THEN 1 ELSE 0 END) FROM risposte "
    "WHERE utente_id=? AND corretta IS NOT NULL AND argomento_id IS NOT NULL", (u_allievo["id"],)).fetchone()
aggr = con.execute("SELECT SUM(n_risposte), SUM(n_errori) FROM stat_utente_argomento WHERE utente_id=?",
                   (u_allievo["id"],)).fetchone()
con.close()
verifica(reali == aggr, f"rollup identico al ricalcolo ({aggr} = {reali})")
rb = c.post("/api/admin/manutenzione/ricostruisci-aggregati", headers=admin).json()
verifica(rb["righe_ricostruite"] > 0, f"ricostruzione aggregati ({rb['righe_ricostruite']} righe)")

ok = sum(1 for e, _ in esiti if e)
print(f"\n{'='*58}\nRISULTATO: {ok}/{len(esiti)} verifiche superate")
for e, d in esiti:
    if not e:
        print("  FALLITA:", d)
sys.exit(0 if ok == len(esiti) else 1)
