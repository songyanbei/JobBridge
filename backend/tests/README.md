# Phase 1 测试运行说明

## 环境准备

1. 复制 `.env.example` 为 `.env`，填入本地数据库和 Redis 连接信息
2. 安装依赖：`pip install -r requirements.txt`

## 数据库初始化

```bash
# 建库
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS jobbridge CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"

# 建表
mysql -u root -p jobbridge < sql/schema.sql

# 导入种子数据
mysql -u root -p jobbridge < sql/seed.sql
mysql -u root -p jobbridge < sql/seed_cities_full.sql
```

## 运行测试

### Linux / macOS / Git Bash

```bash
cd backend

# 单元测试（不需要 MySQL/Redis）
pytest tests/unit/ -v

# 集成测试（需要启动 MySQL + Redis）
RUN_INTEGRATION=1 pytest tests/integration/ -v

# 全部测试
RUN_INTEGRATION=1 pytest tests/ -v
```

### Windows PowerShell

```powershell
cd backend

# 单元测试（不需要 MySQL/Redis）
pytest tests/unit/ -v

# 集成测试（需要启动 MySQL + Redis）
$env:RUN_INTEGRATION='1'; pytest tests/integration/ -v

# 全部测试
$env:RUN_INTEGRATION='1'; pytest tests/ -v
```

### Windows CMD

```cmd
cd backend

:: 单元测试
pytest tests/unit/ -v

:: 集成测试
set RUN_INTEGRATION=1 && pytest tests/integration/ -v

:: 全部测试
set RUN_INTEGRATION=1 && pytest tests/ -v
```

## 自测验证

```bash
cd backend
python -c "from app.models import *; print('models OK')"
python -c "from app.schemas import *; print('schemas OK')"
```
