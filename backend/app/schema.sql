-- =============================================================================
-- AUTOSCUOLA CRUSCOTTO - Schema relazionale (SQLite 3.35+, JSON1, WAL)
-- =============================================================================
-- Convenzioni:
--   * chiavi primarie surrogate INTEGER (rowid alias) -> join veloci e indici compatti
--   * ogni tabella di dominio ha created_at/updated_at in UTC ISO-8601
--   * i payload variabili (log, snapshot, metadati LLM) usano TEXT + CHECK(json_valid)
--     che e' l'equivalente funzionale di JSONB in Postgres
--   * ON DELETE: RESTRICT sui cataloghi ministeriali (immutabili), CASCADE sui dati utente
-- =============================================================================

PRAGMA journal_mode = WAL;          -- letture concorrenti durante le scritture
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- -----------------------------------------------------------------------------
-- 1. MULTI-TENANT, IDENTITA' E RBAC
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS autoscuole (
    id              INTEGER PRIMARY KEY,
    ragione_sociale TEXT    NOT NULL,
    slug            TEXT    NOT NULL UNIQUE,          -- sottodominio / codice accesso
    partita_iva     TEXT,
    citta           TEXT,
    piano           TEXT    NOT NULL DEFAULT 'base'
                    CHECK (piano IN ('base','pro','enterprise')),
    impostazioni    TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(impostazioni)),
    attiva          INTEGER NOT NULL DEFAULT 1 CHECK (attiva IN (0,1)),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS ruoli (
    id          INTEGER PRIMARY KEY,
    codice      TEXT NOT NULL UNIQUE CHECK (codice IN ('allievo','istruttore','admin','superadmin')),
    descrizione TEXT NOT NULL,
    permessi    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(permessi))
);

CREATE TABLE IF NOT EXISTS utenti (
    id             INTEGER PRIMARY KEY,
    autoscuola_id  INTEGER NOT NULL REFERENCES autoscuole(id) ON DELETE CASCADE,
    ruolo_id       INTEGER NOT NULL REFERENCES ruoli(id)      ON DELETE RESTRICT,
    email          TEXT    NOT NULL,
    password_hash  TEXT    NOT NULL,                 -- PBKDF2-HMAC-SHA256, 260k iter
    nome           TEXT    NOT NULL,
    cognome        TEXT    NOT NULL,
    telefono       TEXT,
    codice_fiscale TEXT,
    listato_target TEXT    NOT NULL DEFAULT 'B',     -- listato che l'allievo sta preparando
    data_esame     TEXT,                             -- per il countdown e la priorita' SRS
    attivo         INTEGER NOT NULL DEFAULT 1 CHECK (attivo IN (0,1)),
    ultimo_accesso TEXT,
    preferenze     TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(preferenze)),
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (autoscuola_id, email)                    -- stessa email ammessa su tenant diversi
);
-- Login: lookup per email globale (l'app risolve il tenant dallo slug o dall'email)
CREATE INDEX IF NOT EXISTS ix_utenti_email        ON utenti(email);
CREATE INDEX IF NOT EXISTS ix_utenti_tenant_ruolo ON utenti(autoscuola_id, ruolo_id, attivo);

CREATE TABLE IF NOT EXISTS refresh_token (
    id           INTEGER PRIMARY KEY,
    utente_id    INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    token_hash   TEXT    NOT NULL UNIQUE,            -- si salva solo l'hash, mai il token
    device_info  TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(device_info)),
    scade_il     TEXT    NOT NULL,
    revocato     INTEGER NOT NULL DEFAULT 0 CHECK (revocato IN (0,1)),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_refresh_utente ON refresh_token(utente_id, revocato);

-- -----------------------------------------------------------------------------
-- 2. CATALOGO MINISTERIALE (read-mostly, condiviso fra tutti i tenant)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS listati (
    id            INTEGER PRIMARY KEY,
    codice        TEXT NOT NULL UNIQUE,              -- B, AM, SUP, CQC, CAP, REV_AB...
    nome          TEXT NOT NULL,
    descrizione   TEXT,
    -- Regole d'esame parametrizzate: ogni patente ha durata/errori diversi.
    domande_esame INTEGER NOT NULL DEFAULT 30,
    minuti_esame  INTEGER NOT NULL DEFAULT 20,
    errori_max    INTEGER NOT NULL DEFAULT 3,
    vigente_dal   TEXT,
    attivo        INTEGER NOT NULL DEFAULT 1 CHECK (attivo IN (0,1))
);

