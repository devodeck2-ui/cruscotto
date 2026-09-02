#!/usr/bin/env python3
"""Promemoria all'allievo che ha smesso di esercitarsi.

L'app serve a chi la apre: la maggior parte degli abbandoni non e' una
decisione, e' semplicemente il ritmo che si perde dopo qualche giorno pieno.
Questo job guarda chi non completa una scheda da un po' e gli manda un
richiamo breve, con la stessa infrastruttura delle notifiche delle lezioni
(popup se ha dato il permesso, campanellino in ogni caso).

Tre regole che lo tengono dalla parte giusta del confine fra utile e molesto:
  * si conta l'ULTIMA scheda completata, non l'ultimo accesso: chi apre l'app
    per guardare un video sta comunque studiando, ma chi non risponde a una
    domanda da giorni si e' fermato davvero;
  * un tetto settimanale (default 2): chi ignora due richiami non viene
    tempestato, altrimenti la prima cosa che fa e' togliere il permesso;
  * niente doppioni nello stesso giorno, anche se il job venisse lanciato
    due volte per errore.

Da schedulare una volta al giorno, nel primo pomeriggio o alla sera - vedi
manutenzione.crontab.

    python3 scripts/promemoria_studio.py [--giorni 2] [--max-settimana 2] [--dry-run]
"""
from __future__ import annotations

import argparse

from _comune import log

# Frasi diverse a rotazione: lo stesso testo ripetuto ogni volta diventa
# rumore che l'occhio salta. Sono volutamente brevi (su un telefono la
# notifica viene tagliata dopo poche parole) e mai in colpa: invitano,
# non rimproverano.
MESSAGGI = [
    ("E' l'ora di qualche quiz",        "Dieci minuti di schede e resti in ritmo."),
    ("Riprendiamo da dove eri",         "Una scheda veloce sui capitoli che stai preparando."),
    ("Il tuo ripasso ti aspetta",       "Bastano 10 domande per non perdere il filo."),
    ("Due minuti o dieci, decidi tu",   "Anche una scheda breve tiene allenato l'occhio."),
    ("Ci rimettiamo sotto?",            "Riprendi con un quiz sugli argomenti dove sbagli di piu'."),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--giorni", type=int, default=2,
                    help="giorni senza schede completate oltre i quali si avvisa (default 2)")
    ap.add_argument("--max-settimana", type=int, default=2,
                    help="promemoria massimi per allievo negli ultimi 7 giorni (default 2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra chi verrebbe avvisato senza scrivere nulla")
    args = ap.parse_args()

    from app import db
    from app.services import notifiche

    # Solo allievi attivi, e conta la scheda completata: chi apre l'app tutti
    # i giorni ma non risponde a una domanda va richiamato comunque. Chi si e'
    # appena iscritto no: gli si lascia almeno il tempo della soglia prima di
    # rimproverargli di non aver ancora fatto nulla. Chi ha gia' sostenuto
    # l'esame esce dall'elenco da solo (data_esame passata).
    candidati = db.rows_to_dicts(db.query(
        "SELECT u.id, u.nome, "
        "       (SELECT MAX(s.conclusa_il) FROM schede s "
        "         WHERE s.utente_id = u.id AND s.stato = 'completata') AS ultima_scheda "
        "  FROM utenti u JOIN ruoli r ON r.id = u.ruolo_id "
        " WHERE u.attivo = 1 AND r.codice = 'allievo' "
        "   AND u.created_at < datetime('now', ?) "
        "   AND (u.data_esame IS NULL OR u.data_esame >= date('now')) "
        "   AND NOT EXISTS (SELECT 1 FROM schede s WHERE s.utente_id = u.id "
        "                     AND s.stato = 'completata' "
        "                     AND s.conclusa_il >= datetime('now', ?)) "
        "   AND (SELECT COUNT(*) FROM notifica n WHERE n.utente_id = u.id "
        "          AND n.tipo = 'promemoria_studio' "
        "          AND n.created_at >= datetime('now', '-7 days')) < ? "
        "   AND NOT EXISTS (SELECT 1 FROM notifica n WHERE n.utente_id = u.id "
        "                     AND n.tipo = 'promemoria_studio' "
        "                     AND date(n.created_at) = date('now')) "
        " ORDER BY u.id",
        (f"-{args.giorni} days", f"-{args.giorni} days", args.max_settimana)))

    if not candidati:
        log("nessun allievo da richiamare")
        return 0

    for i, a in enumerate(candidati):
        titolo, corpo = MESSAGGI[i % len(MESSAGGI)]
        fermo_da = a["ultima_scheda"] or "mai"
        if args.dry_run:
            log(f"[dry-run] avviserei {a['nome']} (id {a['id']}, ultima scheda: {fermo_da})")
            continue
        notifiche.notifica_utenti([a["id"]], "promemoria_studio", titolo, corpo, "/#/esercitati")
        log(f"promemoria a {a['nome']} (id {a['id']}, ultima scheda: {fermo_da})")

    log(f"{'[dry-run] ' if args.dry_run else ''}allievi richiamati: {len(candidati)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
