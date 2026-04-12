# Phase 0 Kickoff 基线确认

> 日期：2026-04-12
> 状态：部分完成（环境验证待 Docker 安装后补充）

---

## 1. 文档基线冻结

| 文档 | 版本 | 状态 |
|------|------|------|
| 方案设计_v0.1.md | v0.20 (2026-04-10) + 审查修补 (2026-04-12) | ✅ 冻结 |
| docs/architecture.md | v0.14 + prompt 规范补充 (2026-04-12) | ✅ 冻结 |
| docs/implementation-plan.md | 评审修正版 (2026-04-12) | ✅ 冻结 |
| backend/sql/schema.sql | 11 张表 (2026-04-12) | ✅ 冻结 |

后续修改必须遵循实施计划 §8 的变更控制规则。

---

## 2. 本地开发环境检查

| 依赖 | 要求 | 实际 | 状态 |
|------|------|------|------|
| Python | 3.11+ | 3.12.12 (miniforge3) | ✅ 通过 |
| Node.js | 16+ | v24.14.1 | ✅ 通过 |
| npm | 8+ | 11.11.0 | ✅ 通过 |
| Git | 2.x | 2.53.0 | ✅ 通过 |
| Docker | Docker Desktop 或 WSL2 Docker | 未安装 | ❌ 待安装 |
| Docker Compose | v2+ | 未安装 | ❌ 待安装 |

### Docker 安装后需验证

```bash
# 1. 启动基础设施
docker compose up -d

# 2. 验证 MySQL
docker exec jobbridge-mysql mysql -u jobbridge -pjobbridge -e "SELECT 1"

# 3. 验证 Redis
docker exec jobbridge-redis redis-cli ping

# 4. 启动后端
cd backend
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
cd ..
cp .env.example .env
cd backend
uvicorn app.main:app --reload

# 5. 验证 /health
curl http://localhost:8000/health
# 预期: {"status":"ok","db":{"ok":true}}
```

---

## 3. 关键环境变量基线

基于 `.env.example`，开发环境默认值可直连 Docker 容器，无需修改：

| 变量 | 开发默认值 | 生产部署需改 |
|------|-----------|-------------|
| APP_ENV | development | production |
| APP_SECRET_KEY | change-me | ✅ 必须替换 |
| DB_HOST | localhost | mysql（compose service name） |
| DB_PASSWORD | jobbridge | ✅ 必须替换 |
| REDIS_PASSWORD | （空） | 视情况设置 |
| WECOM_* | （空） | ✅ Phase 4 前必须填 |
| LLM_PROVIDER | qwen | 确认选型后填 |
| LLM_API_KEY | （空） | ✅ Phase 2 前必须到手 |
| ADMIN_JWT_SECRET | change-me | ✅ 必须替换 |
| CORS_ORIGINS | （空） | 生产填具体域名 |

---

## 4. 冻结决策确认

以下决策在一期实施期间不再讨论，如需变更必须先回写需求文档：

| 决策 | 内容 | 来源 |
|------|------|------|
| 删除命令 | `/删除我的信息` 一期单步触发，不加二次确认 | 方案设计 §17.2.4 |
| Webhook 模型 | 快速 ACK + Redis 队列 + 独立 Worker 进程 | 方案设计 §12.5 |
| Worker 部署 | 独立进程，docker-compose 独立服务 | 方案设计 §12.5 |
| 限流策略 | userid 级，10 秒 5 条，主链路必选项 | 方案设计 §12.5 |
| 后台并行 | Admin 后端（Phase 5）与前端（Phase 6）可并行 | 实施计划 §3 |
| 企微出站 | 按方案 A 开发，不支持再降级到方案 C | 方案设计 §12.2 |
| LLM 方案 | 不引入向量数据库/RAG/知识库 | 方案设计 §4.4 |
| 同步/异步 | SQLAlchemy 同步模式，不上 async | 方案设计 §14.4 |

---

## 5. 外部依赖清单

### 5.1 开发前必须确认（阻塞 Phase 1-3）

