"""Utilita' condivise dagli script di manutenzione.

Tutti gli script sono eseguibili sia a mano sia da cron, accettano --dry-run
dove l'operazione e' distruttiva e scrivono su stdout in formato leggibile
(una riga per evento) cosi' che l'output finisca nei log di sistema senza
bisogno di configurare un logger.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "backend"))

# Gli script possono girare su un database diverso da quello di default
# (utile per provare una manutenzione su una copia prima di applicarla).
os.environ.setdefault("AC_DB", str(RADICE / "data" / "autoscuola.db"))
os.environ.setdefault("AC_MEDIA", str(RADICE / "data" / "media"))


def log(messaggio: str) -> None:
    ora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ora}] {messaggio}", flush=True)


def percorso_db() -> Path:
    return Path(os.environ["AC_DB"])