CREATE TABLE IF NOT EXISTS capitoli (
    id          INTEGER PRIMARY KEY,
    listato_id  INTEGER NOT NULL REFERENCES listati(id) ON DELETE CASCADE,
    slug        TEXT    NOT NULL,
    titolo      TEXT    NOT NULL,
    ordine      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (listato_id, slug)
);

CREATE TABLE IF NOT EXISTS argomenti (
    id          INTEGER PRIMARY KEY,
    capitolo_id INTEGER NOT NULL REFERENCES capitoli(id) ON DELETE CASCADE,
    slug        TEXT    NOT NULL,
    titolo      TEXT    NOT NULL,
    ordine      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (capitolo_id, slug)
);
CREATE INDEX IF NOT EXISTS ix_argomenti_capitolo ON argomenti(capitolo_id);

CREATE TABLE IF NOT EXISTS immagini (
    id        INTEGER PRIMARY KEY,
    percorso  TEXT NOT NULL UNIQUE,                  -- nome file content-addressed (sha1)
    hash      TEXT NOT NULL,
    larghezza INTEGER,
    altezza   INTEGER,
    -- descrizione testuale generata una tantum dall'LLM (alt-text + contesto per il
    -- tutor quando si vuole evitare il costo della vision a ogni richiesta)
    alt_text  TEXT
);

-- Il "quesito" ministeriale e' il gruppo: un tronco comune + N affermazioni V/F.
CREATE TABLE IF NOT EXISTS quesiti (
    id             INTEGER PRIMARY KEY,
    listato_id     INTEGER NOT NULL REFERENCES listati(id) ON DELETE CASCADE,
    capitolo_id    INTEGER          REFERENCES capitoli(id) ON DELETE SET NULL,
    argomento_id   INTEGER          REFERENCES argomenti(id) ON DELETE SET NULL,
    codice_min     TEXT,                              -- "Quesito n. 5103"
    tronco         TEXT,                              -- testo comune, se presente
    immagine_id    INTEGER          REFERENCES immagini(id) ON DELETE SET NULL,
    UNIQUE (listato_id, codice_min)
);

CREATE TABLE IF NOT EXISTS domande (
    id            INTEGER PRIMARY KEY,
    listato_id    INTEGER NOT NULL REFERENCES listati(id)   ON DELETE CASCADE,
    quesito_id    INTEGER          REFERENCES quesiti(id)   ON DELETE CASCADE,
    capitolo_id   INTEGER          REFERENCES capitoli(id)  ON DELETE SET NULL,
    argomento_id  INTEGER          REFERENCES argomenti(id) ON DELETE SET NULL,
    immagine_id   INTEGER          REFERENCES immagini(id)  ON DELETE SET NULL,
    codice_min    TEXT,                                -- numero domanda ministeriale
    testo         TEXT    NOT NULL,
    risposta      INTEGER NOT NULL CHECK (risposta IN (0,1)),   -- 1=VERO 0=FALSO
    hash_testo    TEXT    NOT NULL,                    -- dedup fra listati sovrapposti
    difficolta    REAL    NOT NULL DEFAULT 0.5,        -- p(errore) globale, ricalcolata da job
    n_somministr  INTEGER NOT NULL DEFAULT 0,          -- denominatore della difficolta'
    attiva        INTEGER NOT NULL DEFAULT 1 CHECK (attiva IN (0,1)),
    UNIQUE (listato_id, hash_testo)
);
-- Indice di copertura per il generatore di schede (il 90% delle query parte da qui)
CREATE INDEX IF NOT EXISTS ix_domande_sel
    ON domande(listato_id, capitolo_id, attiva, id);
CREATE INDEX IF NOT EXISTS ix_domande_argomento ON domande(argomento_id, attiva);
CREATE INDEX IF NOT EXISTS ix_domande_quesito   ON domande(quesito_id);

