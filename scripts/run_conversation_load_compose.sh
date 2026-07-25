#!/bin/sh
set -eu

# Run the scoped real-model queue probe inside the app container. Override with:
# LOAD_USERS=24 APP_CONTAINER=jobbridge-app ./scripts/run_conversation_load_compose.sh
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_container=${APP_CONTAINER:-jobbridge-app}
users=${LOAD_USERS:-12}
timeout=${LOAD_TIMEOUT_SECONDS:-240}

cleanup() {
  docker exec "$app_container" rm -f \
    /tmp/conversation_load_probe.py \
    /tmp/conversation_production_smoke.py \
    /tmp/demo_acceptance_smoke.py >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for script in \
  conversation_load_probe.py \
  conversation_production_smoke.py \
  demo_acceptance_smoke.py
do
  docker cp "$repo_dir/scripts/$script" "$app_container:/tmp/$script"
done

docker exec -e PYTHONPATH=/tmp:/app "$app_container" sh -c "
  python /tmp/conversation_load_probe.py \
    --redis-url \"\$(python -c 'from app.config import settings; print(settings.redis_url)')\" \
    --mysql-dsn \"\$(python -c 'from app.config import settings; print(settings.db_url)')\" \
    --users '$users' \
    --timeout-seconds '$timeout'
"
