#!/usr/bin/env bash
# Jarvis Trading Portal - Mac/Linux launcher. PAPER TRADING ONLY.
#   ./start_jarvis.sh            start with real prices (Coinbase for crypto, Yahoo for stocks)
#   ./start_jarvis.sh --demo     start with simulated prices (labelled DEMO everywhere)
# What it does:
#   1. finds Python 3.10 or newer
#   2. makes a private Python folder (.venv) here, so nothing is installed system-wide
#   3. installs requirements.txt the first time (and again only if that file changes)
#   4. runs: python server.py   (your browser opens by itself; Ctrl+C stops Jarvis)

cd "$(dirname "$0")" || exit 1

echo
echo "  =================================================="
echo "    JARVIS TRADING PORTAL  -  paper trading only"
echo "  =================================================="
echo

# ---- 1. find Python 3.10+ ----
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "  Python 3.10 or newer was not found."
  echo "    Mac:   install it from https://www.python.org/downloads/  (or: brew install python)"
  echo "    Linux: install python3 with your package manager, e.g. sudo apt install python3 python3-venv"
  echo "  Then run ./start_jarvis.sh again."
  exit 1
fi

# ---- 2. private Python folder (.venv); fall back to the system Python if venv is missing ----
if [ ! -x ".venv/bin/python" ]; then
  echo "  Making a private Python folder (.venv) - first run only..."
  "$PY" -m venv .venv >/dev/null 2>&1 || rm -rf .venv
fi
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  echo "  Note: could not make .venv (on Debian/Ubuntu: sudo apt install python3-venv). Using the system Python."
fi
echo "  Using: $("$PY" --version 2>&1)"
echo

# ---- 3. install add-ons once; the marker is a copy of requirements.txt ----
MARK=".requirements_installed.txt"
if ! cmp -s requirements.txt "$MARK"; then
  echo "  Installing the add-ons Python needs. This takes about a minute..."
  if "$PY" -m pip install --disable-pip-version-check -q -r requirements.txt; then
    cp requirements.txt "$MARK"
    echo "  Add-ons installed."
  else
    echo
    echo "  WARNING: installing the add-ons did not finish. Check your internet connection."
    echo "  Jarvis will try to start anyway. To retry by hand:  $PY -m pip install -r requirements.txt"
  fi
  echo
fi

# ---- 4. brain mode ----
if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "  AI brain: API key found."
else
  echo "  AI brain: no ANTHROPIC_API_KEY found, so Jarvis starts in OFFLINE mode."
  echo "  Offline mode still does status, report, halt, pause, prices and BUY/WAIT answers."
  echo "  To add a key, see README.md - section \"Add your API key\"."
fi
echo
echo "  Starting... your browser opens by itself. Use Chrome or Edge for voice."
echo "  Keep this window open while you use Jarvis. Press Ctrl+C to stop it."
echo

"$PY" server.py "$@"
RC=$?
if [ "$RC" -ne 0 ] && [ "$RC" -ne 130 ] && [ "$RC" -ne 143 ]; then
  echo
  echo "  Jarvis stopped with an error (code $RC). Read the message above."
  echo "  Common fixes:"
  echo "    - 'Address already in use': Jarvis or the old HUD is already running. Stop it, or set a new"
  echo "      port in config/jarvis_config.json."
  echo "    - 'No module named ...': run  $PY -m pip install -r requirements.txt"
  echo "    - Anything else: see README.md - section \"Troubleshooting\"."
fi
exit "$RC"
