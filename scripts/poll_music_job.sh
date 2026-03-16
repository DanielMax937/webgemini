#!/usr/bin/env bash
# 轮询 /music/{job_id} 直到完成，最多 100 次
set -euo pipefail

JOB_ID="${1:?Usage: $0 <job_id>}"
BASE_URL="${MUSIC_BASE_URL:-http://127.0.0.1:8200}"
MAX_POLLS=100
INTERVAL=5

for i in $(seq 1 "$MAX_POLLS"); do
  RES=$(curl -s "$BASE_URL/music/$JOB_ID")
  STATUS=$(echo "$RES" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
  echo "[$i/$MAX_POLLS] status=$STATUS"

  if [[ "${VERBOSE:-0}" == "1" ]]; then
    echo "$RES" | python3 -m json.tool
  fi
  if [[ "$STATUS" == "completed" ]]; then
    echo ""
    echo "✓ 完成"
    echo "$RES" | python3 -m json.tool
    exit 0
  fi
  if [[ "$STATUS" == "failed" ]]; then
    echo ""
    echo "✗ 失败"
    echo "$RES" | python3 -m json.tool
    exit 1
  fi

  [[ $i -lt $MAX_POLLS ]] && sleep "$INTERVAL"
done

echo ""
echo "✗ 超时 (已轮询 $MAX_POLLS 次)"
exit 2
