#!/usr/bin/env bash
# Autoscuola Cruscotto - avvio su Linux.
#   ./avvia.sh                  avvio normale (rete locale + browser)
#   ./avvia.sh --solo-locale    solo su questa macchina
#   ./avvia.sh --porta 9000
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  exec ./.venv/bin/python avvia.py "$@"
fi
for py in python3 python; do
  if command -v $py >/dev/null 2>&1; then exec $py avvia.py "$@"; fi
done
echo "Python 3.9+ non trovato. Su Debian/Ubuntu:  sudo apt install python3 python3-venv"
exit 1
