"""Password hashing, JWT minimale (HS256) e utilita' crittografiche.

Si evita la dipendenza da librerie esterne (python-jose/passlib) implementando
lo stretto necessario sopra hashlib/hmac: meno superficie d'attacco, nessun
problema di supply chain, e il formato resta interoperabile con qualsiasi
client JWT standard.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from .config import settings

# --------------------------------------------------------------------------- #
# Password
# --------------------------------------------------------------------------- #

def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, settings.pbkdf2_iterations)
    return f"pbkdf2_sha256${settings.pbkdf2_iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)   # confronto a tempo costante
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# JWT HS256
# --------------------------------------------------------------------------- #

def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def create_token(payload: dict, ttl_seconds: int) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + ttl_seconds, "jti": secrets.token_hex(8)}
    seg = f"{_b64e(json.dumps(header, separators=(',', ':')).encode())}." \
          f"{_b64e(json.dumps(body, separators=(',', ':')).encode())}"
    sig = hmac.new(settings.jwt_secret.encode(), seg.encode(), hashlib.sha256).digest()
    return f"{seg}.{_b64e(sig)}"


def decode_token(token: str) -> dict | None:
    try:
        h, p, s = token.split(".")
        expected = hmac.new(settings.jwt_secret.encode(), f"{h}.{p}".encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(s), expected):
            return None
        body = json.loads(_b64d(p))
        if body.get("exp", 0) < time.time():
            return None
        return body
    except Exception:
        return None


def hash_opaque(value: str) -> str:
    """Hash per refresh token e IP (GDPR: nessun dato identificativo in chiaro)."""
    return hashlib.sha256((value + settings.jwt_secret).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Link firmati per i file riservati (videolezioni)
# --------------------------------------------------------------------------- #
# Un tag <video> non puo' spedire l'intestazione di autenticazione: il browser
# chiede il file e basta. Per non lasciare le videolezioni scaricabili da
# chiunque conosca l'indirizzo, il server allega alla riga della lezione un
# indirizzo firmato che scade: valido per chi ha appena fatto il login,
# inutile fra qualche ora se il link finisce in una chat.
# Le immagini dei quiz restano libere: sono materiale ministeriale pubblico e
# devono poter stare nella cache del browser.
DURATA_LINK_MEDIA = 6 * 3600


def firma_media(percorso: str, ttl: int = DURATA_LINK_MEDIA) -> str:
    """Restituisce la firma da mettere in coda all'indirizzo del file."""
    scadenza = int(time.time()) + ttl
    corpo = f"{percorso}|{scadenza}"
    sig = hmac.new(settings.jwt_secret.encode(), corpo.encode(), hashlib.sha256).digest()
    return f"{scadenza}.{_b64e(sig)[:32]}"


def verifica_firma_media(percorso: str, firma: str) -> bool:
    try:
        scadenza_txt, _ = firma.split(".", 1)
        if int(scadenza_txt) < time.time():
            return False
        # Si rifa' il conto con la scadenza dichiarata nella firma: quella
        # stessa scadenza fa parte del testo firmato, quindi non e' modificabile.
        corpo = f"{percorso}|{int(scadenza_txt)}"
        sig = hmac.new(settings.jwt_secret.encode(), corpo.encode(), hashlib.sha256).digest()
        atteso = f"{int(scadenza_txt)}.{_b64e(sig)[:32]}"
        return hmac.compare_digest(atteso, firma)
    except Exception:
        return False


def url_media_firmato(url: str) -> str:
    """Aggiunge la firma agli indirizzi dei nostri video; lascia stare il resto
    (YouTube, Drive, Vimeo: sono link esterni, non li serviamo noi)."""
    if not url or not url.startswith("/media/video/"):
        return url
    return f"{url}?f={firma_media(url)}"