| 依赖项 | 状态 | 负责人 | 截止时间 | 备选方案 |
|--------|------|--------|----------|----------|
| Docker 环境安装 | ❌ 待安装 | 开发者 | Phase 1 开始前 | WSL2 Docker 或 Docker Desktop |
| LLM 供应商选型 | ⚠️ 待确认 | 技术 | Phase 2 开始前 | 先选 Qwen，后期可切换 |
| LLM API Key + 额度 | ⚠️ 待获取 | 技术/客户 | Phase 2 联调前 | 临时测试 Key |

### 5.2 企微联调前必须确认（阻塞 Phase 4）

| 依赖项 | 状态 | 负责人 | 截止时间 | 备选方案 |
|--------|------|--------|----------|----------|
| 企微认证级别与客户联系权限 | ⚠️ 待确认 | 客户+技术 | Phase 4 前 | 先按方案 A 开发 |
| 客户企微管理员权限 | ⚠️ 待确认 | 客户 | Phase 4 前 | 客户内部申请 |
| 企微回调公网地址 | ⚠️ 待确认 | 技术/运维 | Phase 4 联调前 | 临时测试域名 |
| 企微 IP 白名单 | ⚠️ 待确认 | 运维 | Phase 4 联调前 | 临时放开测试环境 |

### 5.3 上线前必须确认（阻塞 Phase 7）

| 依赖项 | 状态 | 负责人 | 截止时间 | 备选方案 |
|--------|------|--------|----------|----------|
| 小程序详情页链接规则 | ⚠️ 待确认 | 客户+前端 | Phase 4 联调前 | 临时占位链接 |
| 隐私政策页 | ⚠️ 待确认 | 客户/法务 | 上线前 | 暂用通用模板 |
| 敏感词库初版 | ⚠️ 待确认 | 运营 | Phase 3 | 先用 seed.sql 默认词库 |
| 工种大类字典一期版 | ✅ 已确认 | — | — | seed.sql 已有 10 类 |
| 城市别名规则 | ✅ 已确认 | — | — | seed_cities_full.sql 342 城 |
| 默认管理员账号 | ✅ 已确认 | — | — | admin/admin123 + 首次改密 |
| 生产服务器 | ⚠️ 待确认 | 运维 | 上线前两周 | 临时云主机 |
| 备份与恢复策略 | ⚠️ 待确认 | 运维 | 上线前两周 | 宿主机定时备份 |
| 法务对敏感字段确认 | ⚠️ 待确认 | 客户/法务 | 上线前 | 关闭相关过滤开关 |

---

## 6. 目录职责确认

| 目录 | 职责 | 负责角色 |
|------|------|----------|
| `backend/` | Python FastAPI 后端（API + Services + LLM + WeChat） | 后端开发 |
| `backend/sql/` | 数据库 DDL + 种子数据 | 后端开发 |
| `frontend/` | Vue 3 运营后台 SPA | 前端开发 |
| `frontend/prototype/` | 原型 Demo（参考用，不进生产） | 设计参考 |
| `docs/` | 架构设计 + 实施计划 | 全团队 |
| `nginx/` | 反向代理配置 | 运维/后端 |
| 根目录 | Docker 编排 + .env + 方案设计 | 全团队 |

---

## 7. Phase 0 验收状态

| 检查项 | 状态 |
|--------|------|
| 需求基线冻结（方案设计 v0.20） | ✅ |
| 架构基线冻结（architecture.md） | ✅ |
| 实施计划冻结（implementation-plan.md） | ✅ |
| 冻结决策已文档化 | ✅ |
| 外部依赖清单已生成，高风险项有备选 | ✅ |
| 环境变量基线已确认 | ✅ |
| 目录职责已确认 | ✅ |
| Python/Node/Git 版本满足要求 | ✅ |
| Docker 环境可用 | ❌ 待安装 |
| 基础设施（MySQL + Redis）可启动 | ❌ 待 Docker |
| 后端 /health 可通 | ❌ 待 Docker |

**Phase 0 结论**：文档基线、决策冻结、依赖清单全部就绪。Docker 安装后补验基础设施启动即可进入 Phase 1。Phase 1 的 ORM 和 Schema 编写不依赖数据库运行（对着 schema.sql 写），Docker 安装可与 Phase 1 开发并行进行。
