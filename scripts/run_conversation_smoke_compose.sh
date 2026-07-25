#!/bin/sh
set -eu

# Run the destructive-scoped production dialogue smoke from inside the app
# container so credentials never have to be printed or copied to the host.
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_container=${APP_CONTAINER:-jobbridge-app}
base_url=${SMOKE_BASE_URL:-http://nginx}

cleanup() {
  docker exec "$app_container" rm -f \
    /tmp/conversation_production_smoke.py \
    /tmp/demo_acceptance_smoke.py >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker cp \
  "$repo_dir/scripts/conversation_production_smoke.py" \
  "$app_container:/tmp/conversation_production_smoke.py"
docker cp \
  "$repo_dir/scripts/demo_acceptance_smoke.py" \
  "$app_container:/tmp/demo_acceptance_smoke.py"

docker exec -e PYTHONPATH=/tmp:/app "$app_container" sh -c "
  python /tmp/conversation_production_smoke.py \
    --base-url '$base_url' \
    --redis-url \"\$(python -c 'from app.config import settings; print(settings.redis_url)')\" \
    --mysql-dsn \"\$(python -c 'from app.config import settings; print(settings.db_url)')\" \
    --timeout-seconds 75
"
