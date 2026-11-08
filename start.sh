#!/usr/bin/env bash
# Grid Advisor: one command to set up and run the whole app (macOS / Linux).
#
#   ./start.sh               set up if needed, then run on http://localhost:8000
#   ./start.sh --offline     use saved data only (no live APIs), e.g. on weak Wi-Fi
#   ./start.sh --no-browser  don't open the browser
#   PORT=9000 ./start.sh     use another port
#
# Needs Python 3.11+. Node.js 20+ is only needed when the web app has to be
# (re)built; if a built copy exists in frontend/dist it is used as-is.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
OPEN_BROWSER=1
for arg in "$@"; do
  case "$arg" in
    --offline) export GRID_ADVISOR_LIVE=0 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }

# --- Python environment -------------------------------------------------------
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.11+ is needed. Install it from https://www.python.org/downloads/"; exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "Python 3.11+ is needed (found $("$PYTHON" --version 2>&1))."
  echo "Install a newer Python, or run with PYTHON=/path/to/python3.12 ./start.sh"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  say "Creating the Python environment (.venv)"
  "$PYTHON" -m venv .venv
fi
VENV_PY=.venv/bin/python
WANTED=$("$VENV_PY" -c "import hashlib; print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())")
STAMP=.venv/.requirements-installed
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$WANTED" ]; then
  say "Installing Python packages (first run takes a few minutes)"
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -r requirements.txt
  echo "$WANTED" > "$STAMP"
fi

# --- Web app --------------------------------------------------------------------
DIST=frontend/dist/index.html
needs_build=0
if [ ! -f "$DIST" ]; then
  needs_build=1
elif [ -n "$(find frontend/src frontend/index.html frontend/package.json frontend/package-lock.json \
             frontend/vite.config.js -newer "$DIST" -print -quit 2>/dev/null)" ]; then
  needs_build=1
fi
if [ "$needs_build" = 1 ]; then
  if command -v npm >/dev/null 2>&1; then
    if [ ! -d frontend/node_modules ] || [ frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]; then
      say "Installing web app packages"
      (cd frontend && npm ci --no-audit --no-fund --loglevel=error)
    fi
    say "Building the web app"
    (cd frontend && npm run build --silent)
  elif [ -f "$DIST" ]; then
    echo "Note: the web app has changes but Node.js isn't installed; using the existing build."
  else
    echo "The web app needs building once, which requires Node.js 20+ (https://nodejs.org/)."; exit 1
  fi
fi

# --- Run ------------------------------------------------------------------------
URL="http://localhost:$PORT"
if [ "${GRID_ADVISOR_LIVE:-1}" = 0 ]; then
  say "Starting Grid Advisor on $URL (offline: saved data only)"
else
  say "Starting Grid Advisor on $URL (live data; first start can take ~10 s). Press Ctrl+C to stop."
fi
if [ "$OPEN_BROWSER" = 1 ]; then
  opener=""
  command -v open >/dev/null 2>&1 && opener=open
  command -v xdg-open >/dev/null 2>&1 && opener=xdg-open
  if [ -n "$opener" ]; then
    # Open the browser once the server answers (it loads live data before opening).
    ( for _ in $(seq 1 120); do
        if "$VENV_PY" -c "import urllib.request; urllib.request.urlopen('$URL/api/health', timeout=1)" 2>/dev/null; then
          "$opener" "$URL" >/dev/null 2>&1; break
        fi
        sleep 0.5
      done ) &
  fi
fi
exec "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