-- Ricerca full-text sul testo delle domande (motore FTS5 esterno, sincronizzato
-- da trigger: evita LIKE '%...%' che degrada linearmente su 30k righe)
CREATE VIRTUAL TABLE IF NOT EXISTS domande_fts USING fts5(
    testo, content='domande', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS trg_domande_ai AFTER INSERT ON domande BEGIN
    INSERT INTO domande_fts(rowid, testo) VALUES (new.id, new.testo);
END;
CREATE TRIGGER IF NOT EXISTS trg_domande_ad AFTER DELETE ON domande BEGIN
    INSERT INTO domande_fts(domande_fts, rowid, testo) VALUES('delete', old.id, old.testo);
END;
CREATE TRIGGER IF NOT EXISTS trg_domande_au AFTER UPDATE OF testo ON domande BEGIN
    INSERT INTO domande_fts(domande_fts, rowid, testo) VALUES('delete', old.id, old.testo);
    INSERT INTO domande_fts(rowid, testo) VALUES (new.id, new.testo);
END;

-- -----------------------------------------------------------------------------
-- 3. ESERCITAZIONI: SCHEDE E RISPOSTE
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schede (
    id             INTEGER PRIMARY KEY,
    utente_id      INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    autoscuola_id  INTEGER NOT NULL REFERENCES autoscuole(id) ON DELETE CASCADE, -- denormalizzato
    listato_id     INTEGER NOT NULL REFERENCES listati(id) ON DELETE RESTRICT,
    tipo           TEXT    NOT NULL CHECK (tipo IN ('esercitazione','simulazione','recupero')),
    -- filtri applicati alla generazione: capitoli scelti, seed, parametri SRS
    parametri      TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(parametri)),
    stato          TEXT    NOT NULL DEFAULT 'in_corso'
                   CHECK (stato IN ('in_corso','completata','scaduta','annullata')),
    n_domande      INTEGER NOT NULL,
    n_errori       INTEGER NOT NULL DEFAULT 0,
    n_risposte     INTEGER NOT NULL DEFAULT 0,
    esito          INTEGER CHECK (esito IN (0,1)),   -- superata secondo errori_max
    durata_sec     INTEGER NOT NULL DEFAULT 0,
    limite_sec     INTEGER,                          -- NULL = nessun timer
    iniziata_il    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    conclusa_il    TEXT
);
-- Copre "ultime schede dell'allievo" e "schede del tenant nel periodo"
CREATE INDEX IF NOT EXISTS ix_schede_utente ON schede(utente_id, iniziata_il DESC);
CREATE INDEX IF NOT EXISTS ix_schede_tenant ON schede(autoscuola_id, iniziata_il DESC);
CREATE INDEX IF NOT EXISTS ix_schede_stato  ON schede(stato, iniziata_il);

CREATE TABLE IF NOT EXISTS risposte (
    id             INTEGER PRIMARY KEY,
    scheda_id      INTEGER NOT NULL REFERENCES schede(id)  ON DELETE CASCADE,
    domanda_id     INTEGER NOT NULL REFERENCES domande(id) ON DELETE RESTRICT,
    utente_id      INTEGER NOT NULL REFERENCES utenti(id)  ON DELETE CASCADE,  -- denormalizzato
    capitolo_id    INTEGER,        -- snapshot: il catalogo puo' cambiare, lo storico no
    argomento_id   INTEGER,
    posizione      INTEGER NOT NULL,
    risposta_data  INTEGER CHECK (risposta_data IN (0,1)),   -- NULL = non risposta
    corretta       INTEGER CHECK (corretta IN (0,1)),
    tempo_ms       INTEGER NOT NULL DEFAULT 0,
    flag_dubbio    INTEGER NOT NULL DEFAULT 0 CHECK (flag_dubbio IN (0,1)),
    risposto_il    TEXT,
    UNIQUE (scheda_id, posizione)
);
-- Indice cardine dell'analytics: error-rate per argomento di un allievo
CREATE INDEX IF NOT EXISTS ix_risposte_utente_arg
    ON risposte(utente_id, argomento_id, corretta);
