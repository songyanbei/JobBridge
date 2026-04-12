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

### Linux / macOS / Git Bash

```bash
cd backend

# Unit tests
pytest tests/unit/ -v

# Integration tests
RUN_INTEGRATION=1 pytest tests/integration/ -v

# All tests
RUN_INTEGRATION=1 pytest tests/ -v
```

### Windows PowerShell

```powershell
cd backend

# Unit tests
pytest tests/unit/ -v

# Integration tests
$env:RUN_INTEGRATION='1'; pytest tests/integration/ -v

# All tests
$env:RUN_INTEGRATION='1'; pytest tests/ -v
```

### Windows CMD

```cmd
cd backend

:: Unit tests
pytest tests/unit/ -v

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
