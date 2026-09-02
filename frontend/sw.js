/* Service worker - strategia differenziata per tipo di risorsa.
 *
 *  pagina, grafica, codice -> prima la rete, cache come riserva
 *      cosi' un aggiornamento si vede subito, e senza connessione si usa
 *      comunque l'ultima versione salvata
 *  immagini dei quiz        -> prima la cache
 *      il nome del file e' costruito sul contenuto: non cambiano mai
 *  chiamate API             -> prima la rete, cache come riserva
 */
const VERSIONE = 'cruscotto-v120';
const SHELL = ['/', '/app/index.html', '/app/app.js', '/app/styles.css',
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
