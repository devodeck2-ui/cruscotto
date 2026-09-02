#!/usr/bin/env python3
"""Genera una coppia di chiavi VAPID per le notifiche push.

Non serve nessun account su nessun servizio: le chiavi identificano solo
questo server presso i servizi push dei browser (Google, Mozilla...), e si
generano in locale con la libreria "cryptography".

    python3 scripts/genera_chiavi_vapid.py

Il risultato va incollato in .env (AC_VAPID_PUBLIC_KEY / AC_VAPID_PRIVATE_KEY).
Va fatto una volta sola: rigenerarle invaliderebbe le iscrizioni push gia'
salvate dai dispositivi degli allievi, che dovrebbero re-iscriversi.
"""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(dato: bytes) -> str:
    return base64.urlsafe_b64encode(dato).rstrip(b"=").decode()


def main() -> None:
    privata = ec.generate_private_key(ec.SECP256R1())
    numeri_privati = privata.private_numbers()
    numeri_pubblici = privata.public_key().public_numbers()

    chiave_privata = numeri_privati.private_value.to_bytes(32, "big")
    punto_pubblico = (b"\x04"
                      + numeri_pubblici.x.to_bytes(32, "big")
                      + numeri_pubblici.y.to_bytes(32, "big"))

    print("AC_VAPID_PUBLIC_KEY=" + _b64url(punto_pubblico))
    print("AC_VAPID_PRIVATE_KEY=" + _b64url(chiave_privata))
    print("AC_VAPID_SUBJECT=mailto:info@tuaautoscuola.it")


if __name__ == "__main__":
    main()
