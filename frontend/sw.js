/* Service worker - strategia differenziata per tipo di risorsa.
 *
 *  pagina, grafica, codice -> prima la rete, cache come riserva
 *      cosi' un aggiornamento si vede subito, e senza connessione si usa
 *      comunque l'ultima versione salvata
 *  immagini dei quiz        -> prima la cache
 *      il nome del file e' costruito sul contenuto: non cambiano mai
 *  chiamate API             -> prima la rete, cache come riserva
 */
const VERSIONE = 'cruscotto-v128';
// Nella shell vanno TUTTI i file dell'interfaccia. Bootstrap, Chart.js e il
// font ora stanno in /app/vendor/ e non piu' su CDN esterne: prima erano
// l'unico pezzo che la shell non poteva salvare, e senza rete al primo avvio
// l'app si apriva senza stili ne' grafici, nonostante il README prometta il
// contrario. Il font va elencato con i due woff2, altrimenti il CSS c'e' ma
// i caratteri no.
const SHELL = ['/', '/app/index.html', '/app/app.js', '/app/styles.css',
               '/app/animazioni.css', '/app/animazioni.js',
               '/app/gestione.html', '/app/gestione.js',
               '/app/vendor/bootstrap.min.css', '/app/vendor/bootstrap.bundle.min.js',
               '/app/vendor/chart.umd.js', '/app/vendor/bebas-neue.css',
               '/app/vendor/bebas-neue-latin-400-normal.woff2',
               '/app/vendor/bebas-neue-latin-ext-400-normal.woff2',
               '/manifest.webmanifest', '/app/assets/icon-192.png?v=2'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(VERSIONE)
    .then(c => c.addAll(SHELL).catch(() => null))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(k =>
    Promise.all(k.filter(x => !x.startsWith(VERSIONE)).map(x => caches.delete(x)))
  ).then(() => self.clients.claim()));
});

async function primaLaRete(richiesta, nomeCache) {
  try {
    const risposta = await fetch(richiesta);
    if (risposta && risposta.ok) {
      const copia = risposta.clone();
      caches.open(nomeCache).then(c => c.put(richiesta, copia));
    }
    return risposta;
  } catch (e) {
    const salvata = await caches.match(richiesta);
    if (salvata) return salvata;
    throw e;
  }
}

// Notifiche push: il payload arriva dal server come JSON semplice
// ({ titolo, corpo, url }), preparato in backend/app/services/notifiche.py.
self.addEventListener('push', e => {
  let dati = {};
  try { dati = e.data ? e.data.json() : {}; } catch (err) { dati = {}; }
  const titolo = dati.titolo || 'Autoscuola La Centauro';
  e.waitUntil(self.registration.showNotification(titolo, {
    body: dati.corpo || '',
    icon: '/app/assets/icon-192.png',
    badge: '/app/assets/icon-192.png',
    data: { url: dati.url || '/' },
  }));
});

// Click sulla notifica: si va alla pagina indicata, riusando una scheda del
// sito gia' aperta invece di aprirne sempre una nuova.
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const destinazione = new URL(e.notification.data?.url || '/', self.location.origin).href;
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(elenco => {
    const aperta = elenco.find(c => c.url.startsWith(self.location.origin));
    if (aperta) return aperta.navigate(destinazione).then(c => c.focus());
    return clients.openWindow(destinazione);
  }));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // I video non si mettono in cache: sono file da centinaia di MB, arrivano a
  // pezzi (richieste Range, che una cache non sa ricomporre) e il loro link e'
  // firmato e scade - una copia salvata smetterebbe di funzionare senza che si
  // capisca perche'.
  if (url.pathname.startsWith('/media/video/')) return;

  if (url.pathname.startsWith('/media/')) {
    e.respondWith(caches.open(VERSIONE + '-media').then(async c => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }));
    return;
  }

  if (url.pathname.startsWith('/api/')) {
    e.respondWith(primaLaRete(e.request, VERSIONE + '-api'));
    return;
  }

  e.respondWith(primaLaRete(e.request, VERSIONE));
});