CREATE INDEX IF NOT EXISTS ix_risposte_utente_cap
    ON risposte(utente_id, capitolo_id, corretta);
CREATE INDEX IF NOT EXISTS ix_risposte_domanda ON risposte(domanda_id, corretta);
CREATE INDEX IF NOT EXISTS ix_risposte_scheda  ON risposte(scheda_id, posizione);

-- -----------------------------------------------------------------------------
-- 4. AGGREGATI PRECALCOLATI (write-time rollup)
-- -----------------------------------------------------------------------------
-- Scelta architetturale: invece di ricalcolare COUNT/AVG su milioni di righe a
-- ogni apertura della dashboard, si mantengono contatori incrementali aggiornati
-- nella STESSA transazione della risposta. Costo O(1) in scrittura, O(#argomenti)
-- in lettura. Le tabelle sono ricostruibili da `risposte` (single source of truth).

CREATE TABLE IF NOT EXISTS stat_utente_argomento (
    utente_id     INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    argomento_id  INTEGER NOT NULL,
    capitolo_id   INTEGER NOT NULL,
    listato_id    INTEGER NOT NULL,
    n_risposte    INTEGER NOT NULL DEFAULT 0,
    n_errori      INTEGER NOT NULL DEFAULT 0,
    tempo_tot_ms  INTEGER NOT NULL DEFAULT 0,
    ultimo_errore TEXT,
    ultima_att    TEXT,
    -- tasso di errore mobile con decadimento esponenziale (alpha=0.3):
    -- pesa gli sbagli recenti piu' di quelli vecchi, quindi l'allievo che
    -- migliora "esce" dal recupero senza dover azzerare lo storico
    ema_errore    REAL    NOT NULL DEFAULT 0.0,
    PRIMARY KEY (utente_id, argomento_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_stat_ua_cap ON stat_utente_argomento(utente_id, capitolo_id);

CREATE TABLE IF NOT EXISTS stat_utente_giorno (
    utente_id      INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    giorno         TEXT    NOT NULL,                 -- YYYY-MM-DD (Europe/Rome)
    secondi_app    INTEGER NOT NULL DEFAULT 0,
    schede_eserc   INTEGER NOT NULL DEFAULT 0,
    schede_simul   INTEGER NOT NULL DEFAULT 0,
    schede_recup   INTEGER NOT NULL DEFAULT 0,
    simul_superate INTEGER NOT NULL DEFAULT 0,
    n_risposte     INTEGER NOT NULL DEFAULT 0,
    n_errori       INTEGER NOT NULL DEFAULT 0,
    minuti_video   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (utente_id, giorno)
) WITHOUT ROWID;

-- -----------------------------------------------------------------------------
-- 5. RIPETIZIONE MIRATA (SM-2 semplificato, stato per coppia utente-domanda)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS srs_stato (
    utente_id     INTEGER NOT NULL REFERENCES utenti(id)   ON DELETE CASCADE,
    domanda_id    INTEGER NOT NULL REFERENCES domande(id)  ON DELETE CASCADE,
    argomento_id  INTEGER,
    ripetizioni   INTEGER NOT NULL DEFAULT 0,        -- successi consecutivi
    facilita      REAL    NOT NULL DEFAULT 2.5,      -- E-Factor SM-2, floor 1.3
    intervallo_g  INTEGER NOT NULL DEFAULT 0,        -- giorni al prossimo ripasso
    prossima_il   TEXT    NOT NULL,                  -- data di scadenza (indicizzata)
    n_errori      INTEGER NOT NULL DEFAULT 0,
    ultima_il     TEXT,
    PRIMARY KEY (utente_id, domanda_id)
) WITHOUT ROWID;
-- "Cosa devo ripassare oggi": range scan su una sola colonna
CREATE INDEX IF NOT EXISTS ix_srs_due ON srs_stato(utente_id, prossima_il);

-- -----------------------------------------------------------------------------
-- 6. VIDEOCORSI E LIVE
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS corsi (
    id            INTEGER PRIMARY KEY,
    autoscuola_id INTEGER REFERENCES autoscuole(id) ON DELETE CASCADE, -- NULL = catalogo globale
    listato_id    INTEGER NOT NULL REFERENCES listati(id) ON DELETE CASCADE,
    titolo        TEXT    NOT NULL,
    descrizione   TEXT,
    copertina     TEXT,
    ordine        INTEGER NOT NULL DEFAULT 0,
    pubblicato    INTEGER NOT NULL DEFAULT 0 CHECK (pubblicato IN (0,1))
);

CREATE TABLE IF NOT EXISTS lezioni_video (
    id           INTEGER PRIMARY KEY,
    corso_id     INTEGER NOT NULL REFERENCES corsi(id)     ON DELETE CASCADE,
    capitolo_id  INTEGER          REFERENCES capitoli(id)  ON DELETE SET NULL,
    titolo       TEXT    NOT NULL,
    descrizione  TEXT,
    tipo         TEXT    NOT NULL DEFAULT 'registrata'
                 CHECK (tipo IN ('registrata','live')),
    url          TEXT,                               -- HLS .m3u8 o URL provider
    durata_sec   INTEGER NOT NULL DEFAULT 0,
    inizio_live  TEXT,                               -- solo per tipo='live'
    stato_live   TEXT CHECK (stato_live IN ('programmata','in_onda','conclusa')),
    ordine       INTEGER NOT NULL DEFAULT 0,
    materiali    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(materiali)),
    pubblicata   INTEGER NOT NULL DEFAULT 1 CHECK (pubblicata IN (0,1))
);
CREATE INDEX IF NOT EXISTS ix_lezioni_corso ON lezioni_video(corso_id, ordine);

CREATE TABLE IF NOT EXISTS visione_video (
    utente_id     INTEGER NOT NULL REFERENCES utenti(id)         ON DELETE CASCADE,
    lezione_id    INTEGER NOT NULL REFERENCES lezioni_video(id)  ON DELETE CASCADE,
    secondi_visti INTEGER NOT NULL DEFAULT 0,
    posizione_sec INTEGER NOT NULL DEFAULT 0,        -- resume point cross-device
    completata    INTEGER NOT NULL DEFAULT 0 CHECK (completata IN (0,1)),
    prima_il      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ultima_il     TEXT,
    PRIMARY KEY (utente_id, lezione_id)
) WITHOUT ROWID;

-- -----------------------------------------------------------------------------
-- 7. TRACCIAMENTO TEMPO DI UTILIZZO
-- -----------------------------------------------------------------------------
-- Il client invia un heartbeat ogni 30s con la sezione attiva. La sessione si
-- chiude per logout esplicito o per inattivita' (job di reaper > 5 min di
-- silenzio) evitando di gonfiare i tempi con tab dimenticate aperte.

CREATE TABLE IF NOT EXISTS sessioni_app (
    id             INTEGER PRIMARY KEY,
    utente_id      INTEGER NOT NULL REFERENCES utenti(id) ON DELETE CASCADE,
    autoscuola_id  INTEGER NOT NULL,
    inizio         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ultimo_ping    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    fine           TEXT,
    durata_sec     INTEGER NOT NULL DEFAULT 0,
    piattaforma    TEXT,                             -- web|android|ios|desktop
    user_agent     TEXT,
    ip_hash        TEXT,                             -- GDPR: hash, non IP in chiaro
    -- ripartizione del tempo per sezione, es. {"quiz":900,"video":320,"stat":45}
    breakdown      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(breakdown)),
    chiusa_da      TEXT CHECK (chiusa_da IN ('logout','timeout','reaper'))
);
CREATE INDEX IF NOT EXISTS ix_sess_utente ON sessioni_app(utente_id, inizio DESC);
CREATE INDEX IF NOT EXISTS ix_sess_aperte ON sessioni_app(ultimo_ping) WHERE fine IS NULL;

