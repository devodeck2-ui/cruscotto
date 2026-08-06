# Pubblicare Autoscuola Cruscotto online

Tre modi, dal più semplice al più controllato. Tutti portano allo stesso risultato: l'app raggiungibile da qualsiasi dispositivo con HTTPS valido, installabile come PWA.

## Prima di iniziare: quanto costa

| Voce | Costo indicativo |
|---|---|
| VPS 2 vCPU / 4 GB (Hetzner, Contabo, Aruba) | 5–12 €/mese |
| Dominio `.it` | 10–15 €/anno |
| Certificato HTTPS | gratuito (Let's Encrypt) |
| API Claude per il tutor | ~0,01 € per spiegazione nuova, ~0 € su cache |

Un VPS da 4 GB regge comodamente qualche migliaio di allievi: il carico è quasi tutto in lettura e il database sta in 10 MB.

---

## Opzione 1 — Docker + Caddy (consigliata)

Caddy ottiene e rinnova il certificato da solo. Nessun cron di certbot, nessun rinnovo dimenticato che manda offline il sito a scadenza.

```bash
# 1. Sul server, con Docker già installato
git clone <il-tuo-repo> autoscuola-cruscotto   # oppure carica lo zip e scompattalo
cd autoscuola-cruscotto

# 2. Configurazione
cp deploy/.env.example deploy/.env
nano deploy/.env        # DOMINIO, EMAIL, AC_JWT_SECRET, ANTHROPIC_API_KEY

# 3. Generare il segreto JWT
python3 -c "import secrets;print(secrets.token_hex(32))"

# 4. Far puntare il record DNS A del dominio all'IP del server, poi:
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d

# 5. Verifica
curl https://cruscotto.esempio.it/api/salute
docker compose -f deploy/docker-compose.yml logs -f
```

Il certificato viene emesso al primo avvio: se il DNS non è ancora propagato, Caddy riprova finché non ci riesce.

**Aggiornare l'applicazione senza toccare i dati:**

```bash
git pull
docker compose -f deploy/docker-compose.yml up -d --build
```

Il database vive su un volume Docker, non nell'immagine: ricostruire l'immagine non cancella nulla.

**Backup:** il servizio `backup` gira ogni 24 ore e scrive nel volume dati. Per portarli fuori dal server:

```bash
docker compose -f deploy/docker-compose.yml exec applicazione \
  python /app/scripts/backup.py --destinazione /app/data/backup --comprimi
docker cp $(docker compose -f deploy/docker-compose.yml ps -q applicazione):/app/data/backup ./backup-locale
```

---

## Opzione 2 — Server Linux senza Docker

```bash
sudo useradd --system --home /opt/autoscuola-cruscotto cruscotto
sudo mkdir -p /opt/autoscuola-cruscotto
sudo cp -r . /opt/autoscuola-cruscotto/
cd /opt/autoscuola-cruscotto

sudo -u cruscotto python3 -m venv .venv
sudo -u cruscotto .venv/bin/pip install -r backend/requirements.txt

# Segreti in un file separato con permessi restrittivi
sudo mkdir -p /etc/cruscotto
sudo tee /etc/cruscotto/segreti.env > /dev/null <<'EOF'
AC_JWT_SECRET=<32 byte casuali>
ANTHROPIC_API_KEY=<chiave>
EOF
sudo chmod 600 /etc/cruscotto/segreti.env
sudo chown -R cruscotto:cruscotto /opt/autoscuola-cruscotto

sudo cp deploy/cruscotto.service deploy/cruscotto-manutenzione.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cruscotto cruscotto-manutenzione.timer

# HTTPS con nginx + certbot
sudo cp deploy/nginx.conf /etc/nginx/sites-available/cruscotto
sudo sed -i 's/cruscotto.esempio.it/IL-TUO-DOMINIO/g' /etc/nginx/sites-available/cruscotto
sudo ln -s /etc/nginx/sites-available/cruscotto /etc/nginx/sites-enabled/
sudo certbot --nginx -d IL-TUO-DOMINIO
sudo nginx -t && sudo systemctl reload nginx
```

L'unit systemd è irrobustita: utente non privilegiato, filesystem in sola lettura tranne la cartella dati, namespace ristretti. Un bug nell'applicazione non può modificare il proprio stesso codice.

Log: `sudo journalctl -u cruscotto -f`

---

## Opzione 3 — Piattaforme gestite

Il `Dockerfile` è standard e funziona su Railway, Render, Fly.io, Google Cloud Run.

**Attenzione a un punto non negoziabile:** SQLite ha bisogno di un disco persistente. Su piattaforme con filesystem effimero (Cloud Run senza volume, Heroku) il database **viene cancellato a ogni riavvio**, perdendo tutti i progressi degli allievi.

| Piattaforma | Volume persistente | Nota |
|---|---|---|
| Fly.io | `fly volumes create dati --size 3` | Montare su `/app/data` |
| Railway | Volume dal pannello | Montare su `/app/data` |
| Render | Disk (piano a pagamento) | Montare su `/app/data` |
| Cloud Run | Solo con Filestore | Meglio migrare a PostgreSQL |

Se la piattaforma scelta non offre volumi, la strada corretta non è forzare SQLite ma passare a PostgreSQL: le query sono SQL standard e il layer dati è isolato in `backend/app/db.py`.

---

## Lista di controllo prima di andare in produzione

- [ ] `AC_JWT_SECRET` sostituito con 32 byte casuali (il default è pubblico: chiunque potrebbe forgiare token validi)
- [ ] Password degli utenti demo cambiate o utenti disattivati
- [ ] `allow_origins` in `backend/app/main.py` ristretto al proprio dominio (ora è `*`)
- [ ] HTTPS attivo e redirect da HTTP verificato
- [ ] Backup automatico attivo **e ripristino provato almeno una volta** — un backup mai testato non è un backup
- [ ] `AC_AI_RATE` calibrato sul budget mensile accettabile
- [ ] Reaper delle sessioni schedulato (timer systemd o cron)
- [ ] Informativa privacy e registro dei trattamenti: la piattaforma tratta dati di minorenni, il GDPR chiede base giuridica e consenso del genitore per gli under 14

## Risoluzione dei problemi

| Sintomo | Causa più probabile |
|---|---|
| Certificato non emesso | DNS non propagato, oppure porta 80 chiusa dal firewall |
| `database is locked` | Troppi worker in scrittura: ridurre a 2, o passare a PostgreSQL |
| Tutor AI sempre 503 | `ANTHROPIC_API_KEY` non passata al contenitore |
| Progressi persi al riavvio | Volume non montato su `/app/data` |
| PWA non installabile | Serve HTTPS valido: i browser non installano PWA su HTTP |
