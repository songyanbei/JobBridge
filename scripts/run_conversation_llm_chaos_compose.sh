#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_container=${APP_CONTAINER:-jobbridge-app}
users=${CHAOS_USERS:-20}
timeout=${CHAOS_TIMEOUT_SECONDS:-120}
base="-p jobbridge -f docker-compose.prod.yml -f docker-compose.hardening-test.yml"
chaos="$base -f docker-compose.llm-chaos-test.yml"
original_workers=$(docker compose $base ps -q worker | wc -l | tr -d ' ')
if [ "$original_workers" -lt 1 ]; then
  echo "no running worker replicas found" >&2
  exit 2
fi

cleanup() {
  docker exec "$app_container" python -c \
    "from urllib.request import urlopen; urlopen('http://127.0.0.1:18080/shutdown', timeout=2).read()" \
    >/dev/null 2>&1 || true
  docker exec "$app_container" rm -f \
    /tmp/llm_fault_server.py \
    /tmp/conversation_chaos_probe.py \
    /tmp/conversation_production_smoke.py \
    /tmp/demo_acceptance_smoke.py >/dev/null 2>&1 || true
  # Always restore workers to the normal configured provider.
  docker compose $base up -d --no-deps --force-recreate \
    --scale worker="$original_workers" worker \
    >/dev/null
}
trap cleanup EXIT INT TERM

for script in llm_fault_server.py conversation_chaos_probe.py \
  conversation_production_smoke.py demo_acceptance_smoke.py
do
  docker cp "$repo_dir/scripts/$script" "$app_container:/tmp/$script"
done

# Remove a stale server from an interrupted prior run before binding the port.
docker exec "$app_container" python -c \
  "from urllib.request import urlopen; urlopen('http://127.0.0.1:18080/shutdown', timeout=1).read()" \
  >/dev/null 2>&1 || true
sleep 0.2
docker exec "$app_container" sh -c \
  'python /tmp/llm_fault_server.py >/tmp/llm_fault_server.log 2>&1 &'
docker compose $chaos up -d --no-deps --force-recreate --scale worker=4 worker \
  >/dev/null

docker exec -e PYTHONPATH=/tmp:/app "$app_container" sh -c "
  python /tmp/conversation_chaos_probe.py \
    --mode llm_mixed \
    --run-id jobbridge-llm-mixed-\$(date +%s) \
    --redis-url \"\$(python -c 'from app.config import settings; print(settings.redis_url)')\" \
    --mysql-dsn \"\$(python -c 'from app.config import settings; print(settings.db_url)')\" \
    --users '$users' \
    --timeout-seconds '$timeout'
"
docker exec "$app_container" python -c \
  "from urllib.request import urlopen; print('fault_server_counts=' + urlopen('http://127.0.0.1:18080/stats').read().decode())"
