/* =============================================================================
 * Animazioni: la parte che decide QUANDO far muovere le cose.
 *
 * Scritto per non toccare app.js: osserva la pagina e reagisce da solo. Se un
 * domani si vuole togliere tutto, basta rimuovere il collegamento a questo
 * file e all'omonimo foglio di stile.
 *
 * Quattro comportamenti:
 *   1. i riquadri compaiono quando entrano nello schermo, a scalare
 *   2. i pulsanti mostrano un'onda al tocco
 *   3. i numeri grandi salgono da zero al loro valore
 *   4. le risposte giuste pulsano, quelle sbagliate danno una scossa
 * ========================================================================== */
'use strict';

(function animazioni() {
  const fermo = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (fermo) return;   // chi ha chiesto meno movimento non ne riceve

  /* ---------------------------------------------- 1. comparsa allo scorrimento */

  const osservatore = new IntersectionObserver((voci) => {
    voci.forEach((v, i) => {
      if (!v.isIntersecting) return;
      // Ritardo a scalare, ma con un tetto: oltre i 240 ms l'attesa si nota
      // e diventa fastidiosa invece che elegante.
      v.target.style.transitionDelay = Math.min(i * 60, 240) + 'ms';
      v.target.classList.add('visibile');
      osservatore.unobserve(v.target);
    });
  }, { threshold: .08, rootMargin: '0px 0px -40px 0px' });

  function prepara(radice = document) {
    radice.querySelectorAll('.card-ac:not(.rivela), #scheda-scuola:not(.rivela)')
      .forEach(el => {
        // Chi e' gia' visibile all'apertura non deve sparire e riapparire.
        const posizione = el.getBoundingClientRect();
        if (posizione.top < window.innerHeight && posizione.bottom > 0) {
          el.classList.add('rivela', 'visibile');
          return;
        }
        el.classList.add('rivela');
        osservatore.observe(el);
      });
  }

  /* --------------------------------------------------- 2. onda sui pulsanti */

  document.addEventListener('pointerdown', (e) => {
    const btn = e.target.closest('.btn, .cella-nav, .nav-link');
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const lato = Math.max(r.width, r.height);
    const onda = document.createElement('span');
    onda.className = 'onda';
    onda.style.width = onda.style.height = lato + 'px';
    onda.style.left = (e.clientX - r.left - lato / 2) + 'px';
    onda.style.top = (e.clientY - r.top - lato / 2) + 'px';
    if (getComputedStyle(btn).position === 'static') btn.style.position = 'relative';
    btn.appendChild(onda);
    setTimeout(() => onda.remove(), 600);
  }, { passive: true });

  /* ------------------------------------------------------ 3. numeri che salgono */

  function conta(el) {
    const testo = (el.textContent || '').trim();
    const numero = parseFloat(testo.replace(',', '.').replace('%', ''));
    if (!isFinite(numero) || numero === 0 || el.dataset.contato) return;
    el.dataset.contato = '1';
    const decimali = (testo.split('.')[1] || testo.split(',')[1] || '').replace('%', '').length;
    const percentuale = testo.includes('%');
    const durata = 700;
    const inizio = performance.now();
    el.classList.add('numero-anima');
    function passo(ora) {
      const t = Math.min(1, (ora - inizio) / durata);
      // Rallenta verso la fine: cosi' l'occhio legge il valore finale.
      const valore = numero * (1 - Math.pow(1 - t, 3));
      el.textContent = valore.toFixed(decimali) + (percentuale ? '%' : '');
      if (t < 1) requestAnimationFrame(passo);
    }
    requestAnimationFrame(passo);
  }

  function numeri(radice = document) {
    radice.querySelectorAll('.card-ac .h2, .card-ac .h1, .card-ac .h3, .card-ac .display-6')
      .forEach(conta);
  }

  /* ------------------------------------------- 4. reazione alle risposte date */

  function reazione(nodo) {
    if (!(nodo instanceof HTMLElement)) return;
    const ok = nodo.querySelector?.('.alert-success') ||
               (nodo.classList?.contains('alert-success') ? nodo : null);
    const ko = nodo.querySelector?.('.alert-danger') ||
               (nodo.classList?.contains('alert-danger') ? nodo : null);
    const bersaglio = document.querySelector('#schermata-quiz .card-ac');
    if (!bersaglio) return;
    if (ok) {
      bersaglio.classList.remove('esito-sbagliato');
      bersaglio.classList.add('esito-giusto');
      setTimeout(() => bersaglio.classList.remove('esito-giusto'), 600);
    } else if (ko) {
      bersaglio.classList.remove('esito-giusto');
      bersaglio.classList.add('esito-sbagliato');
      setTimeout(() => bersaglio.classList.remove('esito-sbagliato'), 500);
    }
  }

  /* --------------------------------------------------------------- osservatore */

  const cambiamenti = new MutationObserver((liste) => {
    let daPreparare = false;
    liste.forEach(l => {
      l.addedNodes.forEach(n => {
        if (!(n instanceof HTMLElement)) return;
        daPreparare = true;
        if (n.closest?.('#esito-immediato') || n.id === 'esito-immediato') reazione(n);
        if (n.classList?.contains('cella-nav')) {
          n.classList.add('appena');
          setTimeout(() => n.classList.remove('appena'), 320);
        }
      });
      if (l.target?.id === 'esito-immediato') reazione(l.target);
    });
    if (daPreparare) {
      clearTimeout(window._tAnim);
      window._tAnim = setTimeout(() => { prepara(); numeri(); }, 60);
    }
  });

  function avvia() {
    prepara();
    numeri();
    cambiamenti.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', avvia);
  } else {
    avvia();
  }
})();
