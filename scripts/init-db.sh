#!/usr/bin/env bash
# 初始化 webgemini 的 PostgreSQL 数据库
# 用法: ./scripts/init-db.sh

set -euo pipefail
cd "$(dirname "$0")/.."

export PGDATABASE="${PGDATABASE:-webgemini}"
export PGUSER="${PGUSER:-$(whoami)}"

echo "Creating database '$PGDATABASE' if not exists..."
psql -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$PGDATABASE'" | grep -q 1 || \
  psql -d postgres -c "CREATE DATABASE $PGDATABASE"

echo "Initializing tables..."
env PYTHONPATH=src uv run python -c "
from web_gemini.db import init_db
init_db()
print('Done.')
"
