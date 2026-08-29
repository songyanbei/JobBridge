# 06 开源项目调研与选型结论

> 读者：架构、技术负责人和研发。
> 目标：明确哪些项目值得借鉴、哪些不应直接引入核心链路。

## 1. 选型结论

没有一个开源项目可以直接替代 JobBridge 的领域内核。最合理的做法是借鉴成熟项目的局部设计，在现有 FastAPI、MySQL、Redis、Worker、权限和后台基础上实现轻量运行时。

## 2. 项目对比

| 项目 | 借鉴内容 | 主要限制 | 结论 |
|---|---|---|---|
| [Rasa/CALM](https://github.com/RasaHQ/rasa) | Flow、slot filling、Command、确定性 Dialogue Manager | CALM 完整生产能力与商业版本有关，需区分开源仓库和 Pro 能力 | 最重要的思想参考 |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 状态图、checkpoint、中断恢复、human-in-the-loop | 当前四条短流程引入后会形成第二套状态语义 | 长流程再评估 |
| [Dify](https://github.com/langgenius/dify) | Workflow、Prompt/模型版本、观测和评估 | 不能替代用户、权限、审核和事务内核；许可证有附加条件 | 可作为实验/评估工具 |
| [Temporal](https://github.com/temporalio/temporal) | 持久化 Workflow、重试、人工等待和恢复 | 当前单轮/多轮企微会话不需要跨天 Workflow | 跨天任务再引入 |
| [PydanticAI](https://github.com/pydantic/pydantic-ai) | 类型化输出、工具契约、评估 | 当前项目已有 Pydantic 和 LLM 抽象 | 借鉴方法，非首期必需 |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MCP Tools/Resources/Prompts，多种传输 | MCP 不提供业务授权和事务 | 建议采用，首期 in-process |
| [Botpress](https://github.com/botpress/botpress) | 渠道集成和 Bot 运营体验 | 当前仓库偏 Cloud/SDK，不能替代现有后台 | 参考体验，不作核心依赖 |

## 3. 与 JobBridge 的匹配度

### Rasa/CALM

最符合“LLM 理解 + 预定义 Flow + slot filling”的思路。JobBridge 应借鉴其 Command、Flow、纠正、补槽和确定性 Dialogue Manager，不必把整个系统迁移到 Rasa。

### LangGraph

适合需要 checkpoint、人工介入和长时间恢复的流程。当前招聘和分类信息操作以单轮/多轮消息为主，已有 Worker、Redis Session 和数据库事务，首期直接引入会增加状态管理复杂度。

### Dify

适合做 Prompt 实验、模型对比和运营配置原型，但其 Workflow 运行时不了解 JobBridge 的权限、审核、联系人脱敏和后台数据契约。不能把 Dify 当作核心领域服务。

### Temporal

适合未来的招聘订单、跨天人工审核、支付和交易履约。对于当前“收到消息后几十秒内完成一次业务动作”的场景，增加 Temporal 的收益不足以覆盖运维成本。

### MCP SDK

适合标准化能力边界。建议先实现本地 adapter，等客服工作台、小程序或外部 Agent 需要复用时，再拆远程 Streamable HTTP Server。

## 4. 许可证和治理

- Rasa 开源仓库为 Apache-2.0，但 CALM 文档中的部分能力与商业版本相关；
- LangGraph、Temporal、PydanticAI 和 MCP Python SDK 采用宽松开源许可证，仍需按依赖版本审查；
- Dify 为带附加条件的 Apache 2.0 修改许可，尤其要关注多租户和前端标识要求；
- Botpress 不应在未确认部署和许可边界前作为核心依赖。

正式上线前由项目负责人和法务确认锁定版本、许可证、版权声明和镜像来源。

## 5. 最终技术组合

```text
现有 FastAPI + MySQL + Redis + Worker
  + Python/Pydantic Listing Runtime
  + 版本化 Domain Profile
  + Markdown/YAML Skill Registry
  + 统一 LLM Gateway
  + MCP Python SDK 本地 adapter
  + Inbox/Outbox/幂等/观测补强
```

只有在业务规模和流程复杂度真实达到阈值时，才引入 LangGraph、Temporal、OpenSearch 或远程 MCP。架构演进由指标和业务需求驱动，而不是由框架能力驱动。
