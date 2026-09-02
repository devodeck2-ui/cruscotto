/* =============================================================================
 * Autoscuola Cruscotto - client PWA
 *
 * Nessun framework: l'app e' un router a hash con render dichiarativo per
 * schermata. La scelta e' funzionale al vincolo di prodotto - deve girare
 * fluida su tablet economici e telefoni di fascia bassa, che sono la dotazione
 * reale delle aule di autoscuola - e mantiene il bundle sotto i 30 KB.
 *
 * Sezioni: Stato -> API -> Sessione -> Router -> Schermate -> Avvio
 * ========================================================================== */
'use strict';

/* ----------------------------- Stato globale ----------------------------- */
const S = {
  access: localStorage.getItem('ac_access') || null,
  refresh: localStorage.getItem('ac_refresh') || null,
  utente: JSON.parse(localStorage.getItem('ac_utente') || 'null'),
  corsiVideo: [],        // ultimo elenco corsi caricato, per filtrarlo per patente senza rifare la chiamata
  scheda: null,          // scheda in corso
  indice: 0,
  sessioneId: null,
  timerId: null,
  scadenza: null,
  inizioDomanda: 0,
  sezione: 'home',
  conversazioneTutor: null,
  grafici: {},
};

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Righe segnaposto durante il caricamento: non si conosce il numero reale
// di righe finche' i dati non arrivano, quindi un numero fisso basta - non
// vale la pena costruire una stima.
const scheletroRighe = (n = 5) =>
  Array.from({ length: n }, () => '<div class="scheletro mb-2" style="height:38px"></div>').join('');

// Stessa cosa ma per <table>: una <div> sciolta dentro <table> viene spostata
// fuori dal browser (regole di parsing dell'HTML), quindi qui serve <tr><td>.
const scheletroTabella = (n = 5, colonne = 4) =>
  Array.from({ length: n }, () =>
    `<tr><td colspan="${colonne}"><div class="scheletro" style="height:20px"></div></td></tr>`).join('');

const fmtTempo = (sec) => {
  sec = Math.max(0, Math.round(sec));
  const m = String(Math.floor(sec / 60)).padStart(2, '0');
  return `${m}:${String(sec % 60).padStart(2, '0')}`;
};
const fmtData = (iso) => iso ? new Date(iso).toLocaleDateString('it-IT',
  { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '-';

/* --------------------------------- API ----------------------------------- */
async function api(percorso, opzioni = {}, riprova = true) {
  const h = { 'Content-Type': 'application/json', ...(opzioni.headers || {}) };
  if (S.access) h.Authorization = 'Bearer ' + S.access;

  const res = await fetch(percorso, { ...opzioni, headers: h });

  // Access token scaduto: si tenta il refresh una sola volta, in modo
  // trasparente. L'allievo sotto timer non deve mai vedere una schermata
  // di login comparire a meta' simulazione.
  if (res.status === 401 && riprova && S.refresh) {
    const ok = await rinnovaToken();
    if (ok) return api(percorso, opzioni, false);
    esci();
    throw new Error('Sessione scaduta');
  }
  if (!res.ok) {
    let msg = 'Errore ' + res.status;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}
const get  = (p) => api(p);
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });

async function rinnovaToken() {
  try {
    const r = await fetch('/api/auth/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: S.refresh }),
    });
    if (!r.ok) return false;
    salvaSessione(await r.json());
    return true;
  } catch (e) { return false; }
}

function salvaSessione(d) {
  S.access = d.access_token; S.refresh = d.refresh_token; S.utente = d.utente;
  localStorage.setItem('ac_access', d.access_token);
  localStorage.setItem('ac_refresh', d.refresh_token);
  localStorage.setItem('ac_utente', JSON.stringify(d.utente));
}

function esci() {
  post('/api/auth/logout').catch(() => {});
  localStorage.clear();
  S.access = S.refresh = S.utente = null;
  location.hash = '';
  location.reload();
}

/* ------------------------ Tracciamento del tempo ------------------------- */
/* Heartbeat solo a scheda visibile: il tempo misurato deve corrispondere al
   tempo di studio reale, non a quello di una tab dimenticata aperta.        */
async function avviaTracking() {
  try {
    const r = await post('/api/sessioni/apri', { piattaforma: 'web' });
    S.sessioneId = r.sessione_id;
  } catch (e) { return; }

  let ultimoPing = Date.now();
  setInterval(async () => {
    if (document.hidden || !S.sessioneId) { ultimoPing = Date.now(); return; }
    const delta = Math.round((Date.now() - ultimoPing) / 1000);
    ultimoPing = Date.now();
    if (delta <= 0) return;
    try { await post('/api/sessioni/ping', { sessione_id: S.sessioneId, sezione: S.sezione, delta_sec: Math.min(delta, 120) }); }
    catch (e) {}
  }, 30000);

  addEventListener('pagehide', () => {
    if (!S.sessioneId) return;
    // sendBeacon sopravvive alla chiusura della pagina, fetch no.
    navigator.sendBeacon?.('/api/sessioni/ping', new Blob([JSON.stringify(
      { sessione_id: S.sessioneId, sezione: S.sezione, delta_sec: 5 })], { type: 'application/json' }));
  });
}

/* -------------------------------- Router --------------------------------- */
const ROTTE = {
  '/home': mostraHome,
  '/esercitazione': mostraEsercitazione,
  '/quiz': () => mostra('quiz'),
  '/correzione': () => mostra('correzione'),
  '/video': mostraVideo,
  '/statistiche': mostraStatistiche,
  '/admin': mostraAdmin,
  '/simulazione': () => avviaScheda('simulazione'),
  '/recupero': () => avviaScheda('recupero'),
};

// Il browser sa fondere due schermate meglio di qualsiasi animazione scritta
// a mano: gli si passa la modifica e ci pensa lui. Dove non e' disponibile si
// applica la stessa modifica senza effetto, e l'app funziona identica.
const FERMO = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if (document.startViewTransition && !FERMO) {
  document.documentElement.classList.add('usa-vt');
}

function mostra(nome) {
  const applica = () => {
    $$('.schermata').forEach(s => s.classList.remove('attiva'));
    $('#schermata-' + nome)?.classList.add('attiva');
    S.sezione = nome;
    $$('.tabbar a').forEach(a =>
      a.classList.toggle('attivo', a.getAttribute('href') === '#/' + nome));
    aggiornaAssistente(nome);
  };

  // Il ritorno in cima avviene PRIMA del cambio: se lo si fa dopo, la nuova
  // schermata compare gia' scorsa a meta' e sembra un salto.
  if (window.scrollY > 0) {
    window.scrollTo({ top: 0, behavior: FERMO ? 'instant' : 'smooth' });
  }

  if (document.startViewTransition && !FERMO) {
    document.startViewTransition(applica);
  } else {
    applica();
  }
}

const SOLO_ALLIEVO = ['/home', '/esercitazione', '/statistiche'];

function isStaff() {
  return !!S.utente && ['admin', 'istruttore', 'superadmin'].includes(S.utente.ruolo);
}

async function instrada() {
  if (!S.access) { $('#schermata-login').classList.add('attiva'); return; }
  const rotta = location.hash.replace('#', '') || '/home';
  const [base] = rotta.split('?');
  // L'insegnante non ha una schermata da studente: la sua "home" e' la
  // dashboard con le statistiche degli allievi, non le proprie. Le rotte
  // pensate per chi studia vengono quindi dirottate.
  const h = (isStaff() && SOLO_ALLIEVO.includes(base))
    ? mostraAdmin
    : (ROTTE[base] || ROTTE['/home']);
  try { await h(); } catch (e) { avviso(e.message); }
}

function avviso(msg, tipo = 'danger') {
  const el = document.createElement('div');
  el.className = `alert alert-${tipo} position-fixed top-0 start-50 translate-middle-x mt-2 shadow toast-entra`;
  el.style.zIndex = 2000;
  el.textContent = msg;
  document.body.appendChild(el);
  // Esce con la stessa dissolvenza morbida con cui e' entrato, invece di
  // sparire di scatto - il budget totale (4s) resta invariato.
  setTimeout(() => el.classList.add('toast-esce'), 3750);
  setTimeout(() => el.remove(), 4000);
}

