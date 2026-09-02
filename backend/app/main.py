"""Autoscuola Cruscotto - applicazione FastAPI.

Il processo espone tre superfici:
  * /api/...   API JSON versionabile, consumata dalla PWA
  * /media/... immagini del listato, servite con cache immutabile
  * /          la PWA statica (in produzione la servirebbe un CDN/nginx)

Il monolite modulare e' una scelta deliberata: con questi volumi (decine di
autoscuole, migliaia di allievi) i microservizi aggiungerebbero latenza e
complessita' operativa senza alcun beneficio. La modularizzazione per router
e service rende comunque banale estrarre in futuro i due candidati naturali:
il servizio AI (I/O-bound, scala in modo indipendente) e il job analitico.
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import settings
from .routers import (amministrazione, assistente, auth, catalogo, gestione,
                      notifiche, patenti, quiz, scuola, sessioni, statistiche,
                      tutoraggio, video)

app = FastAPI(title="Autoscuola Cruscotto API", version="1.0.0",
              description="Gestionale didattico per autoscuole: quiz ministeriali, "
                          "videocorsi, AI Tutor e analitiche.")

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    # "*" in sviluppo/demo. In produzione impostare AC_CORS_ORIGINS con il
    # proprio dominio (es. https://cruscotto.tuaautoscuola.it) - vedi deploy/README.md.
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def intestazioni_sicurezza(request: Request, call_next):
    inizio = time.perf_counter()
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Tempo-ms"] = f"{(time.perf_counter() - inizio) * 1000:.1f}"
    return resp


@app.exception_handler(Exception)
async def errore_generico(request: Request, exc: Exception):
    # Nessun traceback verso il client: si logga lato server e si risponde in modo neutro.
    return JSONResponse(status_code=500, content={"detail": "Errore interno del server"})


for r in (auth.router, catalogo.router, quiz.router, statistiche.router,
          tutoraggio.router, amministrazione.router, sessioni.router, video.router,
          gestione.router, assistente.router, scuola.router, patenti.router,
          notifiche.router):
    app.include_router(r)


@app.get("/api/salute", tags=["sistema"])
def salute():
    d = db.query_one("SELECT (SELECT COUNT(*) FROM domande) AS domande,"
                     " (SELECT COUNT(*) FROM listati) AS listati,"
                     " (SELECT COUNT(*) FROM utenti) AS utenti")
    return {"stato": "ok", "database": dict(d),
            "ai_configurata": bool(settings.provider_attivo())}


if settings.media_dir.exists():
    app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")

if settings.frontend_dir.exists():
    app.mount("/app", StaticFiles(directory=settings.frontend_dir, html=True), name="app")

    @app.get("/", include_in_schema=False)
    def root():
        return FileResponse(settings.frontend_dir / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        # Il service worker deve essere servito dalla root per controllare l'intero scope.
        return FileResponse(settings.frontend_dir / "sw.js", media_type="application/javascript")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(settings.frontend_dir / "manifest.webmanifest",
                            media_type="application/manifest+json")
