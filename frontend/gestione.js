/* =============================================================================
 * Gestione autoscuola - orario, presenze, anagrafica, videocorsi, analisi.
 *
 * Pagina autonoma: usa il token gia' salvato dall'app principale, cosi' non
 * serve un secondo accesso. Se il token e' scaduto tenta il rinnovo, e solo
 * se fallisce rimanda al login.
 * ========================================================================== */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
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

const GIORNI = ['lunedi', 'martedi', 'mercoledi', 'giovedi', 'venerdi', 'sabato', 'domenica'];
const S = { utente: null, listati: [], slot: null, lezione: null, allievo: null, grafici: {} };

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
  const el = $('#' + id);
  if (!el) return;
  S.grafici[id]?.destroy();
  S.grafici[id] = new Chart(el, cfg);
}

/* --------------------------------- API ----------------------------------- */
async function api(percorso, opzioni = {}, riprova = true) {
  const h = { ...(opzioni.headers || {}) };
  if (!(opzioni.body instanceof Blob) && !(opzioni.body instanceof File)) {
    h['Content-Type'] = 'application/json';
  }
  const tok = localStorage.getItem('ac_access');
  if (tok) h.Authorization = 'Bearer ' + tok;

  const res = await fetch(percorso, { ...opzioni, headers: h });
  if (res.status === 401 && riprova) {
    if (await rinnova()) return api(percorso, opzioni, false);
    avviso('Sessione scaduta: rientra dall\'app principale.', 'warning');
    setTimeout(() => location.href = '/', 1500);
    throw new Error('Sessione scaduta');
  }
  if (!res.ok) {
    let msg = 'Errore ' + res.status;
    try {
      const d = (await res.json()).detail;
      // Un errore di validazione (422) porta una LISTA di oggetti, non una
      // frase: passata cosi' com'e' a new Error() diventava "[object Object]"
      // e nascondeva il motivo vero del rifiuto.
      if (typeof d === 'string') msg = d;
      else if (Array.isArray(d)) msg = d.map(x => x && x.msg ? x.msg : JSON.stringify(x)).join('; ');
      else if (d) msg = JSON.stringify(d);
    } catch (e) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}
const get = (p) => api(p);
const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b || {}) });
const put = (p, b) => api(p, { method: 'PUT', body: JSON.stringify(b || {}) });
const del = (p) => api(p, { method: 'DELETE' });

async function rinnova() {
  const r = localStorage.getItem('ac_refresh');
  if (!r) return false;
  try {
    const res = await fetch('/api/auth/refresh', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: r }),
    });
    if (!res.ok) return false;
    const d = await res.json();
    localStorage.setItem('ac_access', d.access_token);
    localStorage.setItem('ac_refresh', d.refresh_token);
    return true;
  } catch (e) { return false; }
}

function avviso(testo, tipo = 'danger') {
  const el = document.createElement('div');
  el.className = `alert alert-${tipo} alert-dismissible avviso-entra`;
  el.innerHTML = esc(testo) + '<button class="btn-close" data-bs-dismiss="alert"></button>';
  $('#avvisi').appendChild(el);
  setTimeout(() => el.classList.add('avviso-esce'), 5750);
  setTimeout(() => el.remove(), 6000);
}

const fmtData = (d) => new Date(d + 'T00:00').toLocaleDateString('it-IT',
  { weekday: 'short', day: '2-digit', month: 'short' });

