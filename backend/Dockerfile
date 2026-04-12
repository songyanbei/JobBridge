# ============================================================================
# JobBridge 业务服务镜像
# 构建: docker build -t jobbridge-app .
# ============================================================================
FROM python:3.11-slim AS base

# 系统依赖（编译 bcrypt/cryptography 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷依赖文件，利用 Docker 层缓存（代码改了不用重装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目代码
COPY app/ ./app/

# 创建上传目录
RUN mkdir -p /data/uploads /data/logs

# 非 root 用户运行
RUN useradd -m appuser && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

# 生产启动：2 worker，access log 写文件
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--access-log", \
     "--log-level", "info"]