/* -------------------------------- HOME ----------------------------------- */
async function mostraHome() {
  mostra('home');
  $('#lista-criticita').innerHTML = scheletroRighe(3);
  $('#tabella-storico').innerHTML = scheletroTabella(4, 4);
  const [st, storico, ripasso, miePatenti] = await Promise.all([
    get('/api/statistiche/riepilogo'), get('/api/quiz/storico?limite=8'),
    get('/api/quiz/da-ripassare'), get('/api/mie-patenti'),
  ]);
  // Chi non ha ancora nessuna patente assegnata (caso raro: account staff,
  // o allievo appena creato) vede comunque tutto il catalogo, cosi' la
  // schermata non resta vuota.
  const listati = miePatenti.listati.length ? miePatenti.listati : await get('/api/catalogo/listati');

  $('#saluto').textContent = 'Ciao ' + (S.utente.nome || '');
  const p = st.profilo || {};
  $('#sotto-saluto').textContent =
    `${p.risposte_totali || 0} risposte date - ${p.schede_simulazione || 0} simulazioni svolte`;

  const pr = st.prontezza;
  $('#kpi-prontezza').textContent = pr.punteggio;
  $('#barra-prontezza').style.width = pr.punteggio + '%';
  $('#barra-prontezza').className = 'progress-bar ' +
    (pr.punteggio >= 75 ? 'bg-success' : pr.punteggio >= 50 ? 'bg-warning' : 'bg-danger');
  $('#kpi-livello').textContent = { pronto: 'Sei pronto per l\'esame', quasi: 'Ci sei quasi', 'in formazione': 'Continua ad allenarti' }[pr.livello];
  $('#kpi-simulazioni').textContent = p.simulazioni_superate || 0;
  $('#kpi-simulazioni-tot').textContent = `su ${p.schede_simulazione || 0} svolte`;
  $('#kpi-errore').textContent = (p.tasso_errore_pct ?? '-') + '%';
  $('#kpi-ore').textContent = ((p.secondi_totali || 0) / 3600).toFixed(1);

  if (p.data_esame) {
    const gg = Math.ceil((new Date(p.data_esame) - Date.now()) / 86400000);
    $('#countdown-esame').innerHTML = gg >= 0
      ? `<span class="badge text-bg-primary fs-6">Esame fra ${gg} giorni</span>` : '';
  }

  avvisoLive();

  // La pillola in alto mostra tutte le patenti dell'allievo, non solo quella
  // principale: chi prepara sia l'AM che la B deve vederle entrambe a colpo
  // d'occhio, non scoprire le altre solo aprendo l'esercitazione.
  $('#badge-listato').textContent = listati.map(x => x.codice).join(' · ');
  $('#badge-listato').title = listati.length > 1
    ? 'Le tue patenti: ' + listati.map(x => x.nome).join(', ') : (listati[0]?.nome || '');
  const l = listati.find(x => x.codice === (S.utente.listato_target || 'B')) || listati[0];
  if (l) {
    $('#descr-simulazione').textContent =
      `${l.domande_esame} domande in ${l.minuti_esame} minuti, massimo ${l.errori_max} errori.`;
  }
  // Con piu' patenti l'allievo sceglie su quale simulare l'esame.
  if (listati.length > 1 && !$('#sel-sim-listato')) {
    const sel = document.createElement('select');
    sel.id = 'sel-sim-listato';
    sel.className = 'form-select form-select-sm mb-2';
    sel.innerHTML = listati.map(x =>
      `<option value="${x.codice}">Esame ${esc(x.codice)} - ${x.domande_esame} domande in ${x.minuti_esame} min</option>`).join('');
    $('#btn-avvia-simulazione').parentNode.insertBefore(sel, $('#btn-avvia-simulazione'));
  }

  $('#badge-ripasso').innerHTML = ripasso.n_domande
    ? `<span class="badge text-bg-warning">${ripasso.n_domande} da rivedere</span>` : '';

  $('#lista-criticita').innerHTML = st.criticita.length ? st.criticita.slice(0, 6).map((c, i) => `
    <div class="d-flex justify-content-between align-items-center py-2 border-bottom rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
      <div class="me-3">
        <div class="small fw-semibold">${esc(c.argomento)}</div>
        <div class="text-muted" style="font-size:.75rem">${esc(c.capitolo)}</div>
      </div>
      <div class="text-end" style="min-width:130px">
        <div class="barra-arg"><span style="width:${c.tasso_errore_pct}%;background:${c.tasso_errore_pct > 40 ? '#dc3545' : '#ffc107'}"></span></div>
        <div class="small text-muted mt-1">${c.tasso_errore_pct}% su ${c.n_risposte}</div>
      </div>
    </div>`).join('') : '<p class="text-muted small mb-0">Nessun dato: completa una scheda.</p>';

  schedaScuola();

  $('#tabella-storico').innerHTML = `
    <thead><tr><th>Tipo</th><th>Esito</th><th class="d-none d-md-table-cell">Errori</th><th>Data</th></tr></thead>
    <tbody>${storico.map((s, i) => `
      <tr class="rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms"><td class="text-capitalize">${esc(s.tipo)}</td>
      <td>${s.stato === 'in_corso' ? '<span class="badge text-bg-secondary">in corso</span>'
            : s.esito ? '<span class="badge text-bg-success">superata</span>'
                      : '<span class="badge text-bg-danger">non superata</span>'}</td>
      <td class="d-none d-md-table-cell">${s.n_errori}/${s.n_domande}</td>
      <td class="small text-muted">${fmtData(s.iniziata_il)}</td></tr>`).join('')}</tbody>`;
}

/* ---------------------------- ESERCITAZIONE ------------------------------ */
async function mostraEsercitazione() {
  mostra('esercitazione');
  const sel = $('#sel-listato');
  if (!sel.options.length) {
    const [tutti, mie] = await Promise.all([get('/api/catalogo/listati'), get('/api/mie-patenti')]);
    // Solo le patenti dell'allievo, non tutto il catalogo: chi prepara la B
    // non deve scorrere anche CQC e CAP per trovare la sua.
    const listati = mie.codici.length ? tutti.filter(l => mie.codici.includes(l.codice)) : tutti;
    sel.innerHTML = listati.map(l =>
      `<option value="${l.codice}">${esc(l.nome)} (${l.n_domande})</option>`).join('');
    sel.value = S.utente.listato_target || 'B';
    sel.onchange = caricaCapitoli;
  }
  await caricaCapitoli();
}

async function caricaCapitoli() {
  $('#lista-capitoli').innerHTML = scheletroRighe(4);
  const capitoli = await get('/api/catalogo/capitoli?listato=' + $('#sel-listato').value);
  $('#lista-capitoli').innerHTML = capitoli.map(c => `
    <div class="col-12 col-md-6 col-lg-4">
      <label class="card-ac p-3 h-100 d-flex gap-2 align-items-start" style="cursor:pointer">
        <input class="form-check-input mt-1 chk-capitolo" type="checkbox" value="${c.id}">
        <span class="flex-grow-1">
          <span class="d-block fw-semibold small">${esc(c.titolo)}</span>
          <span class="text-muted" style="font-size:.75rem">${c.n_domande} domande</span>
          ${c.mie_risposte ? `<span class="badge ms-1 ${c.tasso_errore_pct > 30 ? 'text-bg-danger' : 'text-bg-success'}" style="font-size:.65rem">${c.tasso_errore_pct}% errori</span>` : ''}
        </span>
      </label>
    </div>`).join('');
}

/* -------------------------------- QUIZ ----------------------------------- */
async function avviaScheda(tipo, capitoli = null, n = 30) {
  const listato = tipo === 'esercitazione'
    ? $('#sel-listato').value
    : ($('#sel-sim-listato')?.value || S.utente.listato_target || 'B');
  const r = await post('/api/quiz/schede', { tipo, listato, capitoli, n_domande: n });
  const dati = await get('/api/quiz/schede/' + r.scheda_id);
  S.scheda = { ...dati, errori_max: r.errori_max, limite_sec: r.limite_sec };
  S.indice = 0;
  mostra('quiz');
  location.hash = '#/quiz';
  $('#tipo-scheda').textContent = tipo;
  avviaTimer(r.limite_sec);
  renderDomanda();
  renderGriglia();
}

function avviaTimer(limite) {
  clearInterval(S.timerId);
  const t = $('#timer');
  if (!limite) {
    S.scadenza = null; S.inizioScheda = Date.now();
    S.timerId = setInterval(() => {
      t.textContent = fmtTempo((Date.now() - S.inizioScheda) / 1000);
    }, 1000);
    t.classList.remove('critico');
    return;
  }
  S.scadenza = Date.now() + limite * 1000;
  S.inizioScheda = Date.now();
  S.timerId = setInterval(() => {
    const restano = (S.scadenza - Date.now()) / 1000;
    t.textContent = fmtTempo(restano);
    t.classList.toggle('critico', restano <= 120);
    if (restano <= 0) { clearInterval(S.timerId); consegna('scaduta'); }
  }, 500);
}

