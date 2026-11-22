#!/usr/bin/env bash
# Single-container deployment for a key-free AutoClip host.
#
# Runs the API, the Celery worker and Redis in one container so a free/hobby
# container host can serve the whole app. Every managed platform needs a single
# "web" process that stays in the foreground and binds $PORT, so the API is
# exec'd at the end and the others are warm background children.
#
# Differences from docker-entrypoint.sh (the compose entrypoint):
#   * Redis runs in-container instead of a separate service.
#   * AUTOCLIP_OFFLINE_ENGINE=local turns on key-free analysis and seeds the
#     model settings, so nothing asks for an API key.
#   * The built frontend is served by this same process, so one URL serves both.
set -euo pipefail
cd /app
export PYTHONPATH=/app PYTHONUNBUFFERED=1

PORT="${PORT:-8000}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export AUTOCLIP_OFFLINE_ENGINE="${AUTOCLIP_OFFLINE_ENGINE:-local}"
export AUTOCLIP_OFFLINE_ENGINE_PORT="${AUTOCLIP_OFFLINE_ENGINE_PORT:-8100}"
export AUTOCLIP_HOST=0.0.0.0
# The hosted frontend is served from this origin; same-origin writes pass the
# origin guard by Host comparison, so no origin list is needed for the app itself.
export AUTOCLIP_ALLOWED_ORIGINS="${AUTOCLIP_ALLOWED_ORIGINS:-}"

log() { printf '[autoclip] %s\n' "$*"; }

mkdir -p data/projects data/uploads data/temp data/output data/cache logs

# ---------------------------------------------------------------- redis ------
if [[ -x /usr/bin/redis-server ]]; then
  log "starting redis on 127.0.0.1:6379"
  redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no \
    --logfile /app/logs/redis.log --daemonize yes || true
  for _ in $(seq 1 30); do
    if redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; then break; fi
    sleep 0.2
  done
else
  log "redis-server missing; progress falls back to SQLite, Celery tasks will not run"
fi

# ------------------------------------------------------------- database ------
python init_database.py || log "database init reported an error (continuing)"

# ---------------------------------------------------------------- worker -----
log "starting celery worker"
celery -A backend.core.celery_app worker \
  --loglevel=info \
  --concurrency="${CELERY_CONCURRENCY:-2}" \
  --prefetch-multiplier=1 \
  -Q celery,processing,video,notification,upload \
  > /app/logs/celery.log 2>&1 &
CELERY_PID=$!
log "celery worker pid=$CELERY_PID"

# ------------------------------------------------------------------ api ------
log "starting api on 0.0.0.0:$PORT"
exec python -m uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips='*'
