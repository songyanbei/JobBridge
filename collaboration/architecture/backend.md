# 后端架构速览（给后端开发看）

> 完整架构见 `docs/architecture.md`，本文只摘要后端开发日常需要的关键信息。

## 技术栈

- Python 3.11+ / FastAPI / SQLAlchemy 2.0（同步模式）
- MySQL 8.0+ / Redis 7+
- LLM 通过抽象层调用（Qwen / 豆包 / 开源可切换）
- Docker Compose 部署

## 目录结构

```
backend/app/
├── main.py                 FastAPI 入口，CORS，/health
├── config.py               pydantic-settings 集中配置（读 .env）
├── db.py                   SQLAlchemy engine + session
├── models.py               ORM 模型（11 张表，单文件）
├── schemas/                Pydantic DTO（每个模块一个文件）
├── api/
│   ├── deps.py             共享依赖（get_db / get_current_admin / get_redis）
│   ├── webhook.py          POST /webhook/wecom（验签+幂等+限流+入队+快速返回）
│   ├── events.py           POST /api/events/*（小程序埋点回传）
│   └── admin/              /admin/* 运营后台路由组（~50 个端点）
├── services/               业务逻辑层（9 个 service + worker + message_router）
├── llm/                    LLM 抽象层（base + prompts + providers/）
├── wecom/                  企微集成（crypto + callback + client）
├── storage/                对象存储抽象（base + local）
├── tasks/                  定时任务（scheduler + ttl_cleanup + daily_report）
└── core/                   通用工具（exceptions + pagination + redis_client）
```

## 分层规则

```
api/         → 只做参数校验、鉴权、调 service、返回响应
services/    → 所有业务逻辑在这里，依赖 models + schemas + llm/base + storage/base
models.py    → ORM，不含业务逻辑
schemas/     → DTO，不含业务逻辑
llm/wecom/storage/ → 基础设施，通过 ABC 抽象，service 层不 import 具体 provider
```

**禁止**：
- api 层不直接写 SQL 或调 Redis
- service 层不 import `llm/providers/qwen.py`，只 import `llm/base.py`
- ORM 层不做全局过滤（软删除过滤统一在 service 层 `.filter(Model.deleted_at.is_(None))`）

## 数据库（11 张表）

| 表 | 用途 |
|---|---|
| user | 用户（工人/厂家/中介） |
| job | 岗位信息（硬过滤+软匹配+原始描述） |
| resume | 简历信息（结构同 job） |
| conversation_log | 对话历史（30 天 TTL） |
| audit_log | 审核动作日志 |
| dict_city | 城市字典（342 城） |
| dict_job_category | 工种大类字典（10 类） |
| dict_sensitive_word | 敏感词字典 |
| system_config | 系统配置（KV） |
| admin_user | 运营管理员账号 |
| wecom_inbound_event | 企微入站事件（幂等+状态追踪） |

会话状态（`session:{userid}`）存 Redis，不在 MySQL。

## 消息处理链路

```
企微 POST /webhook/wecom
    → 验签 → 解密 → 限流 → 幂等 → 入队 → 返回 200（<100ms）
    
Worker（独立进程）
    → BLPOP queue:incoming
    → wecom_inbound_event 状态 → processing
    → message_router.process()
        → user_service（识别/注册）
        → intent_service（LLM 意图分类）
        → 按 intent 分发（upload/search/show_more/command/chitchat）
        → permission_service（字段级过滤）
    → wecom/client.py 回复用户
    → wecom_inbound_event 状态 → done
```

## API 响应格式

```json
// 成功
{"code": 0, "message": "ok", "data": {...}}

// 分页
{"code": 0, "message": "ok", "data": {"items": [...], "total": 127, "page": 1, "size": 20, "pages": 7}}

// 错误
{"code": 40001, "message": "用户名或密码错误", "data": null}
```

错误码范围：40001-40099 鉴权 / 40100-40199 参数 / 40300-40399 权限 / 40400-40499 资源不存在 / 50000-50099 内部错误 / 50100-50199 LLM 异常

## 前端会依赖你的

- `/admin/*` 全部 API（约 50 个端点，完整清单见 `docs/architecture.md` §7.4）
- 统一响应格式（上面那个 code/message/data）
- JWT 鉴权（`Authorization: Bearer <token>`，24h 过期）
- 审核工作台的软锁（lock/unlock）+ 乐观锁（version 字段）+ Undo（30 秒内可撤销）

## ORM 编写注意

- ENUM 字段用字符串类型 + `sa.Enum` 约束
- JSON 字段用 `sa.JSON`
- `extra` 字段用 `MutableDict.as_mutable(sa.JSON)`（变更跟踪）
- `job.version` / `resume.version`：每次更新 `version = version + 1`
- 详见 `docs/architecture.md` §4.5
