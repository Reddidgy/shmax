#!/bin/bash

# Starts fetcher_shmax.sh in the background via nohup (survives logout). Safe to re-run: a second
# fetcher is never started. Log: logs/fetcher_shmax.log
# Stop: kill "$(cat run/fetcher_shmax.pid)"  (the API keeps running; stop it separately)

cd "$(dirname "$(realpath "$0")")" || exit 1
mkdir -p logs run
pid_file=run/fetcher_shmax.pid

if [ -f "$pid_file" ]; then
  pid=$(cat "$pid_file")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= | grep -q "fetcher_shmax.sh"; then
    echo "fetcher_shmax.sh is already running (PID $pid)."
    exit 0
  fi
fi

nohup ./fetcher_shmax.sh >> logs/fetcher_shmax.log 2>&1 &
echo $! > "$pid_file"
echo "fetcher_shmax.sh started (PID $!), log: logs/fetcher_shmax.log"
