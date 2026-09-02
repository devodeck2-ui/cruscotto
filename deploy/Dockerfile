# =============================================================================
# Autoscuola Cruscotto - immagine di produzione
#
# Build a due stadi: le dipendenze si compilano nel primo stadio e nel secondo
# arriva solo il risultato. L'immagine finale non contiene compilatori ne'
# cache di pip, il che significa meno peso e meno superficie d'attacco.
#
#   docker build -f deploy/Dockerfile -t cruscotto .
#   docker run -p 8080:8080 -v cruscotto-dati:/app/data cruscotto
# =============================================================================

FROM python:3.11-slim AS costruzione

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
# Le librerie dell'ETL (pdfplumber, pymupdf) servono solo a ricostruire il
# catalogo, non a servire le richieste: si escludono dall'immagine runtime.
RUN grep -viE '^(pdfplumber|pymupdf)' requirements.txt > runtime.txt \
 && pip install --prefix=/installazione -r runtime.txt


FROM python:3.11-slim

# Utente non privilegiato: se un giorno l'applicazione venisse compromessa,
# l'attaccante non si ritrova root nel contenitore.
RUN useradd --create-home --uid 10001 cruscotto

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend \
    AC_DB=/app/data/autoscuola.db \
    AC_MEDIA=/app/data/media \
    AC_FRONTEND=/app/frontend \
    PORT=8080

WORKDIR /app
COPY --from=costruzione /installazione /usr/local
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY etl/ ./etl/
COPY scripts/ ./scripts/
COPY data/ ./data/
COPY deploy/avvio.sh /usr/local/bin/avvio.sh

# Copia demo fuori da /app/data: quel percorso viene coperto dal volume dei
# dati, quindi il seme deve stare altrove per restare raggiungibile sempre,
# anche quando il volume esiste gia' ma e' privo del database di lavoro.
RUN mkdir -p /app/seme \
 && cp /app/data/autoscuola.demo.db /app/seme/autoscuola.demo.db \
 && chmod +x /usr/local/bin/avvio.sh

RUN chown -R cruscotto:cruscotto /app
USER cruscotto

EXPOSE 8080

# Healthcheck reale: interroga l'endpoint che tocca il database, non la sola
# porta TCP. Un processo vivo con database irraggiungibile deve risultare malato.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/salute',timeout=4).status==200 else 1)"

# L'entrypoint prepara il database (copia demo al primo avvio) e poi passa
# il controllo al CMD qui sotto.
ENTRYPOINT ["/usr/local/bin/avvio.sh"]

# 4 worker: sufficienti per qualche centinaio di utenti concorrenti. Con SQLite
# non ha senso andare oltre, perche' le scritture restano serializzate.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]
