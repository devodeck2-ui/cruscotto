"""Configurazione centralizzata (12-factor: tutto da variabili d'ambiente).

I valori vengono letti nel costruttore, non a livello di classe: cosi' una
nuova istanza rilegge sempre l'ambiente corrente. E' quello che permette di
caricare il file .env dopo l'import senza che i valori restino congelati, e
rende gli script di prova capaci di cambiare fornitore al volo.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _int(nome: str, predefinito: int) -> int:
    try:
        return int(os.getenv(nome, predefinito))
    except (TypeError, ValueError):
        return predefinito


class Settings:
    def __init__(self) -> None:
        # --- persistenza ---
        self.db_path = Path(os.getenv("AC_DB", ROOT / "data" / "autoscuola.db"))
        self.media_dir = Path(os.getenv("AC_MEDIA", ROOT / "data" / "media"))
        self.frontend_dir = Path(os.getenv("AC_FRONTEND", ROOT / "frontend"))

        # --- sicurezza ---
        self.jwt_secret = os.getenv("AC_JWT_SECRET", "cambiami-in-produzione-32byte-min")
        self.access_ttl_min = _int("AC_ACCESS_TTL", 30)
        self.refresh_ttl_days = _int("AC_REFRESH_TTL_DAYS", 30)
        self.pbkdf2_iterations = _int("AC_PBKDF2_ITER", 260000)

        # --- AI Tutor -------------------------------------------------------
        # Il tutor parla con due fornitori diversi. La scelta e' automatica in
        # base alla chiave presente, e si forza con AC_AI_PROVIDER.
        #
        #   anthropic : qualita' di riferimento, a consumo
        #   gemini    : piano gratuito di Google AI Studio, legge le immagini
        #
        # Nota sul piano gratuito Gemini: Google puo' usare i contenuti inviati
        # per migliorare i propri prodotti. Le domande sono testi ministeriali
        # pubblici, ma vale la pena saperlo prima di usarlo in autoscuola.
        self.ai_provider = os.getenv("AC_AI_PROVIDER", "auto")

        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.ai_model = os.getenv("AC_AI_MODEL", "claude-sonnet-5")

        self.gemini_api_key = (os.getenv("GEMINI_API_KEY")
                               or os.getenv("GOOGLE_API_KEY", "")).strip()
        # Il nome esatto dei modelli Google cambia spesso: se questo non
        # esiste, il client interroga l'elenco dei modelli disponibili e ne
        # sceglie uno della famiglia Flash, l'unica inclusa nel piano gratuito.
        self.gemini_model = os.getenv("AC_GEMINI_MODEL", "gemini-2.5-flash")

        self.ai_max_tokens = _int("AC_AI_MAX_TOKENS", 700)
        self.prompt_version = "tutor-v1.2"
        self.ai_rate_limit_ora = _int("AC_AI_RATE", 400)   # chiamate/ora per tenant

        # --- sessioni ---
        self.heartbeat_sec = 30
        self.inattivita_sec = 300   # oltre questa soglia il reaper chiude

    # ------------------------------------------------------------------ #

    def provider_attivo(self) -> str | None:
        """Fornitore realmente utilizzabile, in base alle chiavi presenti."""
        scelta = (self.ai_provider or "auto").lower()
        if scelta == "anthropic":
            return "anthropic" if self.anthropic_api_key else None
        if scelta == "gemini":
            return "gemini" if self.gemini_api_key else None
        # auto: con entrambe le chiavi vince Anthropic, che rende meglio
        if self.anthropic_api_key:
            return "anthropic"
        if self.gemini_api_key:
            return "gemini"
        return None

    def modello_attivo(self) -> str | None:
        return {"anthropic": self.ai_model,
                "gemini": self.gemini_model}.get(self.provider_attivo())

    def descrizione_ai(self) -> str:
        p = self.provider_attivo()
        if not p:
            return "non configurato (manca ANTHROPIC_API_KEY o GEMINI_API_KEY)"
        etichetta = {"anthropic": "Claude", "gemini": "Google Gemini"}[p]
        return f"attivo - {etichetta} ({self.modello_attivo()})"

    def ricarica(self) -> "Settings":
        """Rilegge l'ambiente sull'istanza condivisa (usata dagli script)."""
        self.__init__()
        return self


settings = Settings()