function renderDomanda() {
  const d = S.scheda.domande[S.indice];
  S.inizioDomanda = Date.now();

  $('#posizione-scheda').textContent = `Domanda ${S.indice + 1} di ${S.scheda.domande.length}`;
  $('#barra-avanzamento').style.width =
    (100 * S.scheda.domande.filter(x => x.risposta_data !== null).length / S.scheda.domande.length) + '%';
  $('#meta-domanda').textContent = [d.capitolo, d.argomento].filter(Boolean).join(' - ');
  $('#testo-domanda').textContent = (d.tronco ? d.tronco + ' ' : '') + d.testo;

  const box = $('#contenitore-immagine');
  if (d.immagine) { $('#img-domanda').src = '/media/' + d.immagine; box.classList.remove('d-none'); }
  else box.classList.add('d-none');

  $$('.btn-vf').forEach(b => {
    const v = b.dataset.risposta === '1';
    b.classList.toggle('selezionato', d.risposta_data !== null && !!d.risposta_data === v);
  });
  $('#btn-dubbio').classList.toggle('btn-warning', !!d.flag_dubbio);
  $('#esito-immediato').innerHTML = '';

  // Nell'esercitazione la correzione e' immediata: e' li' che si impara.
  // Nella simulazione no: si replica la condizione d'esame.
  if (S.scheda.scheda.tipo !== 'simulazione' && d.risposta_data !== null && d.corretta !== null) {
    mostraEsitoImmediato(d);
  }
}

function mostraEsitoImmediato(d) {
  $('#esito-immediato').innerHTML = d.corretta
    ? `<div class="alert alert-success py-2 mb-0">Risposta corretta.</div>`
    : `<div class="alert alert-danger py-2 mb-0 d-flex justify-content-between align-items-center flex-wrap gap-2">
         <span>Sbagliato: la risposta esatta e' <strong>${d.risposta_corretta ? 'VERO' : 'FALSO'}</strong>.</span>
         <button class="btn btn-sm btn-primary" onclick="apriTutor(${d.domanda_id})">Chiedi al Tutor AI</button>
       </div>`;
}

function renderGriglia() {
  const g = $('#griglia-nav');
  g.innerHTML = S.scheda.domande.map((d, i) => {
    let cls = 'cella-nav';
    if (d.corretta === 1) cls += ' giusta';
    else if (d.corretta === 0) cls += ' errata';
    else if (d.risposta_data !== null) cls += ' risposta';
    if (d.flag_dubbio) cls += ' dubbio';
    if (i === S.indice) cls += ' corrente';
    return `<div class="${cls}" data-i="${i}" role="button" tabindex="0" aria-current="${i === S.indice ? 'true' : 'false'}">${i + 1}</div>`;
  }).join('');
  const risp = S.scheda.domande.filter(d => d.risposta_data !== null).length;
  $('#legenda-scheda').textContent =
    `${risp} risposte, ${S.scheda.domande.length - risp} da completare - massimo ${S.scheda.errori_max} errori`;
}

async function rispondi(valore) {
  const d = S.scheda.domande[S.indice];
  const tempo = Math.min(600000, Date.now() - S.inizioDomanda);
  d.risposta_data = valore ? 1 : 0;
  try {
    const r = await post(`/api/quiz/schede/${S.scheda.scheda.id}/rispondi`,
      { posizione: d.posizione, risposta: valore, tempo_ms: tempo, dubbio: !!d.flag_dubbio });
    if ('corretta' in r) { d.corretta = r.corretta ? 1 : 0; d.risposta_corretta = valore === !!r.corretta ? (valore ? 1 : 0) : (valore ? 0 : 1); mostraEsitoImmediato(d); }
  } catch (e) { avviso('Risposta non salvata: ' + e.message); }
  renderGriglia();
  $('#barra-avanzamento').style.width =
    (100 * S.scheda.domande.filter(x => x.risposta_data !== null).length / S.scheda.domande.length) + '%';

  // Avanzamento automatico solo in simulazione: nell'esercitazione l'allievo
  // deve avere il tempo di leggere la correzione.
  if (S.scheda.scheda.tipo === 'simulazione') setTimeout(() => vaiA(S.indice + 1), 180);
}

function vaiA(i) {
  if (i < 0 || i >= S.scheda.domande.length) return;
  S.indice = i; renderDomanda(); renderGriglia();
}

async function consegna(motivo = 'completata') {
  clearInterval(S.timerId);
  const durata = Math.round((Date.now() - S.inizioScheda) / 1000);
  const ris = await post(`/api/quiz/schede/${S.scheda.scheda.id}/chiudi`, { durata_sec: durata, motivo });
  renderCorrezione(ris);
  mostra('correzione');
  location.hash = '#/correzione';
}

/* ----------------------------- CORREZIONE -------------------------------- */
function renderCorrezione(r) {
  const q = r.riepilogo;
  $('#riepilogo-esito').innerHTML = `
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-3">
      <div>
        <div class="display-6 mb-0 ${q.superata ? 'text-success' : 'text-danger'}">
          ${q.superata ? 'Scheda superata' : 'Scheda non superata'}</div>
        <p class="text-muted mb-0">${q.n_errori} errori su ${q.n_domande}
          (massimo consentito ${q.errori_max}) - tempo ${fmtTempo(q.durata_sec)}</p>
      </div>
      <div class="text-end">
        <div class="h1 mb-0">${q.punteggio_pct}%</div>
        <div class="small text-muted">risposte corrette</div>
      </div>
    </div>`;

  $('#errori-capitolo').innerHTML = r.per_capitolo.map(c => `
    <div class="d-flex justify-content-between align-items-center py-1">
      <span class="small">${esc(c.capitolo || 'Non classificato')}</span>
      <span class="badge ${c.errori ? 'text-bg-danger' : 'text-bg-success'}">${c.errori}/${c.n}</span>
    </div>`).join('');

  const errate = r.domande.filter(d => d.corretta === 0 || d.risposta_data === null);
  $('#elenco-correzione').innerHTML = (errate.length ? errate : r.domande).map(d => `
    <div class="card-ac p-3 mb-2">
      <div class="d-flex gap-3">
        ${d.immagine ? `<img src="/media/${d.immagine}" alt="" style="max-width:88px;max-height:88px">` : ''}
        <div class="flex-grow-1">
          <div class="small text-muted">${esc(d.capitolo || '')}</div>
          <div class="mb-2">${esc(d.testo)}</div>
          <div class="small">
            <span class="badge ${d.corretta === 1 ? 'text-bg-success' : 'text-bg-danger'}">
              ${d.risposta_data === null ? 'Non risposta' : (d.risposta_data ? 'Hai risposto VERO' : 'Hai risposto FALSO')}
            </span>
            <span class="badge text-bg-light border">Corretta: ${d.risposta_corretta ? 'VERO' : 'FALSO'}</span>
          </div>
          ${d.corretta === 1 ? '' :
            `<button class="btn btn-sm btn-outline-primary mt-2" onclick="apriTutor(${d.domanda_id})">
               Perche' ho sbagliato?</button>`}
        </div>
      </div>
    </div>`).join('');
}

/* ------------------------------ AI TUTOR --------------------------------- */
window.apriTutor = async function (domandaId) {
  const modale = bootstrap.Modal.getOrCreateInstance($('#modale-tutor'));
  S.conversazioneTutor = null;
  S.tutorDomandaId = domandaId;

  const d = (S.scheda?.domande || []).find(x => x.domanda_id === domandaId);
  $('#tutor-domanda').innerHTML = d
    ? `<strong>${esc(d.testo)}</strong><br>Risposta corretta: ${d.risposta_corretta ? 'VERO' : 'FALSO'}` : '';
  $('#tutor-conversazione').innerHTML =
    '<div class="pannello-tutor p-3 text-muted puntini">Sto analizzando la domanda e la figura</div>';
  modale.show();

  try {
    const r = await post('/api/tutor/spiega', { domanda_id: domandaId });
    $('#tutor-conversazione').innerHTML = bollaAI(r.testo) +
      `<div class="small text-muted mt-2">Fonte: ${r.origine === 'cache' ? 'spiegazione gia' + String.fromCharCode(39) + ' verificata' : 'analisi generata ora'}</div>`;
  } catch (e) {
    $('#tutor-conversazione').innerHTML =
      `<div class="alert alert-warning">Il tutor AI non e' raggiungibile.<br><span class="small">${esc(e.message)}</span>
       <hr><p class="small mb-0">Configura la variabile d'ambiente <code>GEMINI_API_KEY</code> sul server per attivarlo.</p></div>`;
  }
};

const bollaAI = (t) => `<div class="pannello-tutor p-3 mb-2">${markdown(t)}</div>`;

/* Markdown minimale: grassetto, corsivo, codice, elenchi, paragrafi.
   Volutamente non si importa una libreria: l'input proviene dal nostro
   backend e il set di marcatori usato dal prompt e' ristretto e noto. */
function markdown(t) {
  return esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
    .split(/\n{2,}/).map(p => p.startsWith('<ul>') ? p : `<p>${p.replace(/\n/g, '<br>')}</p>`).join('');
}

$('#form-tutor').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#in-tutor');
  const testo = input.value.trim();
  if (!testo) return;
  input.value = '';
  $('#tutor-conversazione').insertAdjacentHTML('beforeend',
    `<div class="bolla-utente p-2 px-3 mb-2 ms-auto" style="max-width:80%;width:fit-content">${esc(testo)}</div>
     <div class="bolla-ai p-2 px-3 mb-2 puntini" id="attesa-tutor">Sto pensando</div>`);
  try {
    const r = await post('/api/tutor/chat', {
      domanda_id: S.tutorDomandaId, conversazione_id: S.conversazioneTutor, messaggio: testo });
    S.conversazioneTutor = r.conversazione_id;
    $('#attesa-tutor').outerHTML = `<div class="bolla-ai p-2 px-3 mb-2">${markdown(r.testo)}</div>`;
  } catch (err) {
    $('#attesa-tutor').outerHTML = `<div class="alert alert-warning py-2">${esc(err.message)}</div>`;
  }
});

