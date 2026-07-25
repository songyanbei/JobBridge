#!/bin/sh
set -eu

# Destructive-scoped chaos acceptance for the local JobBridge compose stack.
# Pauses each dependency only for the configured bounded interval and always
# unpauses it from traps before returning.
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_container=${APP_CONTAINER:-jobbridge-app}
redis_container=${REDIS_CONTAINER:-jobbridge-redis}
mysql_container=${MYSQL_CONTAINER:-jobbridge-mysql}
users=${CHAOS_USERS:-20}
redis_pause=${CHAOS_REDIS_PAUSE_SECONDS:-4}
mysql_pause=${CHAOS_MYSQL_PAUSE_SECONDS:-7}
timeout=${CHAOS_TIMEOUT_SECONDS:-180}
paused=""

cleanup() {
  if [ -n "$paused" ]; then
    docker unpause "$paused" >/dev/null 2>&1 || true
  fi
  docker exec "$app_container" rm -f \
    /tmp/conversation_chaos_probe.py \
    /tmp/conversation_production_smoke.py \
    /tmp/demo_acceptance_smoke.py >/dev/null 2>&1 || true
  rm -f /tmp/jobbridge-chaos-redis.out /tmp/jobbridge-chaos-mysql.out
}
trap cleanup EXIT INT TERM

for script in \
  conversation_chaos_probe.py \
  conversation_production_smoke.py \
  demo_acceptance_smoke.py
do
  docker cp "$repo_dir/scripts/$script" "$app_container:/tmp/$script"
done

run_mode() {
  mode=$1
  dependency=$2
  pause_seconds=$3
  run_id="jobbridge-${mode}-$(date +%s)-$$"
  output="/tmp/jobbridge-chaos-${mode}.out"

  docker exec -e PYTHONPATH=/tmp:/app "$app_container" sh -c "
    python /tmp/conversation_chaos_probe.py \
      --mode '$mode' \
      --run-id '$run_id' \
      --redis-url \"\$(python -c 'from app.config import settings; print(settings.redis_url)')\" \
      --mysql-dsn \"\$(python -c 'from app.config import settings; print(settings.db_url)')\" \
      --users '$users' \
      --timeout-seconds '$timeout'
  " >"$output" 2>&1 &
  probe_pid=$!

  ready=0
  for _ in $(seq 1 100); do
    if docker exec "$redis_container" redis-cli EXISTS "chaos:ready:$run_id" \
      | grep -q '^1$'
    then
      ready=1
      break
    fi
    sleep 0.1
  done
  if [ "$ready" -ne 1 ]; then
    wait "$probe_pid" || true
    cat "$output"
    echo "chaos probe did not publish ready marker: $mode" >&2
    return 1
  fi

  paused=$dependency
  docker pause "$dependency" >/dev/null
  sleep "$pause_seconds"
  docker unpause "$dependency" >/dev/null
  paused=""

  if ! wait "$probe_pid"; then
    cat "$output"
    return 1
  fi
  cat "$output"
}

run_mode redis "$redis_container" "$redis_pause"
run_mode mysql "$mysql_container" "$mysql_pause"
