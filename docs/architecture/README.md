# JobBridge 架构方案文档集

> 版本：v1.0
> 状态：架构基线
> 适用范围：企业微信文字机器人，以及未来扩展到二手物品、房屋、服务等分类信息场景。

## 目标

JobBridge 的目标架构是：

```text
统一 Listing Flow
  + 领域 Profile
  + Skill 驱动的语义理解
  + 有界模型编排
  + MCP 能力适配
  + 确定性权限/审核/事务内核
```

它不是一个开放式 Agent 平台，而是一个由自然语言驱动的分类信息业务系统。模型负责理解用户表达，平台负责执行可审计、可回滚的业务动作。

## 阅读顺序

| 文档 | 主要内容 | 主要读者 |
|---|---|---|
| [01 架构总览](01-architecture-overview.md) | 目标、边界、分层、关键决策 | 全体研发、产品、架构 |
| [02 Listing 领域与流程](02-listing-domain-and-flow.md) | 公共 Listing、Profile、状态机、搜索推荐 | 后端、产品、数据 |
| [03 LLM、Skill、MCP](03-llm-skill-mcp-and-orchestration.md) | 语义协议、Skill、Tool、有限编排 | AI、后端 |
| [04 可靠性与性能](04-reliability-performance-and-observability.md) | 幂等、队列、出站、性能、安全、观测 | 后端、运维、测试 |
| [05 迁移、测试与后台](05-migration-testing-and-admin.md) | 代码迁移、回放测试、灰度、后台兼容 | 项目管理、研发、测试、运营 |
| [06 开源项目选型](06-open-source-reference-and-selection.md) | Rasa、LangGraph、Dify、MCP 等调研 | 架构、技术决策 |

## 当前系统基线

当前实现为 FastAPI + MySQL + Redis + 独立 Worker + 企业微信 Webhook + Qwen/豆包 Provider + Vue 管理后台。现有岗位、简历、搜索、审核、权限、后台和测试资产继续作为迁移基线。

主要代码入口：

- `backend/app/api/webhook.py`：企微验签、解密、幂等、限流、入队；
- `backend/app/services/worker.py`：消息 claim、用户锁、业务处理、出站重试；
- `backend/app/services/message_router.py`：当前约 2300 行，待逐步收敛；
- `backend/app/services/intent_service.py`：当前约 1300 行，legacy/v2 解析并存；
- `backend/app/services/search_service.py`：硬过滤、重排、候选快照和放宽；
- `backend/app/services/upload_service.py`：草稿、字段校验、审核和岗位/简历入库。

## 使用约定

1. 本目录文档是新的主方案；根目录旧总稿仅用于历史对照。
2. 领域新增以 Profile、Schema、Policy 和少量插件为入口，不在总 Router 中继续增加分支。
3. 任何涉及权限、审核、删除、封禁、联系方式和数据库写入的规则，以代码和数据库约束为准，不以 Prompt/Skill 为准。
4. 架构变更应补充对应的 ADR、回放样例和验收指标。
