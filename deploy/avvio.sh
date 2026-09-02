#!/bin/sh
# =============================================================================
#  Autoscuola Cruscotto - preparazione del database prima di avviare il server
#
#  Il database di lavoro (data/autoscuola.db) non e' su GitHub: contiene
#  utenti, password, token e dati degli allievi. Nell'immagine arriva solo la
#  copia demo, anonima.
#
#  Senza questo passaggio, al primo avvio su un volume vuoto SQLite creerebbe
#  da solo un file vuoto: il server parte, il login fallisce e il pannello
#  admin mostra l'elenco allievi deserto, senza un errore che spieghi perche'.
# =============================================================================
set -e

DB="${AC_DB:-/app/data/autoscuola.db}"
SEME="${AC_DB_SEME:-/app/seme/autoscuola.demo.db}"

if [ ! -f "$DB" ]; then
    if [ -f "$SEME" ]; then
        mkdir -p "$(dirname "$DB")"
        cp "$SEME" "$DB"
        echo "primo avvio: database di lavoro creato dalla copia demo ($DB)"
    else
        echo "! $DB non esiste e manca anche la copia demo $SEME:" >&2
        echo "! il server partira' su un database vuoto (niente domande, niente utenti)." >&2
    fi
fi

exec "$@"