/* Avviso della prossima diretta.
 *
 * Si costruisce dai dati che il client ha gia' a disposizione (corsi e
 * lezioni), senza nuovi endpoint. Vengono mostrate solo le dirette non ancora
 * concluse e non piu' vecchie di mezz'ora: una lezione iniziata da poco e'
 * ancora raggiungibile, una di ieri sarebbe solo rumore.
 */
async function avvisoLive() {
  const contenitore = $('#schermata-home');
  $('#avviso-live')?.remove();
  try {
    const corsi = await get('/api/video/corsi');
    const gruppi = await Promise.all(corsi.slice(0, 4).map(c =>
      get(`/api/video/corsi/${c.id}/lezioni`).catch(() => [])));
    const adesso = Date.now();
    const dirette = gruppi.flat()
      .filter(l => l.tipo === 'live' && l.stato_live !== 'conclusa' && l.inizio_live)
      .map(l => ({ ...l, quando: new Date(l.inizio_live).getTime() }))
      .filter(l => l.quando > adesso - 30 * 60000)
      .sort((a, b) => a.quando - b.quando);
    if (!dirette.length) return;

    const l = dirette[0];
    const minuti = Math.round((l.quando - adesso) / 60000);
    const inCorso = l.stato_live === 'in_onda' || minuti <= 0;
    const quando = new Date(l.quando).toLocaleString('it-IT',
      { weekday: 'long', day: '2-digit', month: 'long', hour: '2-digit', minute: '2-digit' });
    const fra = minuti <= 0 ? 'in corso adesso'
      : minuti < 60 ? `fra ${minuti} minuti`
      : minuti < 1440 ? `fra ${Math.round(minuti / 60)} ore`
      : `fra ${Math.round(minuti / 1440)} giorni`;

    const el = document.createElement('div');
    el.id = 'avviso-live';
    el.className = `alert ${inCorso ? 'alert-danger' : 'alert-primary'} d-flex flex-wrap `
      + 'justify-content-between align-items-center gap-2 mb-3';
    el.innerHTML =
      `<div><strong>${inCorso ? 'Lezione in diretta ora' : 'Lezione in diretta'}</strong>`
      + ` &middot; ${esc(l.titolo)}<div class="small">${quando} &mdash; ${fra}</div></div>`
      + (l.url ? `<a class="btn btn-sm ${inCorso ? 'btn-danger' : 'btn-primary'}"`
                 + ` href="${esc(l.url)}" target="_blank" rel="noopener">`
                 + `${inCorso ? 'Entra ora' : 'Apri il collegamento'}</a>` : '');
    contenitore.insertBefore(el, contenitore.firstElementChild);
  } catch (e) { /* l'avviso e' un extra: se non arriva, la home resta valida */ }
}

/* -------------------------------- VIDEO ---------------------------------- */
async function mostraVideo() {
  mostra('video');
  $('#lista-corsi').innerHTML = scheletroRighe(3);
  const [corsi, mie] = await Promise.all([
    get('/api/video/corsi'), get('/api/mie-patenti').catch(() => ({ listati: [] })),
  ]);
  S.corsiVideo = corsi;

  // Il backend restituisce gia' solo i corsi delle patenti dell'allievo: qui
  // si aggiungono dei pulsanti per guardarne una alla volta, utile a chi ne
  // prepara piu' di una insieme (es. AM e B) e non vuole scorrere tutto.
  const listati = mie.listati || [];
  const selPatenti = $('#sel-patenti-video');
  if (listati.length > 1) {
    selPatenti.innerHTML = [`<button type="button" class="btn btn-sm btn-primary" data-listato="">Tutte</button>`,
      ...listati.map(l => `<button type="button" class="btn btn-sm btn-outline-primary"
               data-listato="${esc(l.codice)}">${esc(l.codice)}</button>`)].join('');
    $$('#sel-patenti-video button').forEach(b => b.addEventListener('click', () => {
      $$('#sel-patenti-video button').forEach(x => x.classList.replace('btn-primary', 'btn-outline-primary'));
      b.classList.replace('btn-outline-primary', 'btn-primary');
      disegnaCorsi(b.dataset.listato);
    }));
  } else {
    selPatenti.innerHTML = '';
  }
  disegnaCorsi('');
}

function disegnaCorsi(listato) {
  const corsi = listato ? S.corsiVideo.filter(c => c.listato === listato) : S.corsiVideo;
  const piuDiUnaPatente = !listato && new Set(S.corsiVideo.map(c => c.listato)).size > 1;
  $('#lista-corsi').innerHTML = corsi.length ? corsi.map(c => `
    <div class="col-12 col-md-6 col-lg-4">
      <div class="card-ac p-3 h-100">
        <h3 class="h6">${esc(c.titolo)} ${piuDiUnaPatente ? `<span class="pill ms-1">${esc(c.listato)}</span>` : ''}</h3>
        <p class="small text-muted">${esc(c.descrizione || '')}</p>
        <div class="d-flex justify-content-between align-items-center">
          <span class="small text-muted">${c.completate || 0}/${c.n_lezioni} lezioni</span>
          <button class="btn btn-sm btn-primary" onclick="apriCorso(${c.id})">Apri</button>
        </div>
      </div>
    </div>`).join('') : `<p class="text-muted">Nessun videocorso pubblicato${listato ? ' per questa patente' : ''}.</p>`;
}

window.apriCorso = async function (id) {
  const lez = await get(`/api/video/corsi/${id}/lezioni`);
  $('#lista-lezioni').innerHTML = `
    <h3 class="h6 text-uppercase text-muted">Lezioni</h3>
    <div class="list-group">${lez.map(l => `
      <div class="list-group-item d-flex justify-content-between align-items-center flex-wrap gap-2">
        <div>
          <div class="fw-semibold small">${esc(l.titolo)}
            ${l.tipo === 'live' ? `<span class="badge text-bg-danger ms-1">LIVE ${esc(l.stato_live || '')}</span>` : ''}</div>
          <div class="text-muted" style="font-size:.75rem">
            ${esc(l.capitolo || '')} ${l.durata_sec ? '- ' + Math.round(l.durata_sec / 60) + ' min' : ''}
            ${l.riprendi_da ? '- riprendi da ' + fmtTempo(l.riprendi_da) : ''}</div>
        </div>
        <button class="btn btn-sm ${l.completata ? 'btn-outline-success' : 'btn-outline-primary'}"
                onclick="riproduci(${l.id}, '${esc(l.url || '')}', ${l.riprendi_da || 0})">
          ${l.completata ? 'Rivedi' : 'Guarda'}</button>
      </div>`).join('')}</div>
    <div id="riproduttore" class="mt-3"></div>`;
};

window.riproduci = function (lezioneId, url, riprendiDa) {
  const c = $('#riproduttore');
  c.innerHTML = `<div class="card-ac p-2">
      <video id="video-player" class="w-100 rounded" controls playsinline style="max-height:70vh"></video>
    </div>`;
  const v = $('#video-player');
  // HLS nativo su Safari/iOS; altrove serve hls.js, caricato solo se necessario.
  if (v.canPlayType('application/vnd.apple.mpegurl') || !url.endsWith('.m3u8')) {
    v.src = url;
  } else {
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js';
    s.onload = () => { const h = new Hls(); h.loadSource(url); h.attachMedia(v); };
    document.head.appendChild(s);
  }
  v.currentTime = riprendiDa || 0;

  let ultimo = 0;
  v.addEventListener('timeupdate', () => {
    if (v.currentTime - ultimo < 15) return;   // un ping ogni 15 s di visione
    const delta = Math.round(v.currentTime - ultimo);
    ultimo = v.currentTime;
    post(`/api/video/lezioni/${lezioneId}/progresso`, {
      posizione_sec: Math.round(v.currentTime), delta_sec: delta,
      completata: v.duration && v.currentTime / v.duration > 0.92 }).catch(() => {});
  });
};

