#!/usr/bin/env python3
"""Gestione di autoscuole e utenti da riga di comando.

Serve per il provisioning iniziale (creare il primo tenant e il suo
amministratore, quando ancora non esiste nessuno che possa farlo dalla
dashboard) e per gli interventi di assistenza.

    python3 scripts/gestione_utenti.py crea-autoscuola --nome "Autoscuola Rossi" --slug rossi
    python3 scripts/gestione_utenti.py crea-utente --autoscuola rossi \
        --email mario@rossi.it --nome Mario --cognome Rossi --ruolo admin
    python3 scripts/gestione_utenti.py reimposta-password --email mario@rossi.it
    python3 scripts/gestione_utenti.py elenca --autoscuola rossi
"""
from __future__ import annotations

import argparse
import secrets
import string

from _comune import log


def password_casuale(n: int = 12) -> str:
    alfabeto = string.ascii_letters + string.digits
    return "".join(secrets.choice(alfabeto) for _ in range(n))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando", required=True)

    a = sub.add_parser("crea-autoscuola")
    a.add_argument("--nome", required=True)
    a.add_argument("--slug", required=True)
    a.add_argument("--citta", default=None)
    a.add_argument("--piano", default="base", choices=["base", "pro", "enterprise"])

    u = sub.add_parser("crea-utente")
    u.add_argument("--autoscuola", required=True, help="slug dell'autoscuola")
    u.add_argument("--email", required=True)
    u.add_argument("--nome", required=True)
    u.add_argument("--cognome", required=True)
    u.add_argument("--ruolo", default="allievo",
                   choices=["allievo", "istruttore", "admin", "superadmin"])
    u.add_argument("--listato", default="B")
    u.add_argument("--password", default=None, help="se omessa, ne viene generata una")

    r = sub.add_parser("reimposta-password")
    r.add_argument("--email", required=True)
    r.add_argument("--password", default=None)

    e = sub.add_parser("elenca")
    e.add_argument("--autoscuola", default=None)

    args = ap.parse_args()

    from app import db
    from app.security import hash_password

    if args.comando == "crea-autoscuola":
        db.execute("INSERT INTO autoscuole(ragione_sociale, slug, citta, piano) VALUES(?,?,?,?)",
                   (args.nome, args.slug, args.citta, args.piano))
        log(f"autoscuola creata: {args.nome} (slug {args.slug})")
        return 0

    if args.comando == "crea-utente":
        t = db.query_one("SELECT id FROM autoscuole WHERE slug = ?", (args.autoscuola,))
        if not t:
            log(f"autoscuola '{args.autoscuola}' inesistente")
            return 1
        ruolo = db.query_one("SELECT id FROM ruoli WHERE codice = ?", (args.ruolo,))["id"]
        pwd = args.password or password_casuale()
        # Nell'app si entra con il nome utente: se non lo si genera qui, l'utente
        # appena creato non avrebbe con cosa accedere finche' non riparte il server.
        from app.routers.gestione import _slug_nome, _username_libero
        username = _username_libero(_slug_nome(args.nome, args.cognome))
        try:
            db.execute(
                "INSERT INTO utenti(autoscuola_id, ruolo_id, email, username, password_hash,"
                " nome, cognome, listato_target) VALUES(?,?,?,?,?,?,?,?)",
                (t["id"], ruolo, args.email.lower(), username, hash_password(pwd),
                 args.nome, args.cognome, args.listato))
        except Exception as exc:
            log(f"impossibile creare l'utente: {exc}")
            return 1
        log(f"utente creato: {args.email} ({args.ruolo})")
        log(f"nome utente: {username}   <-- si entra con questo, non con l'email")
        if not args.password:
            log(f"password generata: {pwd}   <-- comunicala e falla cambiare al primo accesso")
        return 0

    if args.comando == "reimposta-password":
        pwd = args.password or password_casuale()
        cur = db.execute("UPDATE utenti SET password_hash = ? WHERE email = ?",
                         (hash_password(pwd), args.email.lower()))
        if not cur.rowcount:
            log(f"nessun utente con email {args.email}")
            return 1
        chi = db.query_one("SELECT username FROM utenti WHERE email = ?", (args.email.lower(),))
        if chi and chi["username"]:
            log(f"nome utente: {chi['username']}   <-- si entra con questo, non con l'email")
        # Tutte le sessioni attive vengono invalidate: se la password e' stata
        # reimpostata per un sospetto accesso abusivo, lasciare vivi i refresh
        # token vanificherebbe l'operazione.
        db.execute("UPDATE refresh_token SET revocato = 1 WHERE utente_id IN "
                   "(SELECT id FROM utenti WHERE email = ?)", (args.email.lower(),))
        log(f"password reimpostata per {args.email}: {pwd}")
        log("tutte le sessioni attive dell'utente sono state revocate")
        return 0

    if args.comando == "elenca":
        sql = ("SELECT u.id, u.email, u.nome, u.cognome, r.codice AS ruolo, u.attivo,"
               "       a.slug, u.ultimo_accesso FROM utenti u "
               "JOIN ruoli r ON r.id = u.ruolo_id JOIN autoscuole a ON a.id = u.autoscuola_id")
        par = ()
        if args.autoscuola:
            sql += " WHERE a.slug = ?"
            par = (args.autoscuola,)
        righe = db.query(sql + " ORDER BY a.slug, r.codice, u.cognome", par)
        print(f"{'id':>4}  {'autoscuola':<12} {'ruolo':<11} {'email':<28} {'stato':<8} ultimo accesso")
        for x in righe:
            print(f"{x['id']:>4}  {x['slug']:<12} {x['ruolo']:<11} {x['email']:<28} "
                  f"{'attivo' if x['attivo'] else 'disattivo':<8} {x['ultimo_accesso'] or '-'}")
        print(f"\ntotale: {len(righe)} utenti")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
