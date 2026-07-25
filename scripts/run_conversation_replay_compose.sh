#!/bin/sh
set -eu

# Execute provider replay inside the configured app container without exposing
# database or model credentials to the host.
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
app_container=${APP_CONTAINER:-jobbridge-app}
mode=${EVAL_MODE:-curated}
limit=${EVAL_LIMIT:-100}
repeat=${EVAL_REPEAT:-1}
extra_args=${EVAL_EXTRA_ARGS:-}

case "$mode" in
  curated) mode_arg="--curated" ;;
  synthetic) mode_arg="--synthetic-matrix" ;;
  historical) mode_arg="" ;;
  *)
    echo "EVAL_MODE must be curated, synthetic, or historical" >&2
    exit 2
    ;;
esac

cleanup() {
  docker exec "$app_container" rm -f \
    /tmp/conversation_replay_eval.py >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker cp \
  "$repo_dir/scripts/conversation_replay_eval.py" \
  "$app_container:/tmp/conversation_replay_eval.py"
docker exec \
  -e PYTHONPATH=/app \
  -e LOG_LEVEL="${EVAL_LOG_LEVEL:-WARNING}" \
  "$app_container" sh -c "
  python /tmp/conversation_replay_eval.py \
    $mode_arg \
    --limit '$limit' \
    --repeat '$repeat' \
    $extra_args
"
