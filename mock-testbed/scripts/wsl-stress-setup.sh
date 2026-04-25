#!/usr/bin/env bash
# One-shot WSL2 stress test setup: venv + deps + pytest.
set -euo pipefail

ROOT=/mnt/d/work/JobBridge/.claude/worktrees/romantic-proskuriakova-bdc75a
VENV=/tmp/mock-testbed-venv
BACKEND="$ROOT/mock-testbed/backend"

cd "$BACKEND"

if [ ! -d "$VENV" ]; then
    echo "==> Creating venv at $VENV"
    python3 -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Upgrading pip"
pip install -q --upgrade pip

echo "==> Installing mock-testbed deps"
pip install -q -r requirements.txt

echo "==> Dep verify"
python -c "import fastapi, uvicorn, sqlalchemy, redis, pydantic_settings, fakeredis, pytest, httpx; print('deps ok')"

echo ""
echo "==> pytest (mock-testbed backend tests, tests/ dir only)"
cd "$BACKEND"
pytest -q tests/ 2>&1 | tail -50
