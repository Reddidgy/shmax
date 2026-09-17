#!/bin/bash

# Server-side updater for shmax (runs on the Oracle VPS; start it with nohup_fetcher_shmax.sh).
# Only postgres and redis are containerised (docker-compose.prod.yml, containers shmax-postgres /
# shmax-redis); the FastAPI backend runs natively from venv/ and is supervised here.
# Every 5 s: `git fetch origin`; when the remote moved, pull (or hard-reset if the branch diverged).
# When the pull touched the API, reinstall its requirements into venv/ and restart it by PID. When
# the pull touched the compose file, re-run `docker compose up -d` to pick up the change.
# It also keeps the API alive: a missing API process is started again (at most every 30 s).
# Frontend releases are NOT handled here — deploy.sh uploads them from the developer machine.

script_path=$(realpath "$0")
repo_root=$(dirname "$script_path")
cd "$repo_root" || { echo "Failed to change directory to script location"; exit 1; }

RUN_DIR="$repo_root/run"
API_PID_FILE="$RUN_DIR/api_shmax.pid"
VENV_DIR="$repo_root/venv"
REQUIREMENTS="backend/requirements.txt"
API_ENTRYPOINT="app.main:app"
COMPOSE_FILE="docker-compose.prod.yml"
# A pull that changes any of these paths triggers `pip install` + an API restart. Other commits
# (docs, .praxis tasks, frontend sources) leave the running API and its in-flight requests alone.
API_PATHS=(backend nohup_api_shmax.sh)
# A pull that changes any of these paths re-runs `docker compose up -d`.
COMPOSE_PATHS=(docker-compose.prod.yml)
POLL_SECONDS=5
API_START_BACKOFF_SECONDS=30
last_api_start=0

mkdir -p "$RUN_DIR" "$repo_root/logs"

log() {
    printf '%s [fetcher] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$1"
}

# Brings up postgres/redis via docker compose. Never aborts the fetcher on failure: the API and
# the git-polling loop must keep running even if Docker is temporarily unavailable.
ensure_infra() {
    if ! command -v docker >/dev/null 2>&1; then
        log "ERROR: docker not found; cannot start ${COMPOSE_FILE} services."
        return 0
    fi
    log "Ensuring infra containers are up (docker compose -f $COMPOSE_FILE up -d)..."
    if docker compose -f "$COMPOSE_FILE" up -d; then
        log "Infra containers are up."
    else
        log "ERROR: docker compose up -d failed; continuing without aborting the fetcher."
    fi
}

# Prints the first Python >= 3.9 that can create a venv (PYTHON_BIN overrides the search).
find_python() {
    if [ -n "${PYTHON_BIN:-}" ]; then
        echo "$PYTHON_BIN"
        return 0
    fi
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# One-time: create venv/ if it does not exist yet.
ensure_venv() {
    if [ -x "$VENV_DIR/bin/python" ] && [ -x "$VENV_DIR/bin/pip" ]; then
        return 0
    fi
    local python_bin
    if ! python_bin=$(find_python); then
        log "ERROR: no Python >= 3.9 with venv/ensurepip found. Install one or set PYTHON_BIN."
        return 1
    fi
    log "Creating virtual environment at $VENV_DIR with $python_bin ($("$python_bin" --version 2>&1))..."
    "$python_bin" -m venv "$VENV_DIR"
}

install_deps() {
    log "Installing API requirements (venv/bin/pip install -r $REQUIREMENTS)..."
    "$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check -r "$REQUIREMENTS"
}

# Prints the PID of the running API, or returns 1. The command-line check guards against a stale
# PID file whose PID was reused by an unrelated process.
api_pid() {
    local pid
    [ -f "$API_PID_FILE" ] || return 1
    pid=$(cat "$API_PID_FILE" 2>/dev/null)
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -q "$API_ENTRYPOINT" || return 1
    echo "$pid"
}

stop_api() {
    local pid
    if ! pid=$(api_pid); then
        rm -f "$API_PID_FILE"
        return 0
    fi
    log "Stopping API (PID $pid)..."
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
        log "API did not exit after 10 s; sending SIGKILL."
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$API_PID_FILE"
}

start_api() {
    last_api_start=$(date +%s)
    log "Starting API..."
    "$repo_root/nohup_api_shmax.sh" 2>&1 | while IFS= read -r line; do log "$line"; done
}

restart_api() {
    stop_api
    start_api
}

# Supervisor duty (no systemd): start the API if its process is gone, with a back-off so a
# crashing build does not respawn every poll.
ensure_api_running() {
    api_pid >/dev/null && return 0
    [ -x "$VENV_DIR/bin/python" ] || return 0
    local now
    now=$(date +%s)
    if [ $((now - last_api_start)) -lt "$API_START_BACKOFF_SECONDS" ]; then
        return 0
    fi
    log "API is not running."
    start_api
}

if ! command -v git >/dev/null 2>&1; then
    log "FATAL: git could not be found. Please install git."
    exit 1
fi

BRANCH_NAME=$(git branch --show-current)
if [ -z "$BRANCH_NAME" ]; then
    log "FATAL: failed to determine the current branch. Ensure this is a valid git repository."
    exit 1
fi

log "Monitoring $repo_root on branch $BRANCH_NAME (PID $$)."

ensure_infra

if ensure_venv && install_deps; then
    ensure_api_running
else
    log "ERROR: environment setup failed; retrying on the next API change."
fi

while true; do
    if ! git fetch --quiet origin; then
        log "WARN: git fetch failed. Retrying in 15 s..."
        sleep 15
        continue
    fi

    LOCAL=$(git rev-parse @)
    REMOTE=$(git rev-parse "@{u}")
    BASE=$(git merge-base @ "@{u}")
    updated=0

    if [ "$LOCAL" = "$REMOTE" ]; then
        :
    elif [ "$LOCAL" = "$BASE" ]; then
        log "Remote has new commits; pulling..."
        if git pull --rebase --quiet origin "$BRANCH_NAME"; then
            updated=1
        else
            log "ERROR: git pull --rebase failed. Manual intervention may be required."
        fi
    else
        log "Local branch diverged from origin/$BRANCH_NAME; resetting to match remote..."
        if git reset --hard --quiet "origin/$BRANCH_NAME"; then
            updated=1
        else
            log "ERROR: git reset --hard failed. Manual intervention may be required."
        fi
    fi

    if [ "$updated" = 1 ]; then
        log "Now at $(git log -1 --format='%h %s')"

        if ! git diff --quiet "$LOCAL" HEAD -- "${COMPOSE_PATHS[@]}"; then
            log "${COMPOSE_FILE} changed; re-running docker compose up -d."
            ensure_infra
        fi

        if git diff --quiet "$LOCAL" HEAD -- "${API_PATHS[@]}"; then
            log "No API changes; restart skipped."
        elif ensure_venv && install_deps; then
            restart_api
        else
            log "ERROR: dependency install failed; the API keeps running the previous code."
        fi

        if ! git diff --quiet "$LOCAL" HEAD -- fetcher_shmax.sh; then
            log "fetcher_shmax.sh changed; re-executing the new version."
            exec "$script_path"
        fi
    fi

    ensure_api_running
    sleep "$POLL_SECONDS"
done