/* -------------------------------- Menu ----------------------------------- */
$$('#menu button').forEach(b => b.addEventListener('click', () => {
  $$('#menu button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  $$('.sezione').forEach(s => s.classList.remove('attiva'));
  $('#sez-' + b.dataset.sez).classList.add('attiva');
  ({ orario: caricaOrario, presenze: caricaLezioni, allievi: caricaAllievi,
     classi: caricaClassi, video: caricaVideo, analisi: caricaAnalisi })[b.dataset.sez]();
}));

/* ------------------------------- ORARIO ---------------------------------- */
async function caricaOrario() {
  $('#griglia-orario').innerHTML = scheletroRighe(3);
  const d = await get('/api/gestione/orario');
  $('#griglia-orario').innerHTML = d.settimana.map(g => `
    <div class="col-giorno">
      <div class="fw-semibold small text-capitalize mb-2">${g.nome}</div>
      ${g.slot.map(s => `
        <div class="fascia ${s.listato.startsWith('REV') ? 'rev' : ''} ${s.attivo ? '' : 'spenta'}"
             data-id="${s.id}">
          <div class="fw-semibold">${s.ora_inizio}-${s.ora_fine}</div>
          <div>${esc(s.listato)}${s.aula ? ' &middot; ' + esc(s.aula) : ''}</div>
          ${s.docente ? `<div class="text-muted">${esc(s.docente)}</div>` : ''}
        </div>`).join('') || '<div class="text-muted small">-</div>'}
    </div>`).join('');

  $$('#griglia-orario .fascia').forEach(el => el.addEventListener('click', async () => {
    const tutte = (await get('/api/gestione/orario')).settimana.flatMap(g => g.slot);
    apriFascia(tutte.find(s => s.id === +el.dataset.id));
  }));
}

function apriFascia(slot) {
  S.slot = slot || null;
  $('#f-giorno').innerHTML = GIORNI.map((g, i) =>
    `<option value="${i}">${g}</option>`).join('');
  $('#f-listato').innerHTML = S.listati.map(l =>
    `<option value="${l.codice}">${esc(l.codice)} - ${esc(l.nome)}</option>`).join('');
  $('#f-giorno').value = slot ? slot.giorno : 0;
  $('#f-inizio').value = slot ? slot.ora_inizio : '09:00';
  $('#f-fine').value = slot ? slot.ora_fine : '10:00';
  $('#f-listato').value = slot ? slot.listato : 'B';
  $('#f-aula').value = slot?.aula || '';
  $('#f-docente').value = slot?.docente || '';
  $('#f-note').value = slot?.note || '';
  $('#btn-elimina-fascia').classList.toggle('d-none', !slot);
  bootstrap.Modal.getOrCreateInstance($('#m-fascia')).show();
}

$('#btn-nuova-fascia').addEventListener('click', () => apriFascia(null));

$('#btn-salva-fascia').addEventListener('click', async () => {
  const corpo = {
    giorno: +$('#f-giorno').value, ora_inizio: $('#f-inizio').value,
    ora_fine: $('#f-fine').value, listato: $('#f-listato').value,
    aula: $('#f-aula').value || null, docente: $('#f-docente').value || null,
    note: $('#f-note').value || null, attivo: true,
  };
  try {
    if (S.slot) await put('/api/gestione/orario/' + S.slot.id, corpo);
    else await post('/api/gestione/orario', corpo);
    bootstrap.Modal.getInstance($('#m-fascia')).hide();
    caricaOrario();
    avviso('Orario aggiornato.', 'success');
  } catch (e) { avviso(e.message); }
});

$('#btn-elimina-fascia').addEventListener('click', async () => {
  if (!S.slot || !confirm('Togliere questa fascia dall\'orario? Le lezioni gia\' svolte restano.')) return;
  await del('/api/gestione/orario/' + S.slot.id);
  bootstrap.Modal.getInstance($('#m-fascia')).hide();
  caricaOrario();
});

/* ---------------------------- LEZIONI E PRESENZE -------------------------- */
async function caricaLezioni() {
  if (!$('#lez-dal').value) {
    const d = new Date(); d.setDate(d.getDate() - d.getDay() + 1);
    $('#lez-dal').value = d.toISOString().slice(0, 10);
    d.setDate(d.getDate() + 13);
    $('#lez-al').value = d.toISOString().slice(0, 10);
  }
  $('#elenco-lezioni').innerHTML = scheletroRighe(4);
  const d = await get(`/api/gestione/lezioni?dal=${$('#lez-dal').value}&al=${$('#lez-al').value}`);
  const perGiorno = {};
  d.lezioni.forEach(l => (perGiorno[l.data] = perGiorno[l.data] || []).push(l));

  $('#elenco-lezioni').innerHTML = Object.keys(perGiorno).sort().map(data => `
    <div class="mb-2">
      <div class="small fw-semibold text-capitalize text-muted">${fmtData(data)}</div>
      ${perGiorno[data].map((l, i) => `
        <button class="btn btn-sm w-100 text-start mb-1 riga-lezione rivela-riga ${l.presenti ? 'fatta' : ''}"
                style="animation-delay:${Math.min(i * 40, 240)}ms"
                data-lez="${l.id}">
          <span class="d-flex justify-content-between align-items-center gap-2">
            <span>
              <span class="fw-semibold">${l.ora_inizio}-${l.ora_fine}</span>
              <span class="pill ms-1">${esc(l.listato)}</span>
              ${l.aula ? `<span class="text-muted" style="font-size:.72rem"> ${esc(l.aula)}</span>` : ''}
            </span>
            <span class="text-nowrap">
              ${l.presenti
                ? `<span class="badge text-bg-success">${l.presenti} presenti</span>`
                : '<span class="apri-presenze">Registra presenze</span>'}
              <span class="freccia">&rsaquo;</span>
            </span>
          </span>
        </button>`).join('')}
    </div>`).join('') || '<p class="text-muted small">Nessuna lezione nel periodo. Usa "Genera dall\'orario".</p>';

  $$('#elenco-lezioni button[data-lez]').forEach(b =>
    b.addEventListener('click', () => apriLezione(+b.dataset.lez)));

  // Si apre da sola la lezione piu' vicina a oggi: entrando nella sezione si
  // trova subito l'elenco da spuntare, invece di un riquadro che dice
  // "scegli una lezione" e lascia l'impressione che manchi qualcosa.
  if (d.lezioni.length && !S.lezione) {
    const oggi = new Date().toISOString().slice(0, 10);
    const vicina = d.lezioni.reduce((a, b) =>
      Math.abs(new Date(b.data) - new Date(oggi)) < Math.abs(new Date(a.data) - new Date(oggi)) ? b : a);
    apriLezione(vicina.id);
  }
  $$('#elenco-lezioni button[data-lez]').forEach(b =>
    b.classList.toggle('scelta', S.lezione && +b.dataset.lez === S.lezione.id));
}

$('#lez-dal').addEventListener('change', caricaLezioni);
$('#lez-al').addEventListener('change', caricaLezioni);

$('#btn-genera').addEventListener('click', async () => {
  try {
    const r = await post('/api/gestione/lezioni/genera',
      { dal: $('#lez-dal').value, al: $('#lez-al').value });
    avviso(r.create ? `${r.create} lezioni create dal calendario settimanale.`
                    : 'Le lezioni del periodo erano gia\' tutte presenti.', 'success');
    caricaLezioni();
  } catch (e) { avviso(e.message); }
});

async function apriLezione(id) {
  const d = await get('/api/gestione/lezioni/' + id);
  S.lezione = d.lezione;
  const l = d.lezione;
  const suoi = d.allievi.filter(a => a.listato_target === l.listato);
  const altri = d.allievi.filter(a => a.listato_target !== l.listato);

  const riga = (a) => `
    <label class="riga-allievo d-flex align-items-center gap-2 py-2 px-1">
      <input class="form-check-input m-0 chk" type="checkbox" data-u="${a.utente_id}"
             ${a.stato === 'presente' ? 'checked' : ''}>
      <span class="flex-grow-1">
        <span class="fw-semibold small">${esc(a.cognome)} ${esc(a.nome)}</span>
        <span class="text-muted d-block" style="font-size:.72rem">
          ${esc(a.listato_target)}${a.telefono ? ' &middot; ' + esc(a.telefono) : ''}
          &middot; ${a.presenze_totali} lezioni frequentate${a.ore_acquistate ? ' su ' + a.ore_acquistate + ' acquistate' : ''}
        </span>
      </span>
    </label>`;

  $('#pannello-presenze').innerHTML = `
    <div class="d-flex justify-content-between align-items-start flex-wrap gap-2">
      <div>
        <h2 class="h6 mb-0">${fmtData(l.data)} &middot; ${l.ora_inizio}-${l.ora_fine}</h2>
        <p class="text-muted small mb-0">Categoria ${esc(l.listato)}${l.aula ? ' &middot; ' + esc(l.aula) : ''}
          ${l.docente ? ' &middot; ' + esc(l.docente) : ''}</p>
      </div>
      <span class="badge text-bg-${l.stato === 'svolta' ? 'success' : 'secondary'}">${esc(l.stato)}</span>
    </div>
    <div class="row g-2 my-2">
      <div class="col-8"><input class="form-control form-control-sm" id="arg-lezione"
             placeholder="Argomento svolto" value="${esc(l.argomento || '')}"></div>
      <div class="col-4"><button class="btn btn-sm btn-outline-primary w-100" id="btn-salva-arg">Salva</button></div>
    </div>
    <div class="d-flex justify-content-between align-items-center border-top pt-2 flex-wrap gap-2">
      <span class="small text-muted">Spunta chi e' presente</span>
      <span class="d-flex align-items-center gap-2">
        <button class="btn btn-sm btn-outline-success" id="btn-tutti-presenti">Tutti presenti</button>
        <button class="btn btn-sm btn-outline-secondary" id="btn-azzera-presenze">Azzera</button>
        <span class="small"><strong id="conta-presenti">0</strong> presenti</span>
      </span>
    </div>
    <div style="max-height:52vh;overflow:auto">
      ${suoi.map(riga).join('')}
      ${altri.length ? `<div class="text-muted small mt-2 mb-1">Da altre categorie</div>${altri.map(riga).join('')}` : ''}
    </div>`;

  const conta = () => $('#conta-presenti').textContent = $$('#pannello-presenze .chk:checked').length;
  conta();
  $$('#pannello-presenze .chk').forEach(ch => ch.addEventListener('change', async () => {
    try {
      await post(`/api/gestione/lezioni/${id}/presenza`,
        { utente_id: +ch.dataset.u, stato: ch.checked ? 'presente' : 'assente' });
      conta();
      caricaLezioni();
    } catch (e) { avviso(e.message); ch.checked = !ch.checked; }
  }));
  // Spunta o togli tutti in un colpo. In aula si segna la classe intera e si
  // levano i due assenti: e' l'ordine naturale delle cose, non il contrario.
  async function tutti(stato) {
    const caselle = $$('#pannello-presenze .chk');
    for (const ch of caselle) {
      if ((stato === 'presente') === ch.checked) continue;
      ch.checked = (stato === 'presente');
      try {
        await post(`/api/gestione/lezioni/${id}/presenza`,
          { utente_id: +ch.dataset.u, stato });
      } catch (e) { avviso(e.message); ch.checked = !ch.checked; }
    }
    conta();
    caricaLezioni();
  }
  $('#btn-tutti-presenti').addEventListener('click', () => tutti('presente'));
  $('#btn-azzera-presenze').addEventListener('click', () => {
    if (confirm('Togliere tutte le presenze di questa lezione?')) tutti('assente');
  });

  $('#btn-salva-arg').addEventListener('click', async () => {
    await put('/api/gestione/lezioni/' + id,
      { argomento: $('#arg-lezione').value, stato: 'svolta' });
    avviso('Lezione aggiornata.', 'success');
    caricaLezioni();
  });
}

/* -------------------------------- CLASSI ---------------------------------
 * Una classe e' il gruppo con cui l'allievo segue il corso. Serve a due cose:
 * raggruppare gli allievi, e decidere chi vede una videolezione o una diretta.
 * Un allievo sta in una classe sola; una lezione puo' essere aperta a piu'
 * classi insieme.
 * ------------------------------------------------------------------------ */
async function elencoClassi() {
  const d = await get('/api/gestione/classi');
  S.classi = d.classi;
  S.senzaClasse = d.senza_classe;
  return d;
}

// I menu a tendina delle classi compaiono in quattro punti diversi: si
// riempiono tutti da qui, cosi' una classe appena creata e' subito scegliibile
// senza ricaricare la pagina.
function riempiTendineClassi() {
  const opzioni = (S.classi || []).filter(c => c.attiva)
    .map(c => `<option value="${c.id}">${esc(c.nome)}</option>`).join('');
  ['#v-classi', '#l-classi'].forEach(sel => { if ($(sel)) $(sel).innerHTML = opzioni; });
  if ($('#a-classe')) {
    $('#a-classe').innerHTML = '<option value="">— nessuna classe —</option>' + opzioni;
  }
}

async function caricaClassi() {
  $('#elenco-classi').innerHTML = scheletroRighe(3);
  const d = await elencoClassi();
  riempiTendineClassi();
  if ($('#c-listato') && !$('#c-listato').innerHTML) {
    $('#c-listato').innerHTML = '<option value="">Patente prevalente (facoltativa)</option>'
      + (S.listati || []).map(l => `<option value="${l.codice}">${esc(l.codice)} - ${esc(l.nome)}</option>`).join('');
  }

  // Gli allievi non ancora assegnati si mostrano per primi: e' la cosa da
  // sistemare, e finche' restano li' non vedono nessuna videolezione.
  const senza = d.senza_classe
    ? `<div class="card-ac p-3 mb-3 border border-warning">
         <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
           <div><strong class="small">${d.senza_classe} allievi senza classe</strong>
             <div class="text-muted" style="font-size:.75rem">Finche' non hanno una classe non vedono videolezioni ne' dirette.</div></div>
           <button class="btn btn-sm btn-warning" id="btn-assegna-senza">Assegnali ora</button>
         </div></div>` : '';

  $('#elenco-classi').innerHTML = senza + (d.classi.map((c, i) => `
    <div class="card-ac p-3 mb-3 rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
      <div class="d-flex justify-content-between align-items-start flex-wrap gap-2 mb-2">
        <div>
          <h3 class="h6 mb-0">${esc(c.nome)}
            ${c.listato_target ? `<span class="pill ms-1">${esc(c.listato_target)}</span>` : ''}
            ${c.attiva ? '' : '<span class="badge text-bg-secondary ms-1">archiviata</span>'}</h3>
          <div class="text-muted" style="font-size:.75rem">${esc(c.descrizione || '')}</div>
        </div>
        <div class="d-flex gap-1">
          <span class="pill">${c.n_allievi} allievi</span>
          <span class="pill">${c.n_lezioni} lezioni</span>
          <button class="btn btn-sm btn-light" data-apri="${c.id}">Allievi</button>
          <button class="btn btn-sm btn-outline-danger" data-elimina="${c.id}"
                  title="Elimina la classe. Gli allievi non si perdono: restano senza classe.">Elimina</button>
        </div>
      </div>
      <div class="d-none" id="cl-${c.id}"></div>
    </div>`).join('') || '<p class="text-muted small">Nessuna classe. Creane una qui a fianco.</p>');

  if ($('#btn-assegna-senza')) {
    $('#btn-assegna-senza').addEventListener('click', () => apriAssegnazione(null));
  }
  $$('#elenco-classi button[data-apri]').forEach(b => b.addEventListener('click', () =>
    apriAssegnazione(+b.dataset.apri)));
  $$('#elenco-classi button[data-elimina]').forEach(b => b.addEventListener('click', async () => {
    const c = S.classi.find(x => x.id === +b.dataset.elimina);
    if (!confirm(`Eliminare la classe "${c.nome}"?\n\n`
      + `I ${c.n_allievi} allievi NON vengono cancellati: restano senza classe, con tutti i loro quiz e statistiche.`)) return;
    try {
      const r = await api('/api/gestione/classi/' + c.id, { method: 'DELETE' });
      avviso(`Classe eliminata. ${r.allievi_senza_classe} allievi ora senza classe.`, 'success');
      caricaClassi();
    } catch (e) { avviso(e.message); }
  }));
}

/* Pannello di assegnazione: a sinistra chi c'e' gia', a destra chi si puo'
 * aggiungere. Passando classe = null si parte da chi non ha nessuna classe. */
async function apriAssegnazione(classeId) {
  const box = classeId ? $('#cl-' + classeId) : null;
  const nome = classeId ? S.classi.find(c => c.id === classeId).nome : null;
  const dentro = classeId ? await get(`/api/gestione/classi/${classeId}/allievi`) : [];
  const tutti = (await get('/api/gestione/allievi')).categorie.flatMap(c => c.allievi);
  const fuori = tutti.filter(a => a.classe_id !== classeId);

  const html = `
    <hr class="my-2">
    <div class="row g-3">
      <div class="col-md-6">
        <label class="form-label small mb-1">${classeId ? 'In questa classe' : 'Allievi'}</label>
        <div class="border rounded p-2" style="max-height:220px;overflow:auto">
          ${dentro.length ? dentro.map(a => `<div class="small py-1">${esc(a.cognome)} ${esc(a.nome)}
             <span class="text-muted" style="font-size:.72rem">${esc(a.listato_target)}</span></div>`).join('')
            : '<p class="text-muted small mb-0">Nessun allievo.</p>'}
        </div>
      </div>
      <div class="col-md-6">
        <label class="form-label small mb-1">Da aggiungere ${classeId ? '' : '(scegli poi la classe)'}</label>
        <select class="form-select form-select-sm" id="sel-agg" multiple size="8">
          ${fuori.map(a => `<option value="${a.id}">${esc(a.cognome)} ${esc(a.nome)}`
            + ` — ${esc(a.classe || 'senza classe')}</option>`).join('')}
        </select>
        <div class="d-flex gap-2 mt-2">
          ${classeId ? '' : `<select class="form-select form-select-sm" id="sel-dest">
              ${(S.classi || []).filter(c => c.attiva).map(c => `<option value="${c.id}">${esc(c.nome)}</option>`).join('')}
            </select>`}
          <button class="btn btn-sm btn-primary" id="btn-fai-assegna">Sposta qui</button>
        </div>
      </div>
    </div>`;

  if (box) {
    box.innerHTML = html;
    box.classList.remove('d-none');
  } else {
    // Senza una classe di partenza si apre in cima, sopra l'elenco.
    const tmp = document.createElement('div');
    tmp.className = 'card-ac p-3 mb-3';
    tmp.innerHTML = '<h3 class="h6">Assegna allievi a una classe</h3>' + html;
    $('#elenco-classi').prepend(tmp);
  }

  $('#btn-fai-assegna').addEventListener('click', async () => {
    const ids = [...$('#sel-agg').selectedOptions].map(o => +o.value);
    if (!ids.length) return avviso('Scegli almeno un allievo.');
    const dest = classeId || +$('#sel-dest').value;
    try {
      const r = await post(`/api/gestione/classi/${dest}/allievi`, { utenti: ids });
      avviso(`${r.spostati} allievi spostati in "${nome || S.classi.find(c => c.id === dest).nome}".`, 'success');
      caricaClassi();
    } catch (e) { avviso(e.message); }
  });
}

$('#btn-salva-classe').addEventListener('click', async () => {
  const nome = $('#c-nome').value.trim();
  if (!nome) return avviso('Serve il nome della classe.');
  try {
    await post('/api/gestione/classi', {
      nome, descrizione: $('#c-descrizione').value.trim() || null,
      listato_target: $('#c-listato').value || null,
    });
    $('#c-nome').value = ''; $('#c-descrizione').value = '';
    avviso('Classe creata.', 'success');
    caricaClassi();
  } catch (e) { avviso(e.message); }
});

/* ------------------------------- ALLIEVI --------------------------------- */
async function caricaAllievi() {
  const q = $('#cerca-allievo').value.trim();
  $('#elenco-allievi').innerHTML = scheletroRighe(4);
  const d = await get('/api/gestione/allievi' + (q ? '?cerca=' + encodeURIComponent(q) : ''));
  $('#riassunto-allievi').textContent =
    `${d.totale} allievi attivi in ${d.categorie.length} categorie`;

  $('#elenco-allievi').innerHTML = d.categorie.map(c => `
    <div class="card-ac p-3 mb-3">
      <div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">
        <h3 class="h6 mb-0">Categoria ${esc(c.codice)} <span class="pill ms-1">${c.n} allievi</span></h3>
        <span class="small text-muted">${c.ore_vendute} ore vendute &middot; ${c.incasso.toFixed(2)} euro incassati</span>
      </div>
      <div class="table-responsive"><table class="table table-sm align-middle mb-0">
        <thead><tr><th>Allievo</th><th>Classe</th><th class="text-end">Lezioni fatte</th>
          <th class="text-end">Quiz</th><th></th></tr></thead>
        <tbody>${c.allievi.map((a, i) => `
          <tr class="rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
            <td><div class="fw-semibold small">${esc(a.cognome)} ${esc(a.nome)}${a.listati_extra ? ` <span class="pill">+${esc(a.listati_extra)}</span>` : ''}</div>
                <div class="text-muted" style="font-size:.72rem">${esc(a.username || a.email)}</div></td>
            <td class="small">${a.classe ? esc(a.classe) : '<span class="text-warning">senza classe</span>'}
                <div class="text-muted" style="font-size:.72rem">${esc(a.telefono || '-')}</div></td>
            <td class="text-end small">${a.ore_frequentate}/${a.ore_acquistate || 0}</td>
            <td class="text-end small">${a.schede} schede<div class="text-muted" style="font-size:.72rem">${a.simulazioni_superate} simul. ok</div></td>
            <td class="text-end"><button class="btn btn-sm btn-light" data-mod="${a.id}">Apri</button></td>
          </tr>`).join('')}</tbody>
      </table></div>
    </div>`).join('') || '<p class="text-muted">Nessun allievo registrato.</p>';

  $$('#elenco-allievi button[data-mod]').forEach(b => b.addEventListener('click', () => {
    const tutti = d.categorie.flatMap(c => c.allievi);
    apriAllievo(tutti.find(a => a.id === +b.dataset.mod));
  }));
}

$('#cerca-allievo').addEventListener('input', () => {
  clearTimeout(S.tCerca);
  S.tCerca = setTimeout(caricaAllievi, 300);
});

function apriAllievo(a) {
  S.allievo = a || null;
  $('#tit-allievo').textContent = a ? `${a.cognome} ${a.nome}` : 'Nuova iscrizione';
  $('#a-listato').innerHTML = S.listati.map(l =>
    `<option value="${l.codice}">${esc(l.codice)} - ${esc(l.nome)}</option>`).join('');
  $('#a-nome').value = a?.nome || '';
  $('#a-cognome').value = a?.cognome || '';
  $('#a-telefono').value = a?.telefono || '';
  $('#a-email').value = (a?.email || '').endsWith('@locale') ? '' : (a?.email || '');
  $('#a-indirizzo').value = a?.indirizzo || '';
  $('#a-cf').value = a?.codice_fiscale || '';
  // La prima selezionata e' la principale: comanda simulazione e scadenze.
  const sue = a ? [a.listato_target, ...String(a.listati_extra || '').split(',')]
      .map(x => (x || '').trim()).filter(Boolean) : ['B'];
  [...$('#a-listato').options].forEach(o => { o.selected = sue.includes(o.value); });
  riempiTendineClassi();
  $('#a-classe').value = a?.classe_id || '';
  $('#a-ore').value = a?.ore_acquistate || 0;
  $('#a-importo').value = a?.importo_pagato || '';
  $('#a-esame').value = a?.data_esame || '';
  $('#a-note').value = a?.note_admin || '';
  $('#box-credenziali').classList.add('d-none');
  $('#btn-nuova-password').classList.toggle('d-none', !a);
  $('#btn-cancella-allievo').classList.toggle('d-none', !a);
  $('#btn-salva-allievo').textContent = a ? 'Salva modifiche' : 'Registra iscrizione';
  bootstrap.Modal.getOrCreateInstance($('#m-allievo')).show();
}

/* Export dell'anagrafica: il file si genera sul momento e resta solo nel
 * computer di chi lo scarica - sul server non viene salvato niente. Serve
 * una fetch e non un semplice link perche' l'API vuole il token di accesso,
 * che un link non porta con se'. */
$('#btn-esporta-allievi')?.addEventListener('click', async (e) => {
  const bottone = e.currentTarget;
  const testo = bottone.textContent;
  bottone.disabled = true;
  bottone.textContent = 'Preparo...';
  try {
    const res = await fetch('/api/gestione/allievi/esporta', {
      headers: { Authorization: 'Bearer ' + localStorage.getItem('ac_access') },
    });
    if (!res.ok) throw new Error('Errore ' + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement('a'), {
      href: url, download: `allievi-${new Date().toISOString().slice(0, 10)}.csv`,
    });
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    avviso('Elenco scaricato. Contiene dati personali: conservalo con cura.', 'success');
  } catch (err) {
    avviso('Export non riuscito: ' + err.message);
  } finally {
    bottone.disabled = false;
    bottone.textContent = testo;
  }
});

$('#btn-nuovo-allievo').addEventListener('click', () => apriAllievo(null));

$('#btn-salva-allievo').addEventListener('click', async () => {
  const corpo = {
    nome: $('#a-nome').value.trim(), cognome: $('#a-cognome').value.trim(),
    telefono: $('#a-telefono').value.trim() || null,
    email: $('#a-email').value.trim() || null,
    indirizzo: $('#a-indirizzo').value.trim() || null,
    codice_fiscale: $('#a-cf').value.trim() || null,
    patenti: (() => {
      // selectedOptions segue l'ordine del menu, non quello dei clic: senza
      // questo, riaprire la scheda di un allievo e salvare poteva cambiargli
      // la patente principale (quella che comanda simulazione e scadenze).
      const scelte = [...$('#a-listato').selectedOptions].map(o => o.value);
      const principale = S.allievo?.listato_target;
      return principale && scelte.includes(principale)
        ? [principale, ...scelte.filter(x => x !== principale)] : scelte;
    })(),
    classe_id: +$('#a-classe').value || null,
    ore_acquistate: +$('#a-ore').value || 0,
    importo_pagato: parseFloat($('#a-importo').value) || null,
    data_esame: $('#a-esame').value || null,
    note: $('#a-note').value.trim() || null,
  };
  if (!corpo.nome || !corpo.cognome) return avviso('Nome e cognome sono obbligatori.');
  try {
    if (S.allievo) {
      await put('/api/gestione/allievi/' + S.allievo.id, corpo);
      bootstrap.Modal.getInstance($('#m-allievo')).hide();
      avviso('Anagrafica aggiornata.', 'success');
    } else {
      const r = await post('/api/gestione/allievi', corpo);
      mostraCredenziali(r.username, r.password, r.email_accesso);
    }
    caricaAllievi();
  } catch (e) { avviso(e.message); }
});

$('#btn-nuova-password').addEventListener('click', async () => {
  if (!S.allievo || !confirm('Generare una nuova password? Quella attuale smettera\' di funzionare.')) return;
  const r = await post(`/api/gestione/allievi/${S.allievo.id}/password`);
  mostraCredenziali(r.username, r.password, S.allievo.email);
});

/* Cancellazione definitiva: e' la risposta all'allievo che chiede di far
 * sparire i propri dati, dove disattivare non basta. Si chiede di riscrivere
 * il cognome perche' non e' un'operazione da fare per sbaglio: schede,
 * statistiche e presenze se ne vanno con lui e non tornano indietro. */
$('#btn-cancella-allievo').addEventListener('click', async () => {
  if (!S.allievo) return;
  const atteso = (S.allievo.cognome || '').trim();
  const scritto = prompt(
    `CANCELLAZIONE DEFINITIVA di ${S.allievo.nome} ${atteso}.\n\n` +
    'Spariscono anagrafica, accessi, quiz svolti, statistiche e presenze. ' +
    'Non si torna indietro: se ti serve solo togliere l\'allievo dagli elenchi, ' +
    'chiudi qui e usa invece la disattivazione.\n\n' +
    `Per procedere scrivi il cognome dell'allievo (${atteso}):`);
  if (scritto === null) return;
  try {
    const r = await api(
      `/api/gestione/allievi/${S.allievo.id}/definitivo?conferma=${encodeURIComponent(scritto)}`,
      { method: 'DELETE' });
    bootstrap.Modal.getOrCreateInstance($('#m-allievo')).hide();
    avviso(`Dati di ${r.cancellato} cancellati definitivamente.`, 'success');
    caricaAllievi();
  } catch (e) {
    avviso(e.message);
  }
});

function mostraCredenziali(username, password, email) {
  $('#box-credenziali').classList.remove('d-none');
  $('#credenziali').innerHTML =
    `nome utente: <strong>${esc(username || email)}</strong><br>` +
    `password: <strong>${esc(password)}</strong>`;
  $('#btn-salva-allievo').classList.add('d-none');
}

$('#m-allievo').addEventListener('hidden.bs.modal', () => {
  $('#btn-salva-allievo').classList.remove('d-none');
});

/* -------------------------------- VIDEO ---------------------------------- */
$$('#tab-video button').forEach(b => b.addEventListener('click', () => {
  $$('#tab-video button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  $('#modo-link').classList.toggle('d-none', b.dataset.modo !== 'link');
  $('#modo-file').classList.toggle('d-none', b.dataset.modo !== 'file');
}));

async function caricaVideo() {
  $('#elenco-video').innerHTML = scheletroRighe(4);
  const v = await get('/api/gestione/video');
  $('#elenco-video').innerHTML = v.map((x, i) => `
    <div class="d-flex justify-content-between align-items-start gap-2 py-2 border-bottom rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms">
      <div class="flex-grow-1">
        <div class="small fw-semibold">${esc(x.titolo)}
          ${x.tipo === 'live' ? '<span class="badge text-bg-danger ms-1">diretta</span>' : ''}
          ${x.pubblicata ? '' : '<span class="badge text-bg-secondary ms-1">nascosta</span>'}</div>
        <div class="text-muted" style="font-size:.72rem">
          ${esc(x.listato)}
          ${x.durata_sec ? ' &middot; ' + Math.round(x.durata_sec / 60) + ' min' : ''}
          ${x.inizio_live ? ' &middot; ' + esc(x.inizio_live.replace('T', ' ')) : ''}
          &middot; ${x.visualizzazioni} visualizzazioni, ${x.completate} completate
        </div>
        <div style="font-size:.72rem">${x.classi_nomi
          ? 'Visibile a: <strong>' + esc(x.classi_nomi) + '</strong>'
          : '<span class="text-warning">Nessuna classe: non la vede nessun allievo</span>'}
          <button class="btn btn-link btn-sm p-0 ms-1" style="font-size:.72rem"
                  data-classi="${x.id}" data-sel="${(x.classi || []).join(',')}">cambia</button></div>
        <div class="text-muted text-truncate" style="font-size:.7rem;max-width:420px">${esc(x.url || '')}</div>
      </div>
      <div class="d-flex gap-1">
        ${x.tipo === 'live' ? `<select class="form-select form-select-sm" data-live="${x.id}" style="width:130px">
            ${['programmata', 'in_onda', 'conclusa'].map(s =>
              `<option ${x.stato_live === s ? 'selected' : ''}>${s}</option>`).join('')}
          </select>` : ''}
        <button class="btn btn-sm btn-outline-primary" data-mod="${x.id}"
                data-titolo="${esc(x.titolo)}" data-url="${esc(x.url || '')}">Modifica</button>
        <button class="btn btn-sm btn-outline-secondary" data-pub="${x.id}" data-val="${x.pubblicata ? 0 : 1}">
          ${x.pubblicata ? 'Nascondi' : 'Pubblica'}</button>
      </div>
    </div>`).join('') || '<p class="text-muted small">Nessun materiale pubblicato.</p>';

  // Cambiare le classi di una lezione gia' pubblicata: capita di doverla
  // aprire a un secondo corso, e ricrearla da zero perderebbe le visualizzazioni.
  $$('#elenco-video button[data-classi]').forEach(b => b.addEventListener('click', () => {
    if (!(S.classi || []).length) return avviso('Prima crea almeno una classe.');
    const gia = (b.dataset.sel || '').split(',').filter(Boolean).map(Number);
    const scelta = prompt(
      'A quali classi e\' destinata questa lezione?\n\n'
      + S.classi.filter(c => c.attiva).map((c, i) => `${i + 1}) ${c.nome}`).join('\n')
      + '\n\nScrivi i numeri separati da virgola (vuoto = nessuna classe).',
      S.classi.filter(c => c.attiva).map((c, i) => gia.includes(c.id) ? i + 1 : null)
        .filter(Boolean).join(','));
    if (scelta === null) return;
    const attive = S.classi.filter(c => c.attiva);
    const ids = scelta.split(',').map(x => attive[parseInt(x, 10) - 1])
      .filter(Boolean).map(c => c.id);
    put('/api/gestione/video/' + b.dataset.classi, { classi: ids })
      .then(() => { avviso(ids.length ? 'Visibilita\' aggiornata.'
        : 'Lezione tolta a tutte le classi: ora non la vede nessuno.',
        ids.length ? 'success' : 'warning'); caricaVideo(); })
      .catch(e => avviso(e.message));
  }));

  $$('#elenco-video button[data-pub]').forEach(b => b.addEventListener('click', async () => {
    await put('/api/gestione/video/' + b.dataset.pub, { pubblicata: b.dataset.val === '1' });
    caricaVideo();
  }));
  // Modifica di titolo e link: si correggono gli errori di battitura senza
  // dover cancellare la lezione e ricrearla da zero.
  $$('#elenco-video button[data-mod]').forEach(b => b.addEventListener('click', async () => {
    const titolo = prompt('Titolo della lezione:', b.dataset.titolo);
    if (titolo === null) return;
    const url = prompt('Link (lasciare invariato se va bene):', b.dataset.url);
    if (url === null) return;
    try {
      await put('/api/gestione/video/' + b.dataset.mod,
        { titolo: titolo.trim() || undefined, url: url.trim() || undefined });
      avviso('Lezione aggiornata.', 'success');
      caricaVideo();
    } catch (e) { avviso(e.message); }
  }));
  $$('#elenco-video select[data-live]').forEach(s => s.addEventListener('change', async () => {
    await put('/api/gestione/video/' + s.dataset.live, { stato_live: s.value });
    avviso('Stato della diretta aggiornato.', 'success');
  }));
}

$('#btn-salva-video').addEventListener('click', async () => {
  const titolo = $('#v-titolo').value.trim();
  if (!titolo) return avviso('Serve il titolo della lezione.');
  const perFile = $('#tab-video button.active').dataset.modo === 'file';
  try {
    if (perFile) {
      const f = $('#v-file').files[0];
      if (!f) return avviso('Scegli un file video.');
      const barra = $('#barra-upload');
      barra.classList.remove('d-none');
      barra.firstElementChild.style.width = '30%';
      const q = `?titolo=${encodeURIComponent(titolo)}&nome_file=${encodeURIComponent(f.name)}`
              + `&listato=${$('#v-listato').value}`;
      const r = await api('/api/gestione/video/carica' + q, { method: 'POST', body: f });
      barra.firstElementChild.style.width = '100%';
      setTimeout(() => barra.classList.add('d-none'), 800);
      avviso(`Caricati ${r.megabyte} MB.`, 'success');
    } else {
      const classi = [...$('#v-classi').selectedOptions].map(o => +o.value);
      await post('/api/gestione/video', {
        titolo, listato: $('#v-listato').value, url: $('#v-url').value.trim(),
        durata_min: +$('#v-durata').value || 0, classi,
      });
      avviso(classi.length ? 'Videolezione aggiunta.'
        : 'Videolezione aggiunta, ma senza classi: per ora non la vede nessuno.',
        classi.length ? 'success' : 'warning');
    }
    $('#v-titolo').value = ''; $('#v-url').value = ''; $('#v-durata').value = '';
    if ($('#v-file')) $('#v-file').value = '';
    caricaVideo();
  } catch (e) { avviso(e.message); $('#barra-upload').classList.add('d-none'); }
});

$('#btn-salva-live').addEventListener('click', async () => {
  try {
    const classi = [...$('#l-classi').selectedOptions].map(o => +o.value);
    await post('/api/gestione/live', {
      titolo: $('#l-titolo').value.trim(), listato: $('#l-listato').value,
      url: $('#l-url').value.trim(), inizio: $('#l-inizio').value, classi,
    });
    avviso(classi.length ? 'Diretta programmata: avviso inviato alle classi scelte.'
      : 'Diretta programmata, ma senza classi: non la vede nessuno e non parte nessun avviso.',
      classi.length ? 'success' : 'warning');
    $('#l-titolo').value = ''; $('#l-url').value = '';
    caricaVideo();
  } catch (e) { avviso(e.message); }
});

/* -------------------------------- ANALISI -------------------------------- */
async function caricaAnalisi() {
  $('#osservazioni').innerHTML = scheletroRighe(3);
  $('#tab-analisi').innerHTML = scheletroTabella(4, 8);
  $('#tab-parti').innerHTML = scheletroTabella(3, 7);
  const d = await get('/api/gestione/analisi');
  $('#nota-metodo').textContent = d.nota_metodo;
  $('#osservazioni').innerHTML = d.osservazioni.map((o, i) =>
    `<div class="d-flex gap-2 py-1 rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms"><span class="text-primary">&bull;</span><span class="small">${esc(o)}</span></div>`).join('');

  const colore = (v) => v === null ? '#cbd5e1' : v > 30 ? '#dc3545' : v > 20 ? '#ffc107' : '#198754';
  $('#tab-analisi').innerHTML = `
    <thead><tr><th>Fascia</th><th>Categoria</th><th class="text-end">Lezioni</th>
      <th class="text-end">Media presenti</th><th class="text-end">Allievi stabili</th>
      <th class="text-end">Errori</th><th class="text-end">Simulazioni ok</th><th></th></tr></thead>
    <tbody>${d.slot.map((s, i) => `
      <tr class="rivela-riga ${s.attivo ? '' : 'text-muted'}" style="animation-delay:${Math.min(i * 40, 240)}ms">
        <td><span class="fw-semibold small text-capitalize">${esc(s.nome_giorno)}</span>
            <div class="small">${esc(s.fascia)}</div></td>
        <td><span class="pill">${esc(s.listato)}</span></td>
        <td class="text-end small">${s.lezioni_svolte}</td>
        <td class="text-end small">${s.media_presenti}</td>
        <td class="text-end small">${s.prestazioni.allievi_attivi}</td>
        <td class="text-end small">${s.prestazioni.tasso_errore_pct ?? '-'}%
          <div class="barra mt-1"><span style="width:${Math.min(100, s.prestazioni.tasso_errore_pct || 0)}%;background:${colore(s.prestazioni.tasso_errore_pct)}"></span></div></td>
        <td class="text-end small">${s.prestazioni.percentuale_superate ?? '-'}%</td>
        <td class="text-end">${s.significativo ? '<span class="badge text-bg-light border">solido</span>'
                                                : '<span class="badge text-bg-warning">indicativo</span>'}</td>
      </tr>`).join('')}</tbody>`;

  disegna('gr-fasce', {
    type: 'bar',
    data: {
      labels: d.slot.map(s => `${s.nome_giorno.slice(0, 3)} ${s.fascia}`),
      datasets: [
        { label: '% errore', data: d.slot.map(s => s.prestazioni.tasso_errore_pct), backgroundColor: '#dc3545' },
        { label: '% simulazioni superate', data: d.slot.map(s => s.prestazioni.percentuale_superate), backgroundColor: '#198754' },
      ],
    },
    options: { responsive: true, scales: { y: { beginAtZero: true, max: 100 } },
      plugins: { legend: { position: 'bottom' } } },
  });

  $('#tab-parti').innerHTML = `
    <thead><tr><th>Parte della giornata</th><th class="text-end">Lezioni</th>
      <th class="text-end">Presenze</th><th class="text-end">Media presenti</th>
      <th class="text-end">Allievi</th><th class="text-end">Errori</th>
      <th class="text-end">Ore di studio medie</th></tr></thead>
    <tbody>${d.per_parte_giornata.map((p, i) => `
      <tr class="rivela-riga" style="animation-delay:${Math.min(i * 40, 240)}ms"><td class="text-capitalize small fw-semibold">${esc(p.parte)}</td>
        <td class="text-end small">${p.lezioni}</td>
        <td class="text-end small">${p.presenze}</td>
        <td class="text-end small">${p.media_presenti}</td>
        <td class="text-end small">${p.prestazioni.allievi_attivi}</td>
        <td class="text-end small">${p.prestazioni.tasso_errore_pct ?? '-'}%</td>
        <td class="text-end small">${p.prestazioni.ore_studio_medie}</td>
      </tr>`).join('')}</tbody>`;

  disegna('gr-parti', {
    type: 'bar',
    data: {
      labels: d.per_parte_giornata.map(p => p.parte.charAt(0).toUpperCase() + p.parte.slice(1)),
      datasets: [
        { label: '% errore', data: d.per_parte_giornata.map(p => p.prestazioni.tasso_errore_pct), backgroundColor: '#dc3545' },
        { label: 'Media presenti', data: d.per_parte_giornata.map(p => p.media_presenti), backgroundColor: '#e0261b', yAxisID: 'y1' },
      ],
    },
    options: { responsive: true, scales: {
      y: { beginAtZero: true, max: 100, title: { display: true, text: '% errore' } },
      y1: { position: 'right', beginAtZero: true, grid: { drawOnChartArea: false }, title: { display: true, text: 'Media presenti' } },
    }, plugins: { legend: { position: 'bottom' } } },
  });
}

/* --------------------------------- Avvio --------------------------------- */
(async function avvio() {
  if (!localStorage.getItem('ac_access')) { location.href = '/'; return; }
  try {
    S.utente = await get('/api/auth/me');
  } catch (e) { return; }
  if (!['admin', 'istruttore', 'superadmin'].includes(S.utente.ruolo)) {
    document.body.innerHTML = '<div class="container py-5"><div class="alert alert-warning">'
      + 'Questa pagina e\' riservata al personale dell\'autoscuola.'
      + ' <a href="/">Torna ai quiz</a></div></div>';
    return;
  }
  $('#chi').textContent = `${S.utente.nome} ${S.utente.cognome || ''} - ${S.utente.ragione_sociale || ''}`;

  S.listati = await get('/api/catalogo/listati');
  // Le tendine delle classi servono in tre sezioni diverse: si caricano una
  // volta all'avvio, non a ogni cambio di scheda.
  try { await elencoClassi(); riempiTendineClassi(); } catch (e) { S.classi = []; }
  ['#v-listato', '#l-listato'].forEach(sel => {
    $(sel).innerHTML = S.listati.map(l =>
      `<option value="${l.codice}">${esc(l.codice)} - ${esc(l.nome)}</option>`).join('');
  });

  // Si puo' arrivare qui puntando a una sezione precisa, per esempio
  // /app/gestione.html#video dal menu principale.
  const sezione = (location.hash || '').replace('#', '');
  const bottone = $(`#menu button[data-sez="${sezione}"]`);
  if (bottone) bottone.click();
  else caricaOrario();
})();
