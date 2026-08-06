#!/usr/bin/env bash
# Autoscuola Cruscotto - avvio su macOS.
# Fare doppio clic su questo file. Se macOS lo blocca, tasto destro > Apri.
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  exec ./.venv/bin/python avvia.py "$@"
fi
for py in python3 python; do
  if command -v $py >/dev/null 2>&1; then exec $py avvia.py "$@"; fi
done
echo
echo "  Python non trovato. Installalo da https://www.python.org/downloads/"
echo "  oppure con:  brew install python"
echo
read -p "  Premi INVIO per chiudere..."
