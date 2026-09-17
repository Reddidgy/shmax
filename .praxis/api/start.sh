#!/usr/bin/env bash
# Praxis Local API launcher.
# Finds Python 3.10+, creates a venv, installs deps, and starts the server.
# Usage: bash .praxis/api/start.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
API_SCRIPT="$SCRIPT_DIR/praxis_wrapper.py"
MIN_PYTHON_MINOR=10

# --- Find a Python >= 3.10 ---------------------------------------------------
find_python() {
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    local path
    path="$(command -v "$candidate" 2>/dev/null)" || continue
    local ver
    ver="$("$path" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null)" || continue
    if [ "$ver" -ge "$MIN_PYTHON_MINOR" ] 2>/dev/null; then
      echo "$path"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)" || {
  echo "ERROR: Python 3.10 or newer is required but not found."
  echo ""
  echo "Your system Python is too old for the MCP dependencies."
  echo "Install a newer Python, for example:"
  echo "  brew install python"
  echo ""
  echo "Then run this script again."
  exit 1
}

PYTHON_VER="$("$PYTHON" --version 2>&1)"
echo "Using $PYTHON_VER ($PYTHON)"

# --- Create venv if needed ----------------------------------------------------
if [ ! -f "$VENV_DIR/bin/python3" ]; then
  echo "Creating virtual environment..."
  "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip3"

# --- Install / update deps ---------------------------------------------------
if [ ! -f "$VENV_DIR/.deps_installed" ] || [ "$REQ_FILE" -nt "$VENV_DIR/.deps_installed" ]; then
  echo "Installing dependencies..."
  "$VENV_PIP" install -q -r "$REQ_FILE" && touch "$VENV_DIR/.deps_installed"
fi

# --- Ensure .mcp.json has the praxis MCP entry --------------------------------
# Derive the project root (mirrors _derive_project_root in praxis_local_api.py).
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
if [ "$(basename "$PARENT_DIR")" = ".praxis" ]; then
  PROJECT_ROOT="$(dirname "$PARENT_DIR")"
else
  PROJECT_ROOT="$PARENT_DIR"
fi

MCP_JSON="$PROJECT_ROOT/.mcp.json"
MCP_PORT="${LOCAL_API_PORT:-${CHAT_API_PORT:-7865}}"

"$VENV_PYTHON" "$SCRIPT_DIR/provision_mcp_json.py" "$MCP_JSON" "$MCP_PORT"

# --- Kill stale instance if pidfile exists ------------------------------------
PID_FILE="$SCRIPT_DIR/praxis.pid"
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping previous instance (PID $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null || true
    # Wait up to 5 seconds for graceful shutdown
    for i in $(seq 1 50); do
      kill -0 "$OLD_PID" 2>/dev/null || break
      sleep 0.1
    done
    # Force kill if still alive
    kill -0 "$OLD_PID" 2>/dev/null && (kill -9 "$OLD_PID" 2>/dev/null || true)
  fi
  rm -f "$PID_FILE"
fi

# --- Rotate an oversized log before launch (POS-1995) -------------------------
# The RotatingFileHandler inside the Python processes caps praxis.log at 5 MB,
# but an unclean shutdown can leave an oversized file behind. Roll it here so a
# fresh session always starts inside the size budget.
LOG_FILE="$SCRIPT_DIR/praxis.log"
LOG_MAX_BYTES=5242880
if [ -f "$LOG_FILE" ]; then
  LOG_SIZE="$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d '[:space:]' || echo 0)"
  case "$LOG_SIZE" in
    ''|*[!0-9]*) LOG_SIZE=0 ;;
  esac
  if [ "$LOG_SIZE" -ge "$LOG_MAX_BYTES" ]; then
    echo "Rotating oversized log ($LOG_SIZE bytes) -> praxis.log.1"
    mv -f "$LOG_FILE" "$LOG_FILE.1"
  fi
fi

# --- Start the API in the background -----------------------------------------
# Append (>>), never truncate (>): the Python RotatingFileHandler owns this file
# now. This redirect only catches raw-fd output from a bootstrap failure before
# Python logging is configured.
echo ""
nohup "$VENV_PYTHON" "$API_SCRIPT" >> "$LOG_FILE" 2>&1 &
WRAPPER_PID=$!
echo "$WRAPPER_PID" > "$PID_FILE"

echo "Praxis Local API started in background (PID $WRAPPER_PID)"
echo "Log file: $LOG_FILE (rotates at 5 MB, 2 backups kept)"
echo "To stop: kill $WRAPPER_PID  (or use the UI stop button)"
echo ""
echo "Return to the PraxisOS application!"
