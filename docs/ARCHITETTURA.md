# Autoscuola Cruscotto — Architettura, modello dati e roadmap

**Versione 1.0 — 24 luglio 2026**
Documento di architettura per una piattaforma SaaS didattica multi-tenant destinata alle autoscuole italiane. Il perimetro è esclusivamente formativo: quiz ministeriali, videocorsi, tutoraggio AI e analitiche. **Non** sono previsti calendari, prenotazioni di guide o gestione appuntamenti.

Il documento accompagna un'implementazione funzionante e verificata (47/47 test end-to-end superati) con **27.737 domande ministeriali** già caricate su 9 listati.

---

## 0. Sintesi delle scelte e loro giustificazione

| Decisione | Scelta | Motivazione |
|---|---|---|
| Stile architetturale | Monolite modulare, non microservizi | Con 10–500 autoscuole e traffico burst nelle ore serali, i microservizi aggiungerebbero latenza di rete e complessità operativa senza alcun beneficio. La modularizzazione per router/service rende l'estrazione futura un'operazione meccanica. |
| Persistenza | SQLite in WAL | Il carico è **read-heavy** (95% letture: domande, immagini, statistiche). Le scritture sono piccole e serializzabili. WAL consente letture concorrenti durante le scritture. Il layer dati è isolato in `db.py`: la migrazione a PostgreSQL non tocca la logica applicativa. |
| Backend | Python 3.10+ / FastAPI | Tipizzazione con Pydantic, validazione automatica degli input, OpenAPI generato, ASGI asincrono per le chiamate LLM I/O-bound. |
| Frontend | HTML + CSS + JS vanilla + Bootstrap 5, come PWA | Deve girare fluido su tablet economici e telefoni di fascia bassa — la dotazione reale delle aule. Bundle < 40 KB, nessun build step, installabile su PC/tablet/telefono, funzionante offline. |
| Isolamento dati | RBAC applicativo + tenant scoping obbligatorio | Replica a livello applicativo ciò che PostgreSQL offrirebbe con Row Level Security. |
| Analitiche | Aggregazione **write-time** (rollup incrementale) | Trasforma letture O(n·storico) in letture O(#argomenti). |
| AI Tutor | Claude via proxy backend, con cache e vision | La chiave API non lascia mai il server; la cache satura e azzera il costo marginale. |

### Perché il monolite modulare è la scelta corretta *qui*

Il requisito parla di alta scalabilità, ma la scalabilità va dimensionata sul carico reale. Un'autoscuola di medie dimensioni ha 80–150 allievi attivi; una rete di 50 autoscuole significa ~6.000 allievi, con un picco stimato di 200 sessioni concorrenti nella fascia 18:00–22:00. Sono numeri che un singolo processo Python con 4 worker gestisce con margine ampio.

Ciò che invece **non** scala nel monolite sono due componenti, ed è su quelli che la modularizzazione è progettata per essere reversibile:

1. **Servizio AI** (`services/tutor.py`) — I/O-bound, latenza 2–8 secondi per chiamata. Nel momento in cui le richieste concorrenti superano il pool di worker, si estrae in un servizio separato con una coda (Redis/RabbitMQ) e risposta in streaming SSE. L'interfaccia è già una singola funzione, `spiega_errore(domanda, risposta_data, tenant)`.
2. **Motore analitico** (`services/analytics.py`) — oggi write-time incrementale. Superata la soglia di ~50 milioni di righe in `risposte`, il rollup si sposta su un job batch notturno con tabelle partizionate per mese, senza modificare le API di lettura.

---

## 1. Architettura del database

### 1.1 Diagramma delle relazioni

```
autoscuole (tenant)
   |
   +-- utenti --< ruoli
   |     |
   |     +--< refresh_token
   |     +--< sessioni_app          (tracciamento tempo)
   |     +--< schede --< risposte   (storico immutabile)
   |     +--< srs_stato             (ripetizione mirata)
   |     +--< stat_utente_argomento (rollup)
   |     +--< stat_utente_giorno    (rollup)
   |     +--< visione_video
   |     +--< ai_conversazioni
   |
   +--< corsi --< lezioni_video
   +--< ai_consumo                  (budget e rate limit)

CATALOGO MINISTERIALE (condiviso, read-mostly)
listati --< capitoli --< argomenti
   |
   +--< quesiti --< domande >-- immagini
                       |
                       +-- domande_fts (indice FTS5)
                       +--< ai_spiegazioni (cache LLM)
```

### 1.2 Tabelle e scelte di modellazione

Lo schema completo, commentato, è in **`backend/app/schema.sql`** (23 tabelle, 33 indici, 2 viste). Qui si motivano le decisioni non ovvie.

#### Il catalogo è a quattro livelli, non a due

```sql
listati (B, AM, SUP, CQC, CAP, REV_*)
  └── capitoli      (25 per il listato B)
        └── argomenti   (1.172 in totale)
              └── domande (27.737)
```

Un modello a due livelli (capitolo → domanda) sarebbe più semplice ma renderebbe la ripetizione mirata inutile. Il capitolo "Segnali di pericolo" contiene 400 domande: dire a un allievo "sbagli i segnali di pericolo" non è azionabile. L'argomento — *"strada dissestata"*, *"attraversamento pedonale"* — è la granularità su cui l'errore è diagnostico e su cui la scheda di recupero diventa efficace.

Il livello **`quesiti`** modella la struttura reale del listato ministeriale: un tronco comune (`"Il segnale raffigurato..."`) con N affermazioni vero/falso e **una sola immagine condivisa**. Serve per due ragioni: permette di ricostruire la scheda d'esame nella sua forma autentica, e collega correttamente l'immagine a tutte le affermazioni del gruppo.

#### Tipi di dato strategici

| Colonna | Tipo | Perché |
|---|---|---|
| `schede.parametri` | `TEXT CHECK(json_valid(...))` | Equivalente funzionale di **JSONB**. Registra filtri, seed di generazione e parametri dell'algoritmo SRS. Schema variabile per natura: metterlo in colonne significherebbe una migrazione a ogni nuovo tipo di scheda. Interrogabile con gli operatori JSON1: `json_extract(parametri,'$.algoritmo')`. |
| `sessioni_app.breakdown` | JSON | `{"quiz":900,"video":320,"statistiche":45}` — la ripartizione del tempo per sezione. Numero di sezioni variabile nel tempo. |
| `ai_conversazioni.messaggi` | JSON | Cronologia della chat col tutor. Documento coeso: non ha senso normalizzarlo in righe. |
| `stat_utente_argomento.ema_errore` | `REAL` | Media mobile esponenziale del tasso d'errore (§3.2). Una sola colonna sostituisce la scansione dello storico. |
| `domande.hash_testo` | `TEXT` + UNIQUE | SHA-1 del testo normalizzato. I listati si sovrappongono (REV_AB ripete molte domande di B): l'hash rende l'import **idempotente**. Ha eliminato 715 duplicati su 28.452 righe importate. |
| `immagini.percorso` | content-addressed (SHA-1) | Lo stesso segnale compare in centinaia di domande. 8.080 domande con figura sono servite da **637 file fisici**. Abilita `Cache-Control: immutable`. |
| `utenti.password_hash` | PBKDF2-HMAC-SHA256, 260k iterazioni | Nessuna dipendenza esterna, formato autodescrittivo `algo$iter$salt$hash`, verifica a tempo costante. |
| `risposte.capitolo_id / argomento_id` | denormalizzati | **Snapshot storico.** Se il MIT riorganizza i capitoli, le statistiche passate non devono cambiare retroattivamente. |

#### Chiavi esterne e politiche di cancellazione

Due politiche distinte, non una:

- **`ON DELETE CASCADE`** sui dati utente: cancellare un tenant o un utente rimuove tutto ciò che gli appartiene. Requisito GDPR (diritto all'oblio).
- **`ON DELETE RESTRICT`** sul catalogo ministeriale: `risposte.domanda_id` impedisce di cancellare una domanda già somministrata. Lo storico deve restare interpretabile. La dismissione avviene con `domande.attiva = 0` (soft delete).
- **`ON DELETE SET NULL`** sui riferimenti di classificazione (`capitolo_id` in `domande`): una riorganizzazione del catalogo non deve distruggere le domande.

#### Indici: perché questi e non altri

Ogni indice è un costo in scrittura. Sono stati creati solo quelli che servono a una query reale del prodotto.

```sql
-- 1. Generatore di schede: il 90% del traffico di lettura sul catalogo.
--    Indice DI COPERTURA: la query si risolve senza toccare la tabella.
CREATE INDEX ix_domande_sel ON domande(listato_id, capitolo_id, attiva, id);

-- 2. Analitiche per argomento: cardine della dashboard e dell'SRS.
CREATE INDEX ix_risposte_utente_arg ON risposte(utente_id, argomento_id, corretta);

-- 3. "Cosa devo ripassare oggi": range scan su una sola colonna ordinata.
CREATE INDEX ix_srs_due ON srs_stato(utente_id, prossima_il);

-- 4. Indice PARZIALE: solo le sessioni aperte, che sono poche decine
--    su milioni di righe storiche. Il reaper lo scansiona ogni minuto.
CREATE INDEX ix_sess_aperte ON sessioni_app(ultimo_ping) WHERE fine IS NULL;

-- 5. Feed cronologici: l'ordinamento è già nell'indice, niente sort in memoria.
CREATE INDEX ix_schede_utente ON schede(utente_id, iniziata_il DESC);
CREATE INDEX ix_schede_tenant ON schede(autoscuola_id, iniziata_il DESC);
```

Le tabelle di aggregazione (`stat_utente_argomento`, `stat_utente_giorno`, `srs_stato`) usano **`WITHOUT ROWID`**: sono tabelle a chiave composita accedute sempre per chiave primaria. Eliminare il rowid nascosto significa una struttura B-tree in meno e circa il 30% di spazio risparmiato.

#### Ricerca full-text

```sql
CREATE VIRTUAL TABLE domande_fts USING fts5(
    testo, content='domande', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');
```

Tabella **esterna** (`content=`): il testo non è duplicato, l'indice punta alla tabella base. Tre trigger la mantengono sincronizzata. L'alternativa (`LIKE '%precedenza%'`) degrada linearmente su 27.737 righe; FTS5 risponde in meno di 5 ms.

### 1.3 Isolamento multi-tenant

Tre livelli, difesa in profondità:

1. **Autenticazione** — JWT HS256 firmato, access token di 30 minuti + refresh rotante di 30 giorni. Il riuso di un refresh già speso revoca l'intera famiglia di token (rilevamento di furto).
2. **Autorizzazione** — dipendenza FastAPI (`require_admin`, `require_staff`) che verifica il ruolo prima di eseguire l'handler.
3. **Scoping dei dati** — *nessuna funzione del layer dati accetta un identificativo di tenant proveniente dal corpo della richiesta.* `utente_id` e `autoscuola_id` arrivano esclusivamente dal token decodificato.

Conseguenze verificate dai test:

| Tentativo | Esito |
|---|---|
| Allievo chiama `/api/admin/panoramica` | **403** |
| Allievo legge la scheda di un altro allievo | **404** (non 403: non si rivela nemmeno l'esistenza) |
| Admin apre un allievo di un altro tenant | **404** — il filtro `autoscuola_id` è inchiodato nella `WHERE` |
| Client chiede le soluzioni durante la simulazione | Il campo `risposta_corretta` **non è nel payload** |
| Tutor AI interrogato su domanda mai affrontata | **404** |

Nota sulla quinta riga: durante una simulazione il JSON consegnato al browser non contiene la risposta corretta. Senza questa precauzione basterebbe aprire gli strumenti per sviluppatori per leggere le soluzioni — un difetto presente in diverse app concorrenti sul mercato.

---

## 2. Integrazione dell'AI Tutor

### 2.1 Flusso architetturale

```
  PWA                        Backend                    Claude API
   |                            |                            |
   |-- POST /api/tutor/spiega ->|                            |
   |   {domanda_id}             |                            |
   |                            |-- 1. AUTORIZZA             |
   |                            |   la domanda è nello       |
   |                            |   storico dell'utente?     |
   |                            |                            |
   |                            |-- 2. CACHE                 |
   |                            |   ai_spiegazioni           |
   |                            |   (domanda, risposta, ver) |
   |                            |      HIT -> ritorna        |
   |                            |                            |
   |                            |-- 3. RATE LIMIT tenant     |
   |                            |                            |
   |                            |-- 4. COSTRUISCE il prompt  |
   |                            |   system + immagine b64    |
   |                            |   + testo + risposta data  |
   |                            |--------------------------->|
   |                            |<---------------------------|
   |                            |-- 5. SALVA in cache        |
   |                            |-- 6. CONTABILIZZA i token  |
   |<-- {testo, origine} -------|                            |
```

**Perché un proxy e non una chiamata diretta dal browser.** Tre ragioni non negoziabili: la chiave API non deve mai raggiungere il client; il rate limiting e il budget devono essere applicati per tenant (l'autoscuola paga); la cache è condivisa fra tutti gli allievi di tutte le autoscuole.

### 2.2 Economia della cache

Una domanda ministeriale ha risposta binaria. Ne segue che per ogni domanda esistono **esattamente due spiegazioni possibili**: "hai risposto VERO ed era FALSO" e il caso opposto. Il tetto teorico è quindi 2 × 27.737 = 55.474 generazioni per l'intero catalogo, ma la distribuzione degli errori è fortemente concentrata: circa il 20% delle domande genera l'80% degli errori.

Stima operativa su 1.000 allievi attivi:

| | Senza cache | Con cache |
|---|---|---|
| Chiamate LLM/mese | ~120.000 | ~8.000 (primo mese), ~900 (a regime) |
| Latenza percepita | 2–8 s sempre | < 50 ms su hit (> 90% dopo 6 settimane) |

La chiave di cache include **`prompt_ver`**: cambiare il system prompt invalida in modo pulito senza cancellare lo storico, e permette di confrontare A/B due versioni del prompt sulle stesse domande.

### 2.3 Vision

Il **54%** delle domande del listato B (3.700 su 6.849) ha una figura: un segnale, un incrocio, un pannello integrativo. Senza vision il tutor spiegherebbe alla cieca una domanda che dice "Il segnale raffigurato...".

L'immagine viene inviata come blocco `image` in base64. Il file è content-addressed, quindi immutabile e cacheable a monte. Alternativa a costo ridotto, prevista dallo schema (`immagini.alt_text`): generare offline, una tantum, una descrizione testuale per ciascuna delle 637 immagini fisiche e iniettarla come testo. Costo una tantum ~637 chiamate, poi zero.

### 2.4 System prompt

Il prompt in produzione è in `backend/app/services/tutor.py` (costante `SYSTEM_PROMPT`, versione `tutor-v1.2`), ispezionabile via `GET /api/tutor/anteprima-prompt/{domanda_id}`.

```
Sei un istruttore di teoria di autoscuola italiano con vent'anni di esperienza
nella preparazione all'esame di teoria per la patente. Parli con un allievo che
ha appena sbagliato una domanda ufficiale del listato ministeriale e deve
capire il proprio errore.

## Il tuo compito
Spiegare PERCHÉ la risposta data dall'allievo è sbagliata e perché quella
corretta è corretta, fondando la spiegazione sul Codice della Strada italiano
(D.Lgs. 285/1992) e sul relativo Regolamento di esecuzione (D.P.R. 495/1992).

## Come devi rispondere
1. **Verdetto in una riga.** Apri dichiarando qual è la risposta esatta e
   sintetizza il concetto in una frase.
2. **La regola.** Enuncia la norma o il principio tecnico applicabile in
   italiano semplice. Cita l'articolo del Codice della Strada solo se sei certo
   del numero; se non lo sei, descrivi la regola senza citare (una citazione
   sbagliata è molto peggio di una citazione assente).
3. **Applicazione al caso.** Collega la regola al testo esatto della domanda.
   Se è presente un'immagine, DESCRIVI ciò che vedi (forma, colore, simboli,
   disposizione dell'incrocio, presenza di segnali o linee) e spiega quali
   elementi visivi determinano la risposta.
4. **L'inganno.** Le domande ministeriali sono costruite su trabocchetti
   ricorrenti: parole assolute ("sempre", "mai", "tutti"), confusione fra
   obbligo e facoltà, fra preavviso e prescrizione, fra divieto di sosta e
   divieto di fermata, fra massa e portata. Indica quale trappola ha fatto
   cadere l'allievo in questo caso specifico.
5. **Regola pratica.** Chiudi con una frase-memoria breve che l'allievo possa
   richiamare all'esame.

## Vincoli
- Massimo 200 parole. All'allievo serve chiarezza, non un trattato.
- Tono diretto e incoraggiante: mai umiliante, mai paternalistico.
- Rigore tecnico assoluto: se la normativa distingue casi, dillo. Non
  semplificare fino a diventare inesatto.
- Usa il "tu".
- Non inventare MAI numeri di articolo, limiti di velocità, misure, distanze o
  sanzioni di cui non sei certo.
- Se la domanda è ambigua o la formulazione ministeriale è datata, dichiaralo
  apertamente invece di arrampicarti sui vetri.
- Resta nel dominio: educazione stradale e preparazione all'esame di teoria.
  Se ti viene chiesto altro, riporta gentilmente la conversazione sulla domanda.
- Rispondi in italiano.
- Non usare emoji.

## Formato
Markdown essenziale: grassetto per i concetti chiave, al massimo un elenco
puntato. Nessun titolo di sezione, nessuna intestazione: un testo scorrevole
di tre o quattro paragrafi brevi.
```

**Messaggio utente iniettato dal backend** (template in `_template_utente`):

```
Domanda ufficiale del listato B — capitolo "Segnali di pericolo",
argomento "Attraversamento pedonale".

AFFERMAZIONE DA VALUTARE:
"Il segnale raffigurato obbliga a fermarsi per dare la precedenza ai pedoni"

Risposta corretta secondo il Ministero: FALSO
Risposta data dall'allievo: VERO — quindi SBAGLIATA.
All'affermazione è allegata l'immagine che ti invio: analizzala.

Spiega all'allievo perché ha sbagliato.
```

**Payload API completo:**

```python
{
  "model": "claude-sonnet-5",
  "max_tokens": 700,
  "temperature": 0.2,          # contenuto normativo, non creativo
  "system": [{
      "type": "text",
      "text": SYSTEM_PROMPT,
      "cache_control": {"type": "ephemeral"}   # prompt caching
  }],
  "messages": [{
      "role": "user",
      "content": [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
        {"type": "text", "text": "<messaggio utente>"}
      ]
  }]
}
```

Tre dettagli progettuali:

- **`temperature: 0.2`** — si sta spiegando una norma, non scrivendo un racconto. La variabilità è un difetto: due allievi che sbagliano la stessa domanda devono ricevere la stessa spiegazione.
- **`cache_control: ephemeral` sul system prompt** — il prompt è identico a ogni chiamata (~600 token). Il prompt caching lo rende quasi gratuito dopo la prima richiesta della finestra.
- **Immagine prima del testo** — l'ordine dei blocchi conta: il modello analizza meglio quando l'immagine precede la domanda che la riguarda.

### 2.5 Guardrail

Il tutor è esposto a minorenni (il patentino AM si prende a 14 anni). Le contromisure:

- Vincolo di dominio esplicito nel system prompt.
- Il follow-up (`/api/tutor/chat`) ricostruisce la cronologia **lato server** dalla tabella `ai_conversazioni`: il client non può iniettare messaggi arbitrari nel contesto.
- Limite di 6 turni di cronologia e 1.000 caratteri per messaggio.
- Rate limit orario per tenant (`ai_consumo`), che è insieme protezione anti-abuso e controllo di budget.
- Divieto esplicito di inventare articoli e sanzioni: nel dominio normativo un'allucinazione precisa è più dannosa di una risposta vaga.

---

## 3. Logica di business

### 3.1 Analitiche: perché non calcolare al volo

La query "tasso di errore per argomento di tutti gli allievi del tenant" scritta in modo ingenuo è:

```sql
SELECT argomento_id, COUNT(*), SUM(corretta = 0)
FROM risposte r JOIN utenti u ON u.id = r.utente_id
WHERE u.autoscuola_id = ? GROUP BY argomento_id;
```

È corretta ed è **inaccettabile**. Il costo cresce linearmente con lo storico: 500 allievi × 6 mesi × 40 risposte/giorno ≈ 3,6 milioni di righe da scandire. Una dashboard aperta 40 volte al giorno rifà 40 volte lo stesso calcolo su dati che cambiano di poche righe.

**Soluzione: aggregazione write-time.** Ogni risposta aggiorna due contatori nella *stessa transazione* della scrittura:

```python
with db.transaction() as con:
    con.execute("UPDATE risposte SET risposta_data=?, corretta=? ...")
    analytics.registra_risposta(con, ...)      # UPSERT su stat_utente_argomento
    analytics.aggiorna_difficolta_domanda(con, ...)
    srs.aggiorna_stato(con, ...)               # UPSERT su srs_stato
```

| | Costo scrittura | Costo lettura dashboard |
|---|---|---|
| Calcolo al volo | 1 INSERT | O(righe storiche) — secondi |
| Rollup write-time | 1 INSERT + 3 UPSERT (~0,05 ms) | O(#argomenti) — millisecondi |

L'invariante che rende il tutto affidabile: **o si scrive tutto, o non si scrive niente**. Non esiste uno stato in cui la risposta è registrata ma le statistiche no. Il test #13 lo verifica confrontando il rollup con un ricalcolo integrale.

Gli aggregati restano comunque **derivati**: `risposte` è l'unica fonte di verità. `POST /api/admin/manutenzione/ricostruisci-aggregati` li rigenera da zero, in modo idempotente — necessario dopo import massivi o migrazioni.

**Aggregati mantenuti:**

- `stat_utente_argomento` — conteggi, tempo, EMA dell'errore, per la mappa delle criticità e per l'SRS
- `stat_utente_giorno` — secondi in app, schede per tipo, minuti di video, per le serie temporali
- `domande.difficolta` — media mobile della probabilità d'errore su tutta la popolazione, che alimenta il ranking delle domande critiche e il riempimento delle schede per allievi senza storico

### 3.2 EMA: perché non il tasso di errore grezzo

Il rapporto `errori/risposte` è una metrica pigra. Un allievo che a gennaio sbagliava il 70% dei "segnali di precedenza" e a marzo li sbaglia al 5% avrà ancora un tasso cumulato del 40%: continuerà a ricevere schede di recupero su un argomento che ormai padroneggia. È il modo più rapido per far disinstallare l'app.

Si mantiene quindi una **media mobile esponenziale**:

```
ema ← α · errore + (1 − α) · ema        con α = 0,30
```

- reagisce in ~10 risposte, non richiede di leggere lo storico
- occupa **una sola colonna REAL**, aggiornata in O(1) nell'UPSERT
- α = 0,30 è il compromesso tra reattività e stabilità: valori più alti fanno oscillare la classifica dopo ogni singola risposta, valori più bassi la rendono inerte

Il tasso cumulato resta comunque disponibile (`n_errori/n_risposte`) e viene mostrato nel dettaglio: serve alla segreteria per la valutazione complessiva, mentre l'EMA guida l'algoritmo.

### 3.3 Ripetizione mirata

Due livelli, perché il problema ha due granularità.

**Livello domanda — SM-2 semplificato** (`services/srs.py`)

Ogni coppia (utente, domanda) ha un intervallo di ripasso che si allunga dopo ogni successo e si azzera dopo ogni errore:

```python
if corretta:
    ripetizioni += 1
    facilita = max(1.3, facilita + 0.1)
    intervallo = INTERVALLI[ripetizioni]        # [0, 1, 3, 7, 16, 35] giorni
                 if ripetizioni < 6 else
                 round(35 * facilita ** (ripetizioni - 5))
else:
    ripetizioni = 0
    facilita = max(1.3, facilita - 0.25)
    intervallo = 0                              # torna nel pool immediato
prossima_il = oggi + intervallo
```

Il fattore di facilità modula la crescita: le domande soggettivamente difficili per *quell'allievo* tornano prima. Il floor 1,3 impedisce che una domanda ostica venga riproposta all'infinito, frustrando l'utente.

Costo per risposta: **una UPSERT su chiave primaria**. La query "cosa devo ripassare oggi" è un range scan su `ix_srs_due`.

**Livello argomento — EMA** (§3.2), che identifica le aree deboli anche su domande mai viste.

**Composizione della scheda di recupero**

| Quota | Contenuto | Perché |
|---|---|---|
| 60% | Domande scadute secondo SM-2, le più sbagliate per prime | È il nucleo della ripetizione spaziata |
| 30% | Domande **mai viste** negli argomenti con EMA più alta | Impedisce che l'allievo impari a memoria le stesse 40 domande invece del concetto |
| 10% | Domande già superate, come controllo di ritenzione | Verifica che il consolidamento sia reale |

Le quote mancanti vengono compensate dalle categorie rimanenti, ordinate per `difficolta` globale: la scheda è **sempre piena**, anche per un allievo al primo accesso senza alcuno storico. È il caso limite che rompe la maggior parte delle implementazioni di spaced repetition.

*Verificato dal test #6: su 5 domande sbagliate nella simulazione, tutte e 5 sono state riproposte nella scheda di recupero successiva.*

### 3.4 Composizione della scheda d'esame

La scheda ministeriale non è un campione casuale piatto: estrae quesiti distribuiti su tutti i capitoli. Il generatore replica la proporzionalità rispetto alla numerosità del listato, garantendo almeno una domanda per capitolo finché il budget lo consente. *Verificato: 30 domande distribuite su 22 capitoli distinti su 25.*

Sul campionamento: `ORDER BY RANDOM() LIMIT 30` costringe SQLite a materializzare e ordinare l'intero set filtrato. Con l'indice di copertura il costo resta sotto i 15 ms, ma per la simulazione — che parte sotto timer e deve essere istantanea — si usa un campionamento a due fasi: scansione dell'indice per estrarre i soli `id`, scelta in Python, poi caricamento delle sole righe selezionate.

### 3.5 Tracciamento del tempo

Il tempo "davanti all'app" è la metrica che l'autoscuola usa per capire chi sta studiando davvero. Misurarla male è peggio che non misurarla: una tab dimenticata aperta per otto ore falserebbe ogni classifica.

Protocollo:

1. Sessione aperta all'ingresso (`POST /api/sessioni/apri`); una sessione recente e ancora viva viene ripresa invece di crearne una nuova.
2. Heartbeat ogni 30 s, **solo a pagina visibile** (Page Visibility API).
3. Il server accumula il delta fra due ping, ma **scarta i delta oltre la soglia di inattività** (5 minuti): se il client tace 20 minuti, quei 20 minuti non vengono conteggiati.
4. Il delta accettato è `min(tempo_reale_server, delta_dichiarato, soglia)`. Regge sia il client onesto sia quello che tenta di gonfiare le proprie ore — *verificato dal test #8: una richiesta con `delta_sec: 120` viene ridotta al tempo realmente trascorso.*
5. Un **reaper** chiude le sessioni orfane (browser chiuso, rete caduta), usando l'indice parziale `ix_sess_aperte`.
6. `navigator.sendBeacon` all'evento `pagehide` recupera l'ultimo frammento: `fetch` non sopravvive alla chiusura della pagina, `sendBeacon` sì.

Il `breakdown` JSON registra la ripartizione per sezione, così l'autoscuola distingue chi passa 3 ore sui quiz da chi le passa sui video.

### 3.6 Indice di prontezza

Un punteggio 0–100 che stima la probabilità di superare l'esame, perché "hai fatto 47 schede" non risponde alla domanda che l'allievo si pone davvero.

| Peso | Segnale | Perché quel peso |
|---|---|---|
| 45% | Esito delle ultime 5 simulazioni | È il proxy più diretto: replica le condizioni d'esame |
| 25% | Copertura del listato (domande distinte affrontate; 35% = punteggio pieno) | Previene il falso positivo di chi ripete sempre le stesse domande |
| 20% | Accuratezza recente (EMA media sugli argomenti) | Cattura il trend, non lo storico |
| 10% | Costanza (giorni attivi nelle ultime 2 settimane) | Intercetta chi ha studiato tutto in un pomeriggio |

I pesi sono un'ipotesi iniziale da calibrare: quando la piattaforma avrà raccolto qualche centinaio di esiti d'esame reali, vanno rifittati con una regressione logistica sugli esiti osservati. La struttura del dato (`schede.esito` + esito reale da inserire in anagrafica) è già predisposta.

---

## 4. Flusso UX/UI

### 4.1 Percorso dell'allievo

```
  LOGIN
    |
    v
  HOME  ─── indice di prontezza, countdown esame, punti deboli, ultime schede
    |         apertura sessione di tracking in background
    |
    ├─────────────────┬──────────────────┬─────────────────┐
    v                 v                  v                 v
 SIMULAZIONE     ESERCITAZIONE      RIPASSO MIRATO     VIDEOCORSI
 30 dom/20 min   scelta capitoli    algoritmo SRS      resume cross-device
    |                 |                  |
    └────────┬────────┴──────────────────┘
             v
        PLAYER QUIZ
        - immagine + testo, pulsanti VERO/FALSO grandi
        - mappa della scheda: stato di ogni domanda a colpo d'occhio
        - timer (rosso e pulsante sotto i 2 minuti)
        - scorciatoie tastiera V/F, frecce
        - esercitazione: correzione immediata
        - simulazione: nessun riscontro fino alla consegna
             |
             v
        CONSEGNA (manuale o allo scadere del timer)
             |
             v
        CORREZIONE
        - esito, punteggio, errori per capitolo
        - elenco delle sole domande sbagliate
        - [Perché ho sbagliato?] su ciascuna
             |
             v
        AI TUTOR (modale)
        - spiegazione in 3-4 paragrafi con analisi della figura
        - chat di approfondimento
             |
             v
        [Genera scheda di recupero su questi argomenti]  ──> chiude il ciclo
```

### 4.2 Decisioni di interazione e loro motivazione

**Correzione immediata in esercitazione, differita in simulazione.** Sono due strumenti diversi. L'esercitazione serve a imparare, e si impara nell'istante immediatamente successivo all'errore, quando la domanda è ancora in memoria di lavoro. La simulazione serve a misurare, e un riscontro immediato ne falserebbe la funzione diagnostica.

**Avanzamento automatico solo in simulazione.** In esercitazione l'allievo deve avere il tempo di leggere la correzione; passare oltre automaticamente la renderebbe invisibile.

**La mappa della scheda è sempre visibile.** Cinque stati cromatici (da rispondere / risposta / dubbio / corretta / errata). Sotto timer, sapere quante domande mancano senza doverle scorrere riduce l'ansia — che all'esame reale è il primo fattore di errore.

**Il pulsante "Segna dubbio".** Ricalca ciò che gli allievi fanno con la matita sul foglio in aula. Alla consegna si vede subito su cosa si era incerti anche quando la risposta era giusta: quella è la domanda che va rivista.

**Pulsanti VERO/FALSO larghi metà schermo.** Il target di tocco è progettato per l'uso con una sola mano su schermi piccoli, che è il contesto reale (autobus, pausa, letto).

**Ingresso al tutor AI dal punto dell'errore.** Il bottone compare nel riquadro rosso della domanda sbagliata, non in un menu. La distanza tra "ho sbagliato" e "capisco perché" deve essere un tocco.

**Countdown all'esame in home.** Se la data è nota, la home apre con "Esame fra 12 giorni". È il singolo elemento che più incide sulla costanza di studio.

**Scorciatoie da tastiera V/F e frecce.** Sul PC dell'aula si fanno centinaia di quiz al giorno; il mouse è un collo di bottiglia.

### 4.3 Percorso dell'amministratore

```
  LOGIN admin ──> DASHBOARD (rotta di default per il ruolo)
    |
    ├── KPI del tenant: allievi attivi, ore di studio, simulazioni superate, tasso di errore
    |
    ├── ELENCO ALLIEVI ordinabile per prontezza / errore / tempo / nome
    |     └── DETTAGLIO: punti deboli, capitoli, serie temporale, schede, sessioni
    |
    ├── COLLI DI BOTTIGLIA FORMATIVI
    |     argomenti in cui l'INTERA classe fatica
    |     -> è il segnale che dice quale lezione in aula va rifatta
    |
    ├── DOMANDE PIÙ SBAGLIATE con figura e tasso di errore
    |
    └── CONSUMO AI per finestra oraria (controllo di budget)
```

L'ordinamento predefinito per **prontezza crescente** è una scelta di prodotto: la prima riga che l'amministratore vede al mattino è l'allievo che rischia di essere bocciato. La dashboard è progettata per rispondere a una domanda operativa — *chi devo chiamare oggi?* — non per esibire grafici.

### 4.4 Progressive Web App

| Requisito | Implementazione |
|---|---|
| Installabile su PC/tablet/telefono | `manifest.webmanifest`, `display: standalone`, icona maskable, shortcut a "Simulazione" e "Ripasso" |
| Funziona offline | Service worker: shell in cache-first, immagini content-addressed in cache permanente, API GET in network-first con fallback |
| Sempre aggiornata | Cache versionata (`cruscotto-v1`), `skipWaiting` + `clients.claim` |
| Layout adattivo | Bootstrap 5: tab bar in basso sotto i 992 px, barra di navigazione sopra; `env(safe-area-inset-bottom)` per i notch |
| Continuità cross-device | Stato di quiz, statistiche e resume point dei video sono **server-side**: si inizia sul telefono e si finisce sul PC |
| Sessione che non si interrompe | Refresh del token trasparente: l'allievo sotto timer non vede mai comparire il login |

---

## 5. Stato dell'implementazione

### 5.1 Dati caricati

| Listato | Domande | Capitoli | Esame | Fonte |
|---|---:|---:|---|---|
| Patente B | 6.849 | 25 | 30 dom / 20 min / max 3 err | JSON + 413 immagini |
| CQC (listato 25.11.2025) | 5.416 | 2 | 60 / 90 min / max 6 | PDF MIT |
| Superiori C-D-E | 3.372 | 22 | 40 / 40 min / max 4 | PDF MIT |
| Revisione CQC | 3.039 | 26 | 60 / 90 min / max 6 | PDF |
| Revisione Superiori | 2.315 | 27 | 40 / 40 min / max 4 | PDF |
| Patentino AM | 1.992 | 10 | 30 / 20 min / max 3 | PDF MIT |
| Revisione A/B | 1.810 | 18 | 30 / 20 min / max 3 | PDF |
| Revisione AM | 1.555 | 10 | 30 / 20 min / max 3 | PDF |
| CAP | 1.389 | 401 | 30 / 30 min / max 3 | PDF |
| **Totale** | **27.737** | **541** | | **637 immagini, 8.080 domande illustrate** |

**Qualità dei dati — due limiti noti da sanare:**

1. **CAP: 401 "capitoli"** — il PDF di origine non riporta i nomi dei capitoli ministeriali, quindi il parser ha usato il tronco del quesito come titolo. Serve una tabella di mappatura manuale verso i capitoli reali del programma CAP. Finché non c'è, l'esercitazione per argomento su CAP resta poco utile (i quiz funzionano comunque).
2. **CQC: 2 "capitoli"** (`tipologia1`, `tipologia2`) — è la classificazione effettivamente presente nel PDF. Va arricchita con la suddivisione tematica del programma CQC.

Su B, AM, SUP e Revisioni la classificazione è corretta e utilizzabile così com'è.

### 5.2 Verifica

`tests/test_e2e.py` — **47 verifiche, 47 superate**. Copre: autenticazione e credenziali errate, catalogo e ricerca full-text, ciclo completo della simulazione con conteggio errori, esercitazione filtrata per capitolo, ripetizione mirata, analitiche, tracciamento del tempo con tentativo di manipolazione, RBAC e isolamento fra tenant, dashboard, videocorsi con resume point, AI Tutor e i suoi guardrail, coerenza fra rollup e ricalcolo integrale.

### 5.3 Avvio e distribuzione

L'applicazione ha tre modalita' di esecuzione, tutte verificate:

| Modalita' | Comando | Requisiti |
|---|---|---|
| Sviluppo / uso locale | doppio clic su `Avvia.bat`, `Avvia.command`, o `./avvia.sh` | Python 3.9+ |
| Eseguibile autonomo | `python3 build_exe.py`, poi doppio clic sul binario | nessuno sul PC finale |
| Produzione | `docker compose -f deploy/docker-compose.yml up -d` | Docker, dominio |

**L'avviatore (`avvia.py`) risolve i tre punti in cui l'utente non tecnico si blocca.** Se le dipendenze mancano crea `.venv`, le installa e si riavvia al suo interno; se la porta 8080 e' occupata ne cerca una libera invece di fallire con uno stack trace; e si mette in ascolto sull'intera rete locale stampando l'indirizzo raggiungibile da tablet e telefono, con un QR code da inquadrare. L'IP corretto viene determinato aprendo un socket UDP verso un indirizzo esterno senza inviare pacchetti: il sistema operativo assegna l'interfaccia di uscita, che e' quella giusta anche con piu' schede di rete o una VPN attiva.

**Modalita' congelata.** Quando gira come eseguibile, il bundle PyInstaller viene estratto in una cartella temporanea di sola lettura, cancellata alla chiusura. Scriverci il database significherebbe perdere i progressi degli allievi a ogni riavvio. Alla prima esecuzione i dati vengono quindi copiati in `dati-cruscotto/` accanto all'eseguibile — cartella stabile, ed e' quella da mettere nei backup. *Verificato compilando ed eseguendo il binario: 27.737 domande servite, immagini e frontend raggiungibili.*

PyInstaller non fa cross-compilazione: il `.exe` per Windows va prodotto compilando su Windows. `build_exe.py` dichiara esplicitamente gli import che uvicorn e FastAPI risolvono per nome a runtime, senza i quali il bundle risulterebbe incompleto e l'eseguibile fallirebbe all'avvio.

---

### 5.4 Manutenzione operativa

Gli script in `scripts/` coprono le attività ricorrenti di esercizio; `scripts/manutenzione.crontab` contiene la pianificazione pronta da installare.

| Script | Frequenza | Funzione |
|---|---|---|
| `backup.py` | notturna | Copia consistente via `VACUUM INTO`, rotazione a N copie, compressione opzionale |
| `reaper_sessioni.py` | ogni minuto | Chiude le sessioni orfane impostando `fine = ultimo_ping` |
| `ricostruisci_aggregati.py` | dopo import, o settimanale in `--verifica` | Rigenera o controlla i rollup rispetto a `risposte` |
| `pregenera_spiegazioni.py` | settimanale | Popola la cache dell'AI Tutor sulle domande più sbagliate; `--stima` mostra il preventivo |
| `gestione_utenti.py` | a richiesta | Provisioning di autoscuole e utenti, reset password |

Due dettagli che fanno la differenza in esercizio:

- **Il backup non si fa con `cp`.** Il database gira in WAL: copiare il solo file `.db` mentre l'applicazione scrive produce un backup silenziosamente incompleto, perché le transazioni recenti risiedono nel file `-wal`. `VACUUM INTO` è l'unico modo corretto, e restituisce per giunta un file già compattato (9,4 MB → 3,3 MB compresso).
- **Il reset password revoca tutte le sessioni.** Se la password viene reimpostata per un sospetto accesso abusivo, lasciare vivi i refresh token vanificherebbe l'operazione.

---

## 6. Roadmap

### Fase 1 — Consolidamento (4–6 settimane)

- Mappatura manuale dei capitoli CAP e CQC
- Pre-generazione degli `alt_text` per le 637 immagini (abbatte il costo vision)
- Recupero password via email, onboarding allievo con codice autoscuola
- Coda offline su IndexedDB: rispondere ai quiz senza rete e sincronizzare al ritorno
- CI con i test end-to-end come gate di merge
- Backup automatico del database (`VACUUM INTO` su storage esterno, orario)

### Fase 2 — Prodotto (2–3 mesi)

- Streaming SSE delle risposte del tutor (percezione di latenza ~300 ms invece di 5 s)
- Pre-generazione notturna delle spiegazioni per le 500 domande più sbagliate
- Notifiche push: "hai 23 domande da ripassare", "esame fra 3 giorni"
- Gamification misurata: serie di giorni consecutivi, obiettivi settimanali — con cautela, la pressione sociale sui minorenni va dosata
- Upload dei videocorsi dalla dashboard, transcodifica HLS, DRM leggero
- Live streaming integrato con chat moderata
- Export PDF del report allievo per il colloquio in segreteria

### Fase 3 — Scala (6+ mesi)

- Migrazione a PostgreSQL con Row Level Security (soglia: ~200 scritture/s sostenute o > 500 autoscuole)
- Estrazione del servizio AI con coda e worker dedicati
- Partizionamento di `risposte` per mese + job di rollup notturno
- Ricalibrazione dell'indice di prontezza con regressione logistica sugli esiti d'esame reali
- Analisi predittiva: allerta all'autoscuola quando un allievo mostra il profilo di chi verrà bocciato
- App native (React Native o Capacitor) riutilizzando le stesse API
- Multilingua: i PDF di revisione CQC in francese e tedesco sono già disponibili nei materiali di origine

### Debito tecnico da monitorare

| Voce | Soglia di intervento |
|---|---|
| SQLite monoprocesso | > 200 scritture/s sostenute |
| Rollup write-time | > 50 M righe in `risposte` |
| Chiamate AI sincrone | > 20 richieste concorrenti |
| Immagini servite da FastAPI | > 1.000 utenti concorrenti (passare a CDN) |
| Cache API nel service worker | Aggiungere TTL: oggi una statistica offline può risultare stantia |

---

## Appendice A — Struttura del repository

```
autoscuola-cruscotto/
├── Avvia.bat                    avvio Windows (doppio clic)
├── Avvia.command                avvio macOS (doppio clic)
├── avvia.sh                     avvio Linux
├── avvia.py                     avviatore: bootstrap, porta, LAN, QR, browser
├── build_exe.py                 compilazione dell'eseguibile autonomo
├── .env.example                 configurazione di esempio
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py              applicazione FastAPI, middleware, mount statici
│       ├── config.py            configurazione 12-factor
│       ├── db.py                connessioni, PRAGMA, transazioni
│       ├── security.py          PBKDF2, JWT HS256, hash opachi
│       ├── rbac.py              Principal, dipendenze di autorizzazione
│       ├── schema.sql           schema completo commentato (23 tabelle, 33 indici)
│       ├── routers/             auth, catalogo, quiz, statistiche,
│       │                        tutoraggio, amministrazione, sessioni, video
│       └── services/
│           ├── generatore.py    composizione delle schede
│           ├── srs.py           ripetizione mirata SM-2 + EMA
│           ├── analytics.py     rollup write-time, indice di prontezza
│           └── tutor.py         integrazione Claude, prompt, cache
├── frontend/
│   ├── index.html               shell della PWA
│   ├── app.js                   router, quiz, tutor, statistiche, admin
│   ├── styles.css               tema e componenti del quiz
│   ├── sw.js                    service worker
│   └── manifest.webmanifest
├── etl/
│   ├── parse_pdf.py             parser dei listati PDF (pdfplumber + PyMuPDF)
│   ├── merge_parts.py           fusione dei blocchi di parsing
│   └── build_db.py              costruzione e popolamento del database
├── data/
│   ├── autoscuola.db            SQLite popolato (9,6 MB)
│   ├── media/                   637 immagini content-addressed
│   └── raw/                     JSON normalizzati per listato
├── deploy/
│   ├── Dockerfile               immagine di produzione a due stadi
│   ├── docker-compose.yml       app + Caddy (HTTPS automatico) + backup
│   ├── Caddyfile                reverse proxy con TLS e politiche di cache
│   ├── nginx.conf               alternativa per chi ha gia' nginx
│   ├── cruscotto.service        unit systemd irrobustita
│   └── README.md                guida al deploy e lista pre-produzione
├── scripts/
│   ├── _comune.py               bootstrap condiviso degli script
│   ├── backup.py                backup consistente con VACUUM INTO + rotazione
│   ├── reaper_sessioni.py       chiusura delle sessioni orfane
│   ├── ricostruisci_aggregati.py  rigenerazione/verifica dei rollup
│   ├── pregenera_spiegazioni.py   popolamento della cache AI Tutor
│   ├── gestione_utenti.py       provisioning tenant/utenti, reset password
│   └── manutenzione.crontab     pianificazione pronta all'uso
├── tests/test_e2e.py            47 verifiche end-to-end
└── docs/ARCHITETTURA.md         questo documento
```

## Appendice B — API principali

| Metodo | Percorso | Ruolo | Descrizione |
|---|---|---|---|
| POST | `/api/auth/login` | pubblico | Login, restituisce access + refresh |
| POST | `/api/auth/refresh` | pubblico | Rotazione del refresh token |
| GET | `/api/catalogo/listati` | autenticato | Listati con regole d'esame e conteggi |
| GET | `/api/catalogo/capitoli?listato=B` | autenticato | Capitoli con le statistiche personali |
| GET | `/api/catalogo/cerca?q=...` | autenticato | Ricerca full-text FTS5 |
| POST | `/api/quiz/schede` | allievo | Genera esercitazione / simulazione / recupero |
| GET | `/api/quiz/schede/{id}` | proprietario | Scheda (soluzioni nascoste se in corso) |
| POST | `/api/quiz/schede/{id}/rispondi` | proprietario | Risposta + rollup + SRS, atomico |
| POST | `/api/quiz/schede/{id}/chiudi` | proprietario | Consegna e correzione completa |
| GET | `/api/quiz/da-ripassare` | allievo | Conteggio delle domande in scadenza |
| GET | `/api/statistiche/riepilogo` | allievo | Prontezza, criticità, capitoli, serie |
| POST | `/api/tutor/spiega` | allievo | Spiegazione dell'errore (cache o LLM) |
| POST | `/api/tutor/chat` | allievo | Approfondimento conversazionale |
| GET | `/api/tutor/anteprima-prompt/{id}` | autenticato | Ispezione del prompt inviato |
| GET | `/api/admin/panoramica` | staff | KPI, colli di bottiglia, domande critiche |
| GET | `/api/admin/allievi` | staff | Elenco con indice di prontezza |
| GET | `/api/admin/allievi/{id}` | staff | Dettaglio completo (solo proprio tenant) |
| POST | `/api/admin/allievi` | admin | Creazione allievo |
| POST | `/api/sessioni/apri` \| `/ping` \| `/chiudi` | autenticato | Tracciamento del tempo |
| GET | `/api/video/corsi` \| `/corsi/{id}/lezioni` | autenticato | Videocorsi e lezioni live |
| POST | `/api/video/lezioni/{id}/progresso` | autenticato | Resume point cross-device |

Documentazione interattiva completa su `/docs` (OpenAPI generato).