-- -----------------------------------------------------------------------------
-- 8. AI TUTOR
-- -----------------------------------------------------------------------------
-- Cache delle spiegazioni: la coppia (domanda, risposta data) ha solo 2 varianti
-- per domanda, quindi la cache satura in fretta e abbatte costo e latenza.

CREATE TABLE IF NOT EXISTS ai_spiegazioni (
    id            INTEGER PRIMARY KEY,
    domanda_id    INTEGER NOT NULL REFERENCES domande(id) ON DELETE CASCADE,
    risposta_data INTEGER NOT NULL CHECK (risposta_data IN (0,1)),
    testo         TEXT    NOT NULL,
    modello       TEXT    NOT NULL,
    prompt_ver    TEXT    NOT NULL,                  -- invalidazione al cambio prompt
    token_in      INTEGER NOT NULL DEFAULT 0,
    token_out     INTEGER NOT NULL DEFAULT 0,
    n_hit         INTEGER NOT NULL DEFAULT 0,
    voto_medio    REAL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (domanda_id, risposta_data, prompt_ver)
);

CREATE TABLE IF NOT EXISTS ai_conversazioni (
    id          INTEGER PRIMARY KEY,
    utente_id   INTEGER NOT NULL REFERENCES utenti(id)   ON DELETE CASCADE,
    domanda_id  INTEGER          REFERENCES domande(id)  ON DELETE SET NULL,
    scheda_id   INTEGER          REFERENCES schede(id)   ON DELETE SET NULL,
    messaggi    TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(messaggi)),
    token_tot   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_ai_conv_utente ON ai_conversazioni(utente_id, updated_at DESC);