/* ----------------------------- STATISTICHE ------------------------------- */
async function mostraStatistiche() {
  mostra('statistiche');
  $('#tabella-capitoli').innerHTML = scheletroRighe(4);
  const [st, mie] = await Promise.all([
    get('/api/statistiche/riepilogo'), get('/api/mie-patenti').catch(() => ({ listati: [] })),
  ]);
  const serie = [...st.serie].reverse();

  disegna('gr-andamento', {
    type: 'line',
    data: {
      labels: serie.map(s => s.giorno.slice(5)),
      datasets: [
        { label: 'Risposte', data: serie.map(s => s.n_risposte), borderColor: '#d97706', tension: .3, yAxisID: 'y' },
        { label: '% errore', data: serie.map(s => s.tasso_errore_pct), borderColor: '#dc3545', tension: .3, yAxisID: 'y1' },
      ],
    },
    options: { responsive: true, interaction: { mode: 'index', intersect: false },
      scales: { y: { beginAtZero: true }, y1: { position: 'right', beginAtZero: true, max: 100, grid: { drawOnChartArea: false } } } },
  });

  disegna('gr-tempo', {
    type: 'bar',
    data: {
      labels: serie.map(s => s.giorno.slice(5)),
      datasets: [{ label: 'Minuti', data: serie.map(s => Math.round(s.secondi_app / 60)), backgroundColor: '#198754' }],
    },
    options: { responsive: true, scales: { y: { beginAtZero: true } } },
  });

  disegnaTabellaCapitoli(st.capitoli);

  // Chi prepara piu' patenti insieme vede qui dei pulsanti per scegliere
  // quale controllare: la tabella sopra parte gia' con la principale.
  const listati = mie.listati || [];
  const selPatenti = $('#sel-patenti-capitoli');
  if (listati.length > 1) {
    selPatenti.innerHTML = listati.map((l, i) =>
      `<button type="button" class="btn btn-sm ${i === 0 ? 'btn-primary' : 'btn-outline-primary'}"
               data-listato="${esc(l.codice)}">${esc(l.codice)}</button>`).join('');
    $$('#sel-patenti-capitoli button').forEach(b => b.addEventListener('click', async () => {
      $$('#sel-patenti-capitoli button').forEach(x => x.classList.replace('btn-primary', 'btn-outline-primary'));
      b.classList.replace('btn-outline-primary', 'btn-primary');
      const r = await get('/api/statistiche/capitoli?listato=' + encodeURIComponent(b.dataset.listato));
      disegnaTabellaCapitoli(r.capitoli);
    }));
  } else {
    selPatenti.innerHTML = '';
  }
}

function disegnaTabellaCapitoli(capitoli) {
  $('#tabella-capitoli').innerHTML = `<div class="table-responsive"><table class="table table-sm align-middle">
    <thead><tr><th>Capitolo</th><th class="text-end">Domande</th><th class="text-end">Fatte</th><th class="text-end">Errore</th><th></th></tr></thead>
    <tbody>${capitoli.map(c => `<tr>
      <td class="small">${esc(c.titolo)}</td>
      <td class="text-end small text-muted">${c.n_domande}</td>
      <td class="text-end small">${c.n_risposte}</td>
      <td class="text-end small">${c.tasso_errore_pct ?? '-'}%</td>
      <td style="width:120px"><div class="barra-arg"><span style="width:${Math.min(100, 100 * c.n_risposte / Math.max(1, c.n_domande))}%;background:#e0261b"></span></div></td>
    </tr>`).join('') || '<tr><td colspan="5" class="text-muted small">Nessun dato.</td></tr>'}</tbody></table></div>`;
}

// Chart.js di suo scrive assi e legenda in un grigio scuro fisso: col tema
// scuro del telefono diventava quasi illeggibile sopra le card scure.
if (window.Chart && matchMedia('(prefers-color-scheme: dark)').matches) {
  Chart.defaults.color = '#94a3b8';
  Chart.defaults.borderColor = 'rgba(148, 163, 184, .25)';
}

// Easing coerente con il resto dell'app (le altre transizioni usano
// cubic-bezier(.2,.7,.3,1)); disattivata per chi ha chiesto meno movimento.
if (window.Chart) {
  Chart.defaults.animation = matchMedia('(prefers-reduced-motion: reduce)').matches
    ? false : { easing: 'easeOutQuart', duration: 500 };
}

function disegna(id, cfg) {
  S.grafici[id]?.destroy();
  S.grafici[id] = new Chart($('#' + id), cfg);
}

/* -------------------------------- ADMIN ---------------------------------- */
async function mostraAdmin() {
  if (!['admin', 'istruttore', 'superadmin'].includes(S.utente.ruolo)) { location.hash = '#/home'; return; }
  mostra('admin');
  $('#kpi-admin').innerHTML = Array.from({ length: 4 },
    () => '<div class="col-6 col-lg-3"><div class="card-ac p-3"><div class="scheletro" style="height:52px"></div></div></div>').join('');
  $('#tabella-allievi').innerHTML = scheletroTabella(5, 5);
  $('#lista-bottleneck').innerHTML = scheletroRighe(3);
  $('#lista-domande-critiche').innerHTML = scheletroRighe(3);
  const [pan, allievi] = await Promise.all([
    get('/api/admin/panoramica'), get('/api/admin/allievi?ordina=' + $('#sel-ordina-allievi').value)]);

  const t = pan.totali;
  const kpi = [['Allievi attivi', t.allievi], ['Ore di studio', t.ore],
               ['Simulazioni superate', `${t.superate}/${t.simul}`],
               ['Tasso di errore', (t.tasso_errore_pct ?? '-') + '%']];
  $('#kpi-admin').innerHTML = kpi.map(([k, v]) => `
    <div class="col-6 col-lg-3"><div class="card-ac p-3">
      <div class="text-muted small">${k}</div><div class="h3 mb-0">${v}</div></div></div>`).join('');

  $('#tabella-allievi').innerHTML = `
    <thead><tr><th>Allievo</th><th class="text-end">Pronto?</th><th class="text-end d-none d-md-table-cell">Ore</th>
    <th class="text-end">Errore</th><th></th></tr></thead>
    <tbody>${allievi.map((a, i) => `<tr class="rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
      <td><div class="small fw-semibold">${esc(a.nominativo)}</div>
          <div class="text-muted" style="font-size:.72rem">${esc(a.email)} - ${esc(a.listato_target)}</div></td>
      <td class="text-end"><span class="badge ${a.prontezza.punteggio >= 75 ? 'text-bg-success' : a.prontezza.punteggio >= 50 ? 'text-bg-warning' : 'text-bg-danger'}">${a.prontezza.punteggio}%</span></td>
      <td class="text-end small d-none d-md-table-cell">${a.ore}</td>
      <td class="text-end small">${a.tasso_errore_pct ?? '-'}%</td>
      <td class="text-end"><button class="btn btn-sm btn-light" onclick="dettaglioAllievo(${a.utente_id})">Apri</button></td>
    </tr>`).join('')}</tbody>`;

  schedaScuola('#schermata-admin');

  $('#lista-bottleneck').innerHTML = pan.colli_di_bottiglia.map((b, i) => `
    <div class="d-flex justify-content-between align-items-center py-1 border-bottom rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
      <div class="me-2"><div class="small">${esc(b.argomento)}</div>
        <div class="text-muted" style="font-size:.72rem">${b.allievi_coinvolti} allievi - ${b.n_risposte} risposte</div></div>
      <span class="badge text-bg-danger">${b.tasso_errore_pct}%</span>
    </div>`).join('') || '<p class="small text-muted mb-0">Dati insufficienti.</p>';

  $('#lista-domande-critiche').innerHTML = pan.domande_critiche.slice(0, 8).map((d, i) => `
    <div class="py-2 border-bottom d-flex gap-2 align-items-start rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
      ${d.immagine ? `<img src="/media/${d.immagine}" alt="" style="width:38px">` : ''}
      <div class="flex-grow-1"><div class="small">${esc(d.testo)}</div>
        <div class="text-muted" style="font-size:.72rem">${esc(d.capitolo || '')} - ${d.errori}/${d.somministrazioni}</div></div>
      <span class="badge text-bg-warning">${d.tasso_errore_pct}%</span>
    </div>`).join('') || '<p class="small text-muted mb-0">Dati insufficienti.</p>';
}

