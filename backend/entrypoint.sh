#!/usr/bin/env sh
set -e

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  alembic -c backend/alembic.ini upgrade head
fi

# Cloudflare snapshot deployments must not serve traffic after an uncertain
# or incomplete bootstrap. Other installations retain the legacy behavior.
if [ "${OPENTERMINALUI_CF_CONTAINER:-0}" = "1" ] && [ "${OPENTERMINALUI_DATABASE_STORE:-}" = "d1_snapshot" ]; then
  python scripts/seed_admin.py
else
  python scripts/seed_admin.py || true
fi

exec python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
