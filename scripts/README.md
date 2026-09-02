# Script di manutenzione

Tutti gli script si eseguono da questa cartella e leggono la configurazione dalle
stesse variabili d'ambiente dell'applicazione (`AC_DB`, `AC_MEDIA`, `ANTHROPIC_API_KEY`).
Per lavorare su una copia del database senza toccare quello di produzione:

```bash
AC_DB=/tmp/prova.db python3 scripts/reaper_sessioni.py --dry-run
```

| Script | Frequenza consigliata | Cosa fa |
|---|---|---|
| `backup.py` | notturna | Copia consistente via `VACUUM INTO`, con rotazione, compressione e cifratura opzionali (`--cifra`, `--decifra`) |
| `reaper_sessioni.py` | ogni minuto | Chiude le sessioni orfane impostando `fine = ultimo_ping` |
| `ricostruisci_aggregati.py` | dopo import / settimanale in `--verifica` | Rigenera o controlla gli aggregati statistici |
| `pregenera_spiegazioni.py` | settimanale | Popola la cache dell'AI Tutor sulle domande piu' sbagliate |
| `promemoria_studio.py` | giornaliera (18:00) | Richiama gli allievi fermi da 2 giorni, max 2 promemoria a settimana |
| `gestione_utenti.py` | a richiesta | Provisioning di autoscuole e utenti, reset password |

La pianificazione pronta all'uso e' in `manutenzione.crontab`.

## Note operative

**Il backup va fatto con `VACUUM INTO`, non con `cp`.** Il database gira in modalita'
WAL: copiare il solo file `.db` mentre l'applicazione scrive produce un backup
silenziosamente incompleto, perche' le transazioni recenti stanno nel file `-wal`.
`VACUUM INTO` risolve alla radice e restituisce per giunta un file gia' compattato.

**Il reset password revoca tutte le sessioni.** Se si reimposta una password per un
sospetto accesso abusivo, lasciare vivi i refresh token vanificherebbe l'operazione.

**La pre-generazione richiede storico d'uso.** Ordina per difficolta' osservata: su
un'installazione appena avviata non ha dati su cui lavorare e lo dichiara. Usare
`--stima` per vedere il preventivo di spesa prima di lanciare il job.