window.dettaglioAllievo = async function (id) {
  $('#dettaglio-allievo').innerHTML = `<div class="card-ac p-4">${scheletroRighe(4)}</div>`;
  $('#dettaglio-allievo').scrollIntoView({ behavior: 'smooth' });
  const d = await get('/api/admin/allievi/' + id);
  const serie = [...d.serie].reverse();
  const haDati = serie.some(s => s.n_risposte > 0);

  $('#dettaglio-allievo').innerHTML = `
    <div class="card-ac p-4">
      <div class="d-flex justify-content-between flex-wrap gap-2">
        <div><h3 class="h5 mb-0">${esc(d.profilo.nominativo)}</h3>
          <p class="text-muted small mb-0">${esc(d.profilo.email)} - listato ${esc(d.profilo.listato_target || '-')}
          ${d.profilo.data_esame ? '- esame il ' + d.profilo.data_esame : ''}</p></div>
        <div class="text-end"><div class="h3 mb-0">${d.prontezza.punteggio}%</div>
          <div class="small text-muted">pronto per l'esame</div></div>
      </div>
      <hr>
      ${haDati ? `
      <div class="row g-3 mb-3">
        <div class="col-12 col-lg-7">
          <h4 class="h6 text-uppercase text-muted">Andamento ultimi 60 giorni</h4>
          <canvas id="gr-andamento-allievo" height="140"></canvas>
        </div>
        <div class="col-12 col-lg-5">
          <h4 class="h6 text-uppercase text-muted">Tempo di studio</h4>
          <canvas id="gr-tempo-allievo" height="140"></canvas>
        </div>
      </div>` : `
      <p class="small text-muted">Non ha ancora risposto a nessuna domanda: niente da mostrare nei grafici.</p>`}
      <div class="row g-3 mb-3">
        <div class="col-md-6"><h4 class="h6 text-uppercase text-muted">Punti deboli</h4>
          ${d.criticita.map((c, i) => `<div class="d-flex justify-content-between small py-1 rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
            <span>${esc(c.argomento)}</span><span class="badge text-bg-danger">${c.tasso_errore_pct}%</span></div>`).join('') || '<p class="small text-muted">Nessuno.</p>'}</div>
        <div class="col-md-6"><h4 class="h6 text-uppercase text-muted">Ultime schede</h4>
          ${d.schede.slice(0, 8).map((s, i) => `<div class="d-flex justify-content-between small py-1 rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
            <span class="text-capitalize">${esc(s.tipo)}</span>
            <span>${s.n_errori}/${s.n_domande} ${s.esito ? '<span class="text-success">OK</span>' : ''}</span></div>`).join('') || '<p class="small text-muted">Nessuna.</p>'}</div>
      </div>
      <h4 class="h6 text-uppercase text-muted">Copertura per capitolo</h4>
      ${d.capitoli.length ? `
      <div class="table-responsive"><table class="table table-sm align-middle">
        <thead><tr><th>Capitolo</th><th class="text-end">Domande</th><th class="text-end">Fatte</th><th class="text-end">Errore</th><th></th></tr></thead>
        <tbody>${d.capitoli.map((c, i) => `<tr class="rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
          <td class="small">${esc(c.titolo)}</td>
          <td class="text-end small text-muted">${c.n_domande}</td>
          <td class="text-end small">${c.n_risposte}</td>
          <td class="text-end small">${c.tasso_errore_pct ?? '-'}%</td>
          <td style="width:120px"><div class="barra-arg"><span style="width:${Math.min(100, 100 * c.n_risposte / Math.max(1, c.n_domande))}%;background:#e0261b"></span></div></td>
        </tr>`).join('')}</tbody>
      </table></div>` : '<p class="small text-muted">Nessuna patente assegnata: non c\'e\' un programma da confrontare.</p>'}
    </div>`;

  if (haDati) {
    disegna('gr-andamento-allievo', {
      type: 'line',
      data: {
        labels: serie.map(s => s.giorno.slice(5)),
        datasets: [
          { label: 'Risposte', data: serie.map(s => s.n_risposte), borderColor: '#d97706', tension: .3, yAxisID: 'y' },
          { label: '% errore', data: serie.map(s => s.tasso_errore_pct), borderColor: '#dc3545', tension: .3, yAxisID: 'y1' },
        ],
      },
      options: { responsive: true, interaction: { mode: 'index', intersect: false },
        scales: { y: { beginAtZero: true }, y1: { position: 'right', beginAtZero: true, max: 100, grid: { drawOnChartArea: false } } } },
    });
    disegna('gr-tempo-allievo', {
      type: 'bar',
      data: {
        labels: serie.map(s => s.giorno.slice(5)),
        datasets: [{ label: 'Minuti', data: serie.map(s => Math.round(s.secondi_app / 60)), backgroundColor: '#198754' }],
      },
      options: { responsive: true, scales: { y: { beginAtZero: true } } },
    });
  }
};

/* --------------------------- SCHEDA AUTOSCUOLA --------------------------- */
/* Riquadro con indirizzo, contatti, orari e presentazione. Sta in fondo alla
 * home: serve, ma non deve rubare spazio ai pulsanti con cui si studia.
 */
async function schedaScuola(contenitore = '#schermata-home') {
  let s;
  try { s = await get('/api/scuola'); } catch (e) { return; }
  $('#scheda-scuola')?.remove();

  const mappa = s.mappa || ('https://www.google.com/maps/search/?api=1&query='
    + encodeURIComponent(`${s.indirizzo} ${s.citta}`));
  const el = document.createElement('div');
  el.id = 'scheda-scuola';
  el.className = 'card-ac p-4 mt-4';
  const iconaPin = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>';
  const iconaTelefono = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 '
    + '19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.9.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/></svg>';
  const iconaOrologio = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>';

  el.innerHTML = `
    <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
      <div>
        <h3 class="ss-etichetta mb-1">La tua autoscuola</h3>
        <div class="ss-nome mb-1">${esc(s.ragione_sociale || '')}</div>
      </div>
      ${isStaff() ? '<button class="btn btn-sm btn-outline-secondary" id="btn-modifica-scuola">'
                    + 'Modifica scheda</button>' : ''}
    </div>
    <p class="mb-3 mt-2">${esc(s.descrizione || '')}</p>
    <div class="row g-3 small">
      <div class="col-md-4 ss-voce">
        ${iconaPin}
        <div>
          <div class="ss-voce-titolo">Dove siamo</div>
          <div>${esc(s.indirizzo || '')}</div>
          <div>${esc(s.citta || '')}</div>
          <a href="${esc(mappa)}" target="_blank" rel="noopener">Apri la mappa</a>
        </div>
      </div>
      <div class="col-md-4 ss-voce">
        ${iconaTelefono}
        <div>
          <div class="ss-voce-titolo">Contatti</div>
          ${s.telefono ? `<div><a href="tel:${esc(s.telefono.replace(/\s/g, ''))}">${esc(s.telefono)}</a></div>` : ''}
          ${s.email ? `<div><a href="mailto:${esc(s.email)}">${esc(s.email)}</a></div>` : ''}
          ${s.sito ? `<div><a href="${esc(s.sito)}" target="_blank" rel="noopener">Sito internet</a></div>` : ''}
        </div>
      </div>
      <div class="col-md-4 ss-voce">
        ${iconaOrologio}
        <div>
          <div class="ss-voce-titolo">Orari di segreteria</div>
          <div>${esc(s.orari || '')}</div>
        </div>
      </div>
    </div>`;
  ($(contenitore) || $('#schermata-home')).appendChild(el);

  $('#btn-modifica-scuola')?.addEventListener('click', () => modificaScuola(s));
}

/* Modifica della scheda, riservata all'amministratore. Una domanda per volta:
 * sono sei campi che si toccano una volta l'anno, non vale la pena costruire
 * una finestra dedicata. */
async function modificaScuola(s) {
  const campi = [
    ['ragione_sociale', 'Nome dell\'autoscuola'],
    ['indirizzo', 'Indirizzo (via e numero)'],
    ['citta', 'Citta e provincia'],
    ['telefono', 'Telefono'],
    ['email', 'Email'],
    ['orari', 'Orari di segreteria'],
    ['descrizione', 'Breve descrizione'],
  ];
  const corpo = {};
  for (const [chiave, etichetta] of campi) {
    const valore = prompt(etichetta + ':', s[chiave] || '');
    if (valore === null) return;
    corpo[chiave] = valore.trim();
  }
  try {
    await put('/api/scuola', corpo);
    avviso('Scheda aggiornata.', 'success');
    schedaScuola();
  } catch (e) { avviso(e.message); }
}

/* --------------------------------- NOTIFICHE ------------------------------ */
/* Due canali che si completano a vicenda: lo storico in-app (campanellino,
 * sempre disponibile) e la push del browser (popup del sistema operativo
 * anche a sito chiuso, solo se il browser la supporta e l'utente da' il
 * permesso). La seconda si aggiunge alla prima, non la sostituisce: chi
 * nega il permesso o e' su iPhone prima di installare il sito continua a
 * vedere le notifiche aprendo il campanellino.
 */
function base64UrlAUint8Array(base64) {
  const riempimento = '='.repeat((4 - base64.length % 4) % 4);
  const sicura = (base64 + riempimento).replace(/-/g, '+').replace(/_/g, '/');
  const grezzo = atob(sicura);
  return Uint8Array.from([...grezzo].map(c => c.charCodeAt(0)));
}

async function caricaNotifiche() {
  try {
    const d = await get('/api/notifiche');
    const badge = $('#badge-notifiche');
    badge.textContent = d.non_lette > 9 ? '9+' : d.non_lette;
    badge.classList.toggle('d-none', d.non_lette === 0);
    $('#lista-notifiche').innerHTML = d.notifiche.length ? d.notifiche.map(n => `
      <li><a class="dropdown-item py-2 ${n.letta ? '' : 'fw-semibold'}" href="${esc(n.url || '#')}">
        <div class="small">${esc(n.titolo)}</div>
        <div class="text-muted" style="font-size:.72rem">${esc(n.corpo || '')}</div>
      </a></li>`).join('') : '<li><p class="text-muted small p-3 mb-0">Nessuna notifica.</p></li>';
  } catch (e) { /* niente campanello se non si riesce a caricare: non deve bloccare il resto */ }
}

