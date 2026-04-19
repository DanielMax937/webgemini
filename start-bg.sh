#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8200}"
PID_FILE="${WG_PID_FILE:-./webgemini.pid}"
LOG_FILE="${WG_LOG_FILE:-./webgemini.log}"
START_TIMEOUT="${WG_START_TIMEOUT:-20}"
HEALTH_URL="${WG_HEALTH_URL:-http://127.0.0.1:${PORT}/health}"
WG_TASK_TIMEOUT_S="${WG_TASK_TIMEOUT_S:-3000}"

chrome_start() {
  echo "Starting Chrome (bundled web_gemini.chrome_automation.manager, CDP 9222)..."
  env PYTHONPATH=src uv run python -m web_gemini.chrome_automation.manager start
  echo "✓ Chrome ready at http://127.0.0.1:9222 (profile: ./chrome_data/chrome-profile)"
}

cleanup_stale_pid() {
  if [ -f "$PID_FILE" ]; then
    local old_pid
    old_pid=$(cat "$PID_FILE")
    if kill -0 "$old_pid" 2>/dev/null; then
      echo "Service already running (PID: $old_pid)"
      echo "Stop it first with: ./stop-bg.sh"
      exit 1
    fi
    echo "Removing stale PID file..."
    rm -f "$PID_FILE"
  fi
}

wait_until_ready() {
  local service_pid="$1"
  local elapsed=0
  while [ "$elapsed" -lt "$START_TIMEOUT" ]; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "$service_pid" 2>/dev/null; then
      break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

start_detached() {
  python3 - "$LOG_FILE" <<'PY'
import os
import subprocess
import sys

log_file = sys.argv[1]
env = os.environ.copy()
env["PYTHONPATH"] = "src"
with open(log_file, "ab", buffering=0) as log:
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "web_gemini.main:app", "--host", "0.0.0.0", "--port", env.get("PORT", "8200")],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
print(proc.pid)
PY
}

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT already in use. Stop the service first with: ./stop-bg.sh"
  exit 1
fi

cleanup_stale_pid

chrome_start

echo "──────────────────────────────────────────"
echo "  Web Gemini (FastAPI)"
echo ""
echo "  URL:      http://127.0.0.1:${PORT}"
echo "  Docs:     http://127.0.0.1:${PORT}/docs"
echo "  Log file: $LOG_FILE"
echo "  Timeout:  WG_TASK_TIMEOUT_S=${WG_TASK_TIMEOUT_S}"
echo "──────────────────────────────────────────"

SERVICE_PID="$(PORT="$PORT" WG_TASK_TIMEOUT_S="$WG_TASK_TIMEOUT_S" start_detached)"
echo "$SERVICE_PID" > "$PID_FILE"

if ! wait_until_ready "$SERVICE_PID"; then
  echo ""
  echo "Service failed to become ready within ${START_TIMEOUT}s."
  kill "$SERVICE_PID" 2>/dev/null || true
  sleep 1
  if kill -0 "$SERVICE_PID" 2>/dev/null; then
    kill -9 "$SERVICE_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "Recent logs:"
  tail -n 40 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo ""
echo "✓ Service started (PID: $SERVICE_PID)"
echo ""
echo "Commands:"
echo "  View logs:  tail -f $LOG_FILE"
echo "  Stop:       ./stop-bg.sh"
