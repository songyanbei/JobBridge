#!/bin/sh
set -eu

# Closed-loop sustained acceptance. Production example:
#   LOAD_CONCURRENCY="$((2 * C_peak))" LOAD_DURATION_SECONDS=14400 \
#     sh scripts/run_conversation_sustained_compose.sh
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_container=${APP_CONTAINER:-jobbridge-app}
concurrency=${LOAD_CONCURRENCY:-12}
duration=${LOAD_DURATION_SECONDS:-30}
drain_timeout=${LOAD_DRAIN_TIMEOUT_SECONDS:-240}

cleanup() {
  docker exec "$app_container" rm -f \
    /tmp/conversation_sustained_probe.py \
    /tmp/conversation_load_probe.py \
    /tmp/conversation_production_smoke.py \
    /tmp/demo_acceptance_smoke.py >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for script in \
  conversation_sustained_probe.py \
  conversation_load_probe.py \
  conversation_production_smoke.py \
  demo_acceptance_smoke.py
do
  docker cp "$repo_dir/scripts/$script" "$app_container:/tmp/$script"
done

docker exec -e PYTHONPATH=/tmp:/app "$app_container" sh -c "
  python /tmp/conversation_sustained_probe.py \
    --redis-url \"\$(python -c 'from app.config import settings; print(settings.redis_url)')\" \
    --mysql-dsn \"\$(python -c 'from app.config import settings; print(settings.db_url)')\" \
    --concurrency '$concurrency' \
    --duration-seconds '$duration' \
    --drain-timeout-seconds '$drain_timeout'
"
