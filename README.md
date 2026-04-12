# JobBridge 招聘撮合平台

基于企业微信 + LLM 的蓝领招聘撮合系统，自动完成信息收集、结构化抽取、智能匹配推荐和多轮对话，连接厂家、中介和工人三方。

## 核心能力

- **自然语言交互**：用户通过企微发送自由文本，LLM 自动抽取结构化信息
- **智能匹配引擎**：三步漏斗（SQL 硬过滤 → LLM 语义重排 → 角色权限过滤）
- **多轮对话**：跨轮次条件累积、"更多"翻页、条件修正，30 分钟会话保持
- **内容审核**：敏感词 + LLM 安全检测 + 人工审核队列
- **运营后台**：15 页 Vue 3 管理后台（审核工作台、账号/岗位/简历管理、数据看板等）

## 架构概览

```
企微消息 → Webhook（验签+幂等+入队） → Redis 队列
                                            ↓
                                   Worker（独立进程）
                                            ↓
                                   消息路由 → 意图识别(LLM)
                                            ↓
                              ┌─────────────┼─────────────┐
                              ↓             ↓             ↓
                          上传入库      检索匹配      指令/闲聊
                         (审核→存储)  (硬过滤→重排)   (直接回复)
                              ↓             ↓
                           企微回复用户 ← 权限过滤 + 格式化
```

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11+ / FastAPI / SQLAlchemy 2.0 |
| 数据库 | MySQL 8.0+ / Redis 7+ |
| LLM | 可插拔（Qwen / 豆包 / 开源自部署） |
| 前端 | Vue 3 / Element Plus / Vite 5 / Pinia |
| 部署 | Docker Compose / Nginx |

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/songyanbei/JobBridge.git
cd JobBridge

# 2. 启动基础设施（MySQL + Redis，自动初始化数据库）
docker compose up -d

# 3. 创建后端环境
cd backend
python -m venv .venv
source .venv/Scripts/activate  # Windows Git Bash
# source .venv/bin/activate    # Linux / macOS
pip install -r requirements.txt

# 4. 配置环境变量
cd ..
cp .env.example .env
# 默认值可直连 docker 启的 MySQL/Redis，开发环境无需改动

# 5. 启动后端
cd backend
uvicorn app.main:app --reload

# 6. 验证
curl http://localhost:8000/health
# 预期: {"status":"ok","env":"development","db":{"ok":true,...}}

# 7. Swagger API 文档
# 浏览器打开 http://localhost:8000/docs
```

**默认账号**：MySQL `jobbridge/jobbridge` · 运营后台 `admin/admin123`（首次登录强制改密码）

## 项目结构

```
JobBridge/
├── backend/                    Python FastAPI 后端
│   ├── app/
│   │   ├── main.py             应用入口
│   │   ├── config.py           集中配置（pydantic-settings）
│   │   ├── api/                HTTP 路由（webhook + admin）
│   │   ├── services/           业务逻辑层
│   │   ├── llm/                LLM 抽象层（base + providers）
│   │   ├── wecom/              企微集成（加解密 + 消息收发）
│   │   ├── storage/            对象存储抽象
│   │   ├── tasks/              定时任务（TTL 清理 / 日报）
│   │   └── core/               通用工具（Redis / 异常 / 分页）
│   └── sql/                    DDL + 种子数据
├── frontend/                   Vue 3 运营后台
│   └── prototype/              P0 原型 Demo
├── docs/
│   └── architecture.md         技术架构设计
├── nginx/                      反向代理配置
├── docker-compose.yml          开发环境
├── docker-compose.prod.yml     生产部署
└── 方案设计_v0.1.md             产品 & 系统设计文档
```

## 文档

- [方案设计](方案设计_v0.1.md) — 产品需求、数据模型、匹配引擎、多轮对话、运营后台、部署方案
- [技术架构](docs/architecture.md) — 分层架构、接口契约、前后端通信协议、Prompt 设计规范

## 当前状态

**阶段**：骨架代码完成，进入业务开发

- ✅ 数据库 DDL（11 张表）+ 种子数据
- ✅ 项目骨架 + 核心抽象接口
- ✅ Docker 开发/生产环境配置
- ✅ 前端 P0 原型
- 🔧 业务逻辑实现中（services 层）
- 🔧 运营后台前端开发中

## License

Private — 仅限项目团队内部使用。
