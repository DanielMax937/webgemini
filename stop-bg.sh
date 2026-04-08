#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8200}"
PID_FILE="${WG_PID_FILE:-./webgemini.pid}"

chrome_stop() {
  echo "Stopping Chrome (web_gemini.chrome_automation.manager)..."
  env PYTHONPATH=src uv run python -m web_gemini.chrome_automation.manager stop || true
  echo "✓ Chrome stop issued"
}

stopped=0

if PID=$(lsof -ti:"$PORT" 2>/dev/null); then
  echo "Stopping Web Gemini on port $PORT (PID: $PID)..."
  echo "$PID" | xargs kill 2>/dev/null || true
  sleep 2
  if lsof -ti:"$PORT" >/dev/null 2>&1; then
    echo "Force stopping..."
    lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "✓ Web Gemini stopped"
  stopped=1
fi

if [ "$stopped" -eq 0 ] && [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping Web Gemini (PID: $PID)..."
    kill "$PID" 2>/dev/null || true
    sleep 2
    kill -9 "$PID" 2>/dev/null || true
    stopped=1
  fi
  rm -f "$PID_FILE"
  echo "✓ Web Gemini stopped"
fi

chrome_stop

if [ "$stopped" -eq 1 ]; then
  exit 0
fi

echo "No process found on port $PORT. Web Gemini not running."
exit 0