$('#btn-notifiche')?.addEventListener('click', () => {
  // Si segnano come lette all'apertura: il pallino rosso serve ad accorgersi
  // che c'e' qualcosa di nuovo, non a tenere il conto di chi non ha ancora
  // cliccato dentro ogni singola voce.
  if (!$('#badge-notifiche').classList.contains('d-none')) {
    post('/api/notifiche/segna-lette').catch(() => {});
    $('#badge-notifiche').classList.add('d-none');
  }
});

async function iscrivitAllePush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  if (typeof Notification === 'undefined' || Notification.permission === 'denied') return;
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      if (Notification.permission !== 'granted') {
        const esito = await Notification.requestPermission();
        if (esito !== 'granted') return;
      }
      const { chiave } = await get('/api/notifiche/chiave-pubblica');
      if (!chiave) return;   // scuola senza chiavi VAPID configurate: solo storico in-app
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: base64UrlAUint8Array(chiave),
      });
    }
    const s = sub.toJSON();
    await post('/api/notifiche/iscrizione', {
      endpoint: s.endpoint, p256dh: s.keys.p256dh, auth: s.keys.auth,
      user_agent: navigator.userAgent,
    });
  } catch (e) { /* niente di grave: resta comunque lo storico in-app */ }
}

/* ---------------------- INVITO A INSTALLARE L'APP ------------------------- */
/* Il sito e' gia' installabile, ma quasi nessuno lo sa: su iPhone il
 * passaggio e' nascosto nel menu Condividi, e senza installazione li' non
 * esistono proprio le notifiche push. Da qui l'invito esplicito, con due
 * regole per non diventare fastidioso: mai se l'app e' gia' installata,
 * e una volta rifiutato non si ripresenta per due settimane.
 */
const INSTALLA_RINVIO_GIORNI = 14;
let promptInstalla = null;      // evento del browser (Android/desktop Chrome)

function appGiaInstallata() {
  return window.matchMedia('(display-mode: standalone)').matches ||
         window.navigator.standalone === true;
}

function suIphone() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent) ||
         // iPadOS recenti si dichiarano "Macintosh": si riconoscono dal touch
         (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function installaRinviato() {
  try {
    const fino = Number(localStorage.getItem('ac_installa_rinvio') || 0);
    return fino > Date.now();
  } catch (e) { return false; }
}

function rinviaInstalla() {
  try {
    localStorage.setItem('ac_installa_rinvio',
      String(Date.now() + INSTALLA_RINVIO_GIORNI * 86400000));
  } catch (e) { /* navigazione privata: pazienza, si ripresentera' */ }
  $('#banner-installa')?.classList.add('d-none');
}

window.addEventListener('beforeinstallprompt', e => {
  // Si blocca il banner automatico del browser per mostrarlo quando serve
  // a noi: dopo il login, non in mezzo alla schermata di accesso.
  e.preventDefault();
  promptInstalla = e;
});

window.addEventListener('appinstalled', () => {
  $('#banner-installa')?.classList.add('d-none');
  avviso('App installata: la trovi fra le tue app.', 'success');
});

function proponiInstallazione() {
  const banner = $('#banner-installa');
  if (!banner || appGiaInstallata() || installaRinviato()) return;
  // Da PC non ha molto senso: l'utile vero (icona e notifiche) e' sul telefono.
  const daTelefono = window.matchMedia('(max-width: 820px)').matches || suIphone();
  if (!daTelefono && !promptInstalla) return;
  if (!promptInstalla && !suIphone()) return;   // browser che non sa installare

  // Un attimo di respiro dopo il login: l'invito arriva a schermata gia'
  // caricata, non sovrapposto al primo caricamento.
  setTimeout(() => banner.classList.remove('d-none'), 2500);
}

$('#btn-installa-si')?.addEventListener('click', async () => {
  if (promptInstalla) {
    $('#banner-installa').classList.add('d-none');
    promptInstalla.prompt();
    const esito = await promptInstalla.userChoice.catch(() => null);
    promptInstalla = null;
    if (!esito || esito.outcome !== 'accepted') rinviaInstalla();
    return;
  }
  // iPhone: il pulsante non esiste, si spiegano i due passaggi.
  $('#banner-installa').classList.add('d-none');
  new bootstrap.Modal($('#modal-installa-ios')).show();
});

$('#btn-installa-no')?.addEventListener('click', rinviaInstalla);
$('#btn-installa-dopo')?.addEventListener('click', rinviaInstalla);

/* ------------------------- ASSISTENTE FLUTTUANTE -------------------------- */
/* Pulsante in basso a destra con la finestrella di chat. Compare ovunque
 * tranne che durante una scheda: mentre si risponde alle domande si e' soli,
 * come all'esame. Sugli errori resta il tutor dedicato, che invece vede la
 * domanda ministeriale e la figura.
 */
const ASS = { aperto: false, conversazione: null, occupato: false };

async function montaAssistente() {
  if ($('#ass-fab')) return;
  // Se l'autoscuola non ha ancora configurato una chiave AI, meglio non
  // mostrare affatto il pulsante: cliccarlo fallirebbe sempre, e sembrerebbe
  // un pezzo di app rotto invece che una funzione non ancora attivata.
  try {
    const stato = await get('/api/assistente/stato');
    if (!stato.disponibile) return;
  } catch (e) { return; }

  const stile = document.createElement('style');
  stile.textContent = `
    #ass-fab { position:fixed; right:18px; bottom:86px; z-index:1050; width:54px; height:54px;
      border-radius:50%; border:none; background:#6f42c1; color:#fff; font-size:1.5rem;
      box-shadow:0 6px 18px rgba(15,23,42,.28); cursor:pointer; }
    #ass-fab:hover { filter:brightness(1.08); }
    #ass-pannello { position:fixed; right:18px; bottom:150px; z-index:1050; width:340px;
      max-width:calc(100vw - 36px); background:var(--ac-superficie, #fff);
      border:1px solid var(--ac-bordo, #e2e8f0); color:var(--ac-testo, #0f172a);
      border-radius:14px; box-shadow:0 12px 34px rgba(15,23,42,.22); display:none;
      flex-direction:column; overflow:hidden; }
    #ass-pannello.aperto { display:flex; }
    #ass-testata { background:#6f42c1; color:#fff; padding:10px 14px; font-weight:600;
      display:flex; justify-content:space-between; align-items:center; }
    #ass-corpo { padding:12px; max-height:46vh; overflow:auto;
      background:var(--ac-fondo, #f8fafc); color:var(--ac-testo, #0f172a); }
    #ass-corpo .msg { margin-bottom:10px; font-size:.86rem; line-height:1.45; }
    #ass-corpo .io { background:var(--ac-primario, #e0261b); color:#fff;
      border-radius:12px 12px 4px 12px;
      padding:8px 11px; margin-left:auto; width:fit-content; max-width:85%; }
    #ass-corpo .lui { background:var(--ac-superficie, #fff);
      color:var(--ac-testo, #0f172a); border:1px solid var(--ac-bordo, #e2e8f0);
      border-radius:12px 12px 12px 4px; padding:8px 11px; max-width:92%; }
    #ass-piede { display:flex; gap:6px; padding:10px; border-top:1px solid #e2e8f0; }
    @media (min-width:992px){ #ass-fab { bottom:24px; } #ass-pannello { bottom:88px; } }
  `;
  document.head.appendChild(stile);

  const fab = document.createElement('button');
  fab.id = 'ass-fab';
  fab.title = 'Chiedi all\'assistente';
  fab.innerHTML = '&#128172;';
  document.body.appendChild(fab);

  const pannello = document.createElement('div');
  pannello.id = 'ass-pannello';
  pannello.innerHTML = `
    <div id="ass-testata">
      <span>Assistente</span>
      <button class="btn btn-sm btn-link text-white p-0" id="ass-chiudi">&times;</button>
    </div>
    <div id="ass-corpo"></div>
    <form id="ass-piede">
      <input class="form-control form-control-sm" id="ass-input" maxlength="800"
             placeholder="Scrivi la tua domanda...">
      <button class="btn btn-sm btn-primary" type="submit">Invia</button>
    </form>`;
  document.body.appendChild(pannello);

  fab.addEventListener('click', () => {
    ASS.aperto = !ASS.aperto;
    pannello.classList.toggle('aperto', ASS.aperto);
    if (ASS.aperto) {
      if (!$('#ass-corpo').children.length) {
        bolla('lui', 'Sono il tuo assistente virtuale. Qui potrai farmi tutte '
          + 'le domande inerenti all\'autoscuola e alle patenti.');
      }
      $('#ass-input').focus();
    }
  });
  $('#ass-chiudi').addEventListener('click', () => {
    ASS.aperto = false;
    pannello.classList.remove('aperto');
  });
  $('#ass-piede').addEventListener('submit', inviaAssistente);
}

function bolla(chi, testo) {
  const d = document.createElement('div');
  d.className = 'msg ' + (chi === 'io' ? 'io' : 'lui');
  d.innerHTML = chi === 'io' ? esc(testo) : markdown(testo);
  $('#ass-corpo').appendChild(d);
  $('#ass-corpo').scrollTop = $('#ass-corpo').scrollHeight;
  return d;
}

async function inviaAssistente(e) {
  e.preventDefault();
  if (ASS.occupato) return;
  const input = $('#ass-input');
  const testo = input.value.trim();
  if (testo.length < 2) return;
  input.value = '';
  bolla('io', testo);
  const attesa = bolla('lui', 'Sto pensando');
  attesa.classList.add('puntini');
  ASS.occupato = true;
  try {
    const r = await post('/api/assistente/chiedi',
      { messaggio: testo, conversazione_id: ASS.conversazione });
    ASS.conversazione = r.conversazione_id;
    attesa.classList.remove('puntini');
    attesa.innerHTML = markdown(r.testo);
  } catch (err) {
    attesa.classList.remove('puntini');
    attesa.innerHTML = '<span class="text-danger">' + esc(err.message) + '</span>';
  } finally {
    ASS.occupato = false;
    $('#ass-corpo').scrollTop = $('#ass-corpo').scrollHeight;
  }
}

/* Durante il quiz il pulsante sparisce del tutto. */
function aggiornaAssistente(schermata) {
  const fab = $('#ass-fab'), pannello = $('#ass-pannello');
  if (!fab) return;
  const nascondi = schermata === 'quiz';
  fab.style.display = nascondi ? 'none' : 'block';
  if (nascondi && pannello) {
    pannello.classList.remove('aperto');
    ASS.aperto = false;
  }
}

/* --------------------------------- Avvio --------------------------------- */
$('#form-login').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#btn-login');
  btn.disabled = true; btn.textContent = 'Accesso in corso...';
  try {
    const d = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('#in-email').value.trim(), password: $('#in-password').value }),
    }, false);
    salvaSessione(d);
    location.reload();
  } catch (err) {
    $('#errore-login').textContent = err.message;
    $('#errore-login').classList.remove('d-none');
  } finally { btn.disabled = false; btn.textContent = 'Entra'; }
});

