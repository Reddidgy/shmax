#!/bin/bash

# Starts the FastAPI backend (venv/bin/python -m uvicorn app.main:app, run from backend/) in the
# background via nohup. Config: repo-root .env (shell KEY=value syntax, see .env.example), plus
# safe defaults for a VPS behind nginx. Safe to re-run: a second instance is never started.
# Log: logs/api_shmax.log
# fetcher_shmax.sh stops the old process (by PID) and calls this script on backend updates.
# Stop: kill "$(cat run/api_shmax.pid)"  (a running fetcher restarts it within ~30 s)

cd "$(dirname "$(realpath "$0")")" || exit 1
repo_root=$(pwd)
mkdir -p logs run
pid_file=run/api_shmax.pid

if [ -f "$pid_file" ]; then
  pid=$(cat "$pid_file")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= | grep -q "app.main:app"; then
    echo "API is already running (PID $pid)."
    exit 0
  fi
fi

if [ ! -x venv/bin/python ]; then
  echo "venv/bin/python not found; run nohup_fetcher_shmax.sh first (it creates the venv)." >&2
  exit 1
fi

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
else
  echo "WARNING: $repo_root/.env not found; using defaults (see .env.example)." >&2
fi

# Loopback only (nginx is the public entry point).
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8100}"
export ROOT_PATH="${ROOT_PATH:-/api}"

# cd backend so pydantic-settings finds backend/.env and relative paths behave; exec is required
# so the recorded PID is the uvicorn process itself (not a subshell), which is what the
# stale-PID check above (and fetcher_shmax.sh) greps via `ps -p <pid> -o args=`.
nohup env PYTHONUNBUFFERED=1 sh -c 'cd backend && exec ../venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --root-path "$ROOT_PATH" --proxy-headers --forwarded-allow-ips 127.0.0.1' >> logs/api_shmax.log 2>&1 &
echo $! > "$pid_file"
echo "API started (PID $!, $HOST:$PORT), log: logs/api_shmax.log"
