# Autoscuola Cruscotto

Piattaforma SaaS didattica per autoscuole italiane: quiz ministeriali, videocorsi, tutor AI e analitiche. Due interfacce, una sola app installabile su PC, tablet e telefono.

**27.737 domande ministeriali** su 9 listati (B, AM, Superiori, CQC, CAP, Revisioni), 637 immagini, 8.080 domande illustrate.

## Avvio

### Windows

Doppio clic su **`Avvia.bat`**. Al primo avvio prepara l'ambiente (circa un minuto), poi apre il browser da solo. Serve Python 3.9+ installato con l'opzione *Add python.exe to PATH*: se manca, il file te lo dice e apre la pagina di download.

### macOS

Doppio clic su **`Avvia.command`** (se il sistema lo blocca: tasto destro > Apri).

### Linux

```bash
./avvia.sh
```

### Opzioni

```bash
python3 avvia.py --porta 9000      # porta diversa
python3 avvia.py --solo-locale     # non esporre sulla rete
python3 avvia.py --no-browser      # non aprire il browser
```

L'avviatore crea l'ambiente virtuale se manca, installa le dipendenze, sceglie una porta libera se la 8080 e' occupata, e apre il browser quando il server risponde.

### Accessi dimostrativi

| Email | Ruolo |
|---|---|
| `marco@demo.it` | Allievo |
| `admin@demo.it` | Amministratore autoscuola |

Password: `demo1234`

### Attivare l'AI Tutor

Rinomina `.env.example` in `.env` e inserisci la chiave:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Senza chiave l'app funziona in tutto il resto; il tutor risponde 503 con un messaggio esplicativo.

## Test

```bash
python3 tests/test_e2e.py           # 47 verifiche end-to-end
```

## Usarlo da tablet e telefono

All'avvio vengono stampati due indirizzi:

```
  Su questo PC:        http://localhost:8080/
  Da tablet/telefono:  http://192.168.1.20:8080/
```

Il secondo funziona da qualsiasi dispositivo sullo **stesso Wi-Fi**: inquadra il QR code mostrato nel terminale, oppure digita l'indirizzo. Dal telefono, *Condividi > Aggiungi a schermata Home* installa l'app come se fosse nativa.

Se non compare l'indirizzo di rete, il firewall di Windows sta bloccando la porta: alla prima esecuzione consenti l'accesso alle **reti private** quando Windows lo chiede.

## Eseguibile autonomo

Per distribuirlo a chi non ha Python:

```bash
python3 build_exe.py               # cartella dist/Cruscotto/ con l'eseguibile
python3 build_exe.py --file-unico  # un unico file, avvio piu' lento
```

Va compilato **sul sistema di destinazione**: PyInstaller non fa cross-compilazione, quindi il `.exe` per Windows si ottiene compilando su Windows. Al primo avvio l'eseguibile crea accanto a se' la cartella `dati-cruscotto/` con il database: e' quella da salvare nei backup.

## Pubblicarlo online

```bash
cp deploy/.env.example deploy/.env     # dominio, email, segreto JWT
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
```

HTTPS automatico con Caddy. Guida completa, alternative senza Docker e lista di controllo pre-produzione in [`deploy/README.md`](deploy/README.md).

## Manutenzione

```bash
python3 scripts/backup.py --comprimi --tieni 14     # backup consistente
python3 scripts/reaper_sessioni.py                  # chiude le sessioni orfane
python3 scripts/ricostruisci_aggregati.py --verifica
python3 scripts/pregenera_spiegazioni.py --stima    # preventivo cache AI
python3 scripts/gestione_utenti.py elenca
crontab scripts/manutenzione.crontab                # pianificazione completa
```

Dettagli in [`scripts/README.md`](scripts/README.md).

## Ricostruire il database dalle fonti

```bash
python3 etl/parse_pdf.py "Domande AM italiano 04 04 2025.pdf" data/raw/am.json data/media AM
python3 etl/build_db.py --json-b quizPatenteB2023.json --img-b img_sign --demo --reset
```

Per PDF di oltre ~120 pagine il parsing accetta un intervallo (`0:95`) e i frammenti si uniscono con `etl/merge_parts.py`.

## Funzionalità

**Allievo** — esercitazioni filtrate per capitolo, simulazione d'esame con timer e regole ministeriali per listato, ripasso mirato con algoritmo di ripetizione spaziata, videocorsi con resume cross-device e lezioni live, statistiche personali, tutor AI che spiega gli errori analizzando anche la figura.

**Autoscuola** — KPI di classe, elenco allievi ordinato per indice di prontezza, dettaglio individuale con tempi d'uso e punti deboli, colli di bottiglia formativi dell'intera classe, domande più sbagliate, consumo AI.

## Architettura

Documento completo in [`docs/ARCHITETTURA.md`](docs/ARCHITETTURA.md): schema relazionale motivato, integrazione LLM con system prompt in chiaro, logica di analytics e spaced repetition, user flow, roadmap.

Backend Python/FastAPI, SQLite in WAL, frontend HTML/CSS/JS con Bootstrap 5 come PWA installabile e funzionante offline.
