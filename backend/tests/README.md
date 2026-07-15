# Backend Test Runbook

## Environment

1. Copy `.env.example` to `.env`
2. Fill in local MySQL and Redis connection values
3. Install dependencies: `pip install -r requirements.txt`

## Database Bootstrap

```bash
# 建库
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS jobbridge CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"

# 建表
mysql -u root -p jobbridge < sql/schema.sql

# 导入种子数据
mysql -u root -p jobbridge < sql/seed.sql
mysql -u root -p jobbridge < sql/seed_cities_full.sql
```

## Test Commands

The default local run skips tests marked `integration`. Set `RUN_INTEGRATION=1`
only when the required external services are running.

### Linux / macOS / Git Bash

```bash
cd backend

# Unit tests (same command used by CI)
pytest -p no:cacheprovider tests/unit -q

# Search SQL integration gate (requires MySQL 8)
RUN_INTEGRATION=1 \
DB_HOST=127.0.0.1 DB_PORT=3306 DB_NAME=jobbridge_test \
DB_USER=jobbridge_test DB_PASSWORD=jobbridge_test \
pytest -p no:cacheprovider tests/integration/test_search_sql_mysql.py -q

# Integration tests
RUN_INTEGRATION=1 pytest tests/integration/ -v

# All tests
RUN_INTEGRATION=1 pytest tests/ -v
```

### Windows PowerShell

```powershell
cd backend

# Unit tests (same command used by CI)
pytest -p no:cacheprovider tests/unit -q

# Search SQL integration gate (requires MySQL 8)
$env:RUN_INTEGRATION='1'
$env:DB_HOST='127.0.0.1'
$env:DB_PORT='3306'
$env:DB_NAME='jobbridge_test'
$env:DB_USER='jobbridge_test'
$env:DB_PASSWORD='jobbridge_test'
pytest -p no:cacheprovider tests/integration/test_search_sql_mysql.py -q

# Integration tests
$env:RUN_INTEGRATION='1'; pytest tests/integration/ -v

# All tests
$env:RUN_INTEGRATION='1'; pytest tests/ -v
```

### Windows CMD

```cmd
cd backend

:: Unit tests (same command used by CI)
pytest -p no:cacheprovider tests/unit -q

:: Search SQL integration gate (requires MySQL 8)
set RUN_INTEGRATION=1 && set DB_HOST=127.0.0.1 && set DB_PORT=3306 && set DB_NAME=jobbridge_test && set DB_USER=jobbridge_test && set DB_PASSWORD=jobbridge_test && pytest -p no:cacheprovider tests/integration/test_search_sql_mysql.py -q

:: Integration tests
set RUN_INTEGRATION=1 && pytest tests/integration/ -v

:: All tests
set RUN_INTEGRATION=1 && pytest tests/ -v
```

## Phase 2 Pressure Test

The composite Phase 2 pressure test covers:

- WeCom crypto and callback parsing
- Redis rate-limit / dedup / queue flow
- Mocked Qwen provider extract + rerank
- Local storage save / exists / delete
- Mocked WeCom client outbound calls

### PowerShell

```powershell
cd backend
.\.venv\Scripts\python.exe tests/perf/phase2_pressure.py
```

### Linux / macOS / Git Bash

```bash
cd backend
python tests/perf/phase2_pressure.py
```

Example custom run:

```bash
python tests/perf/phase2_pressure.py --messages 2400 --ingress-workers 32 --consumer-workers 16 --client-iterations 1800 --client-workers 48
```

## Phase 3 Tests (Business Services)

Phase 3 adds 7 service unit tests and 4 integration tests.

### Unit Tests (no external dependencies)

```bash
cd backend

# All Phase 3 service unit tests
pytest tests/unit/test_intent_service.py tests/unit/test_conversation_service.py tests/unit/test_user_service.py tests/unit/test_audit_service.py tests/unit/test_permission_service.py tests/unit/test_upload_service.py tests/unit/test_search_service.py -v
```

### Integration Tests (require MySQL + Redis)

```bash
cd backend

# All Phase 3 integration tests
RUN_INTEGRATION=1 pytest tests/integration/test_phase3_upload_and_search.py tests/integration/test_phase3_delete_flow.py tests/integration/test_phase3_broker_flow.py tests/integration/test_phase3_upload_then_search_smoke.py -v
```

PowerShell:
```powershell
$env:RUN_INTEGRATION='1'; pytest tests/integration/test_phase3_upload_and_search.py tests/integration/test_phase3_delete_flow.py tests/integration/test_phase3_broker_flow.py tests/integration/test_phase3_upload_then_search_smoke.py -v
```

### Phase 3 Smoke Flow

The `test_phase3_upload_then_search_smoke.py` integration test serves as the Phase 3 smoke flow:
- Factory uploads a job → passes audit → enters DB
- Worker searches → finds the job → result formatted correctly
- Worker result does not leak phone/address/discriminatory fields

## Smoke Checks

```bash
cd backend
python -c "from app.models import *; print('models OK')"
python -c "from app.schemas import *; print('schemas OK')"
python -c "from app.llm import get_intent_extractor, get_reranker; print('llm OK')"
python -c "from app.storage import get_storage; print('storage OK')"
python -c "from app.wecom.client import WeComClient; print('wecom OK')"
python -c "from app.wecom.crypto import verify_signature, decrypt_message; print('crypto OK')"
python -c "from app.wecom.callback import parse_message, WeComMessage; print('callback OK')"
```

## Phase 5 Full-Service Async Message Smoke

This smoke test injects a mock WeCom text message into `queue:incoming`, lets the
real worker consume it, and reads the business reply from Redis Pub/Sub channel
`mock:outbound:<userid>`. It is intentionally not called an HTTP E2E test
because the real webhook returns an enqueue ACK rather than the business reply.

Prerequisites:

- Backend app is running and `/health` is reachable.
- Worker is running against the same Redis/MySQL.
- `MOCK_WEWORK_OUTBOUND=true`
- `MOCK_WEWORK_REDIS_URL=<redis-url>`
- `APP_ENV` is not `production`.
- Base seed and `scripts/demo_seed_supplement.sql` have been applied.

```bash
python ../scripts/demo_acceptance_smoke.py \
  --base-url http://127.0.0.1:8000 \
  --redis-url redis://127.0.0.1:6379/0 \
  --mysql-dsn mysql+pymysql://jobbridge:jobbridge@127.0.0.1:3306/jobbridge \
  --expect "为您找到"
```

For gate hit/miss acceptance, run once with recommendation rollout values set to
`100`, restart app and worker, then run again with the same rollout values set
to `0` and restart both processes.