-- Rate limiting e controllo costi per tenant (finestra oraria)
CREATE TABLE IF NOT EXISTS ai_consumo (
    autoscuola_id INTEGER NOT NULL REFERENCES autoscuole(id) ON DELETE CASCADE,
    finestra      TEXT    NOT NULL,                  -- YYYY-MM-DDTHH
    n_chiamate    INTEGER NOT NULL DEFAULT 0,
    token_in      INTEGER NOT NULL DEFAULT 0,
    token_out     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (autoscuola_id, finestra)
) WITHOUT ROWID;

-- -----------------------------------------------------------------------------
-- 9. AUDIT
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY,
    utente_id     INTEGER REFERENCES utenti(id) ON DELETE SET NULL,
    autoscuola_id INTEGER,
    azione        TEXT NOT NULL,
    entita        TEXT,
    entita_id     INTEGER,
    dettagli      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(dettagli)),
    ip_hash       TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_log(autoscuola_id, created_at DESC);

-- -----------------------------------------------------------------------------
-- 10. VISTE DI SERVIZIO
-- -----------------------------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_progresso_allievo AS
SELECT u.id                                        AS utente_id,
       u.autoscuola_id,
       u.nome || ' ' || u.cognome                  AS nominativo,
       u.email,
       u.listato_target,
       u.data_esame,
       COALESCE(SUM(g.secondi_app), 0)             AS secondi_totali,
       COALESCE(SUM(g.schede_eserc), 0)            AS schede_esercitazione,
       COALESCE(SUM(g.schede_simul), 0)            AS schede_simulazione,
       COALESCE(SUM(g.simul_superate), 0)          AS simulazioni_superate,
       COALESCE(SUM(g.n_risposte), 0)              AS risposte_totali,
       COALESCE(SUM(g.n_errori), 0)                AS errori_totali,
       CASE WHEN COALESCE(SUM(g.n_risposte),0) = 0 THEN NULL
            ELSE ROUND(100.0 * SUM(g.n_errori) / SUM(g.n_risposte), 2) END AS tasso_errore_pct,
       MAX(g.giorno)                               AS ultimo_giorno_attivo
FROM utenti u
LEFT JOIN stat_utente_giorno g ON g.utente_id = u.id
GROUP BY u.id;

CREATE VIEW IF NOT EXISTS v_criticita_argomento AS
SELECT s.utente_id,
       s.capitolo_id,
       s.argomento_id,
       c.titolo  AS capitolo,
       a.titolo  AS argomento,
       s.n_risposte,
       s.n_errori,
       ROUND(100.0 * s.n_errori / NULLIF(s.n_risposte, 0), 2) AS tasso_errore_pct,
       s.ema_errore,
       s.ultimo_errore
FROM stat_utente_argomento s
JOIN argomenti a ON a.id = s.argomento_id
JOIN capitoli  c ON c.id = s.capitolo_id;
