#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8200}"
PID_FILE="${WG_PID_FILE:-./webgemini.pid}"
LOG_FILE="${WG_LOG_FILE:-./webgemini.log}"

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT already in use. Stop the service first with: ./stop-bg.sh"
  exit 1
fi

echo "──────────────────────────────────────────"
echo "  Web Gemini (FastAPI)"
echo ""
echo "  URL:      http://127.0.0.1:${PORT}"
echo "  Docs:     http://127.0.0.1:${PORT}/docs"
echo "  Log file: $LOG_FILE"
echo "──────────────────────────────────────────"

nohup env PYTHONPATH=src uv run uvicorn web_gemini.main:app --host 0.0.0.0 --port "$PORT" >> "$LOG_FILE" 2>&1 &
SERVICE_PID=$!
echo $SERVICE_PID > "$PID_FILE"

echo ""
echo "✓ Service started (PID: $SERVICE_PID)"
echo ""
echo "Commands:"
echo "  View logs:  tail -f $LOG_FILE"
echo "  Stop:       ./stop-bg.sh"