$('#btn-logout').addEventListener('click', esci);
$('#btn-avvia-simulazione').addEventListener('click', () => avviaScheda('simulazione').catch(e => avviso(e.message)));
$('#btn-avvia-recupero').addEventListener('click', () => avviaScheda('recupero').catch(e => avviso(e.message)));
$('#btn-recupero-da-esito').addEventListener('click', () => avviaScheda('recupero').catch(e => avviso(e.message)));
$('#btn-seleziona-tutti').addEventListener('click', () => {
  const chk = $$('.chk-capitolo');
  const tutti = chk.every(c => c.checked);
  chk.forEach(c => c.checked = !tutti);
});
$('#btn-avvia-esercitazione').addEventListener('click', () => {
  const cap = $$('.chk-capitolo').filter(c => c.checked).map(c => +c.value);
  avviaScheda('esercitazione', cap.length ? cap : null, +$('#sel-numero').value).catch(e => avviso(e.message));
});
$('#sel-ordina-allievi')?.addEventListener('change', mostraAdmin);
$('#btn-spiega-prontezza')?.addEventListener('click', () => {
  $('#spiega-prontezza').classList.toggle('d-none');
});
$$('.btn-vf').forEach(b => b.addEventListener('click', () => rispondi(b.dataset.risposta === '1')));
$('#btn-precedente').addEventListener('click', () => vaiA(S.indice - 1));
$('#btn-successiva').addEventListener('click', () => vaiA(S.indice + 1));
$('#btn-dubbio').addEventListener('click', () => {
  const d = S.scheda.domande[S.indice];
  d.flag_dubbio = !d.flag_dubbio;
  post(`/api/quiz/schede/${S.scheda.scheda.id}/rispondi`,
    { posizione: d.posizione, risposta: d.risposta_data === null ? null : !!d.risposta_data, dubbio: d.flag_dubbio }).catch(() => {});
  renderDomanda(); renderGriglia();
});
$('#btn-consegna').addEventListener('click', () => {
  const mancanti = S.scheda.domande.filter(d => d.risposta_data === null).length;
  if (mancanti && !confirm(`Hai ${mancanti} domande senza risposta: verranno contate come errori. Consegnare?`)) return;
  consegna().catch(e => avviso(e.message));
});
$('#griglia-nav').addEventListener('click', (e) => {
  const c = e.target.closest('.cella-nav'); if (c) vaiA(+c.dataset.i);
});
// Anche da tastiera: chi naviga con Tab deve poter saltare a una domanda
// della griglia, non solo cliccarla col mouse.
$('#griglia-nav').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const c = e.target.closest('.cella-nav'); if (!c) return;
  e.preventDefault();
  vaiA(+c.dataset.i);
});

// Scorciatoie da tastiera: sul PC dell'aula si fanno centinaia di quiz al giorno.
addEventListener('keydown', (e) => {
  if (!S.scheda || !$('#schermata-quiz').classList.contains('attiva')) return;
  if (['v', 'V', '1'].includes(e.key)) rispondi(true);
  if (['f', 'F', '0'].includes(e.key)) rispondi(false);
  if (e.key === 'ArrowRight') vaiA(S.indice + 1);
  if (e.key === 'ArrowLeft') vaiA(S.indice - 1);
});

addEventListener('hashchange', instrada);

// Cliccare la voce di menu su cui ci si trova gia' non cambia l'indirizzo,
// quindi il browser non emette 'hashchange' e sembra che il pulsante non
// funzioni. Qui si intercetta il clic e si ridisegna comunque la schermata:
// diventa anche il modo naturale per aggiornare i dati a video.
document.addEventListener('click', (e) => {
  const voce = e.target.closest('a[href^="#/"]');
  if (voce && voce.getAttribute('href') === location.hash) instrada();
});
addEventListener('online',  () => document.body.classList.remove('offline'));
addEventListener('offline', () => document.body.classList.add('offline'));

(async function avvio() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  if (!S.access) { $('#schermata-login').classList.add('attiva'); return; }

  $('#schermata-login').classList.remove('attiva');
  $('#app-shell').classList.remove('d-none');
  $('#btn-utente').textContent = S.utente.nome || 'Utente';
  $('#voce-tenant').textContent = S.utente.ragione_sociale || '';
  if (['admin', 'istruttore', 'superadmin'].includes(S.utente.ruolo)) {
    // L'amministratore e' l'insegnante, non un allievo: le voci pensate per
    // chi studia (esercitazioni, statistiche personali) vengono nascoste.
    $$('#nav-desktop a[href="#/esercitazione"], #nav-desktop a[href="#/statistiche"],'
      + '.tabbar a[href="#/esercitazione"], .tabbar a[href="#/statistiche"],'
      + '.dropdown-menu a[href="#/statistiche"]')
      .forEach(a => a.classList.add('d-none'));
    // "Home" per l'insegnante diventa la panoramica dell'autoscuola (KPI,
    // allievi, argomenti critici): prima "Panoramica" restava sulla
    // schermata pensata per chi studia, e un secondo pulsante "Dashboard"
    // a parte portava dove serviva davvero - due voci per lo stesso posto,
    // una delle quali sbagliata. Ora c'e' una sola voce, corretta.
    $$('#nav-desktop a[href="#/home"], .tabbar a[href="#/home"]').forEach(a => {
      a.setAttribute('href', '#/admin');
      if (a.querySelector('span')) a.lastChild.textContent = 'Panoramica';
      else a.textContent = 'Panoramica';
    });
    // Per l'insegnante "Videocorsi" vuol dire gestirli, non guardarli:
    // il link porta dritto alla sezione di caricamento e delle dirette.
    $$('#nav-desktop a[href="#/video"], .tabbar a[href="#/video"]')
      .forEach(a => a.setAttribute('href', '/app/gestione.html?v=3#video'));
    // Il vecchio pulsante "Dashboard" (nav desktop) e la voce "Gestione"
    // della tabbar mobile diventano l'unico link alla vera gestione
    // (orario, presenze, allievi, video, analisi) - non se ne crea uno
    // nuovo apposta, cosi' non restano doppioni nascosti nella pagina.
    // Il ?v= forza il browser a scaricare la pagina aggiornata invece di
    // tenersi quella vecchia salvata in cache (stesso problema del logo).
    const linkAdmin = $('#link-admin');
    linkAdmin.setAttribute('href', '/app/gestione.html?v=3');
    linkAdmin.textContent = 'Gestione';
    linkAdmin.classList.remove('d-none');
    const tabAdmin = $('#tab-admin');
    tabAdmin.setAttribute('href', '/app/gestione.html?v=3');
    tabAdmin.innerHTML = '<span>&#9881;</span>Gestione';
    tabAdmin.classList.remove('d-none');
    if (!location.hash) location.hash = '#/admin';
  }
  avviaTracking();
  montaAssistente();
  caricaNotifiche();
  iscrivitAllePush();
  proponiInstallazione();
  instrada();
})();
