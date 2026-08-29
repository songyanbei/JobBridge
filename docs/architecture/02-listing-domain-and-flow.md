# 02 Listing 领域模型与统一流程

> 读者：产品、后端、数据和搜索推荐研发。
> 目标：定义跨领域复用的 Listing、Profile、状态和搜索推荐协议。

## 1. 公共 Listing 模型

推荐采用“公共主体 + 领域详情”，不要把所有领域字段混成一张超级表。

```text
listing
  id, profile_key, owner_id, title, body
  location_id, status, moderation_status
  contact_policy, expires_at, created_at, updated_at

listing_detail_job
  listing_id, job_category, salary_floor, pay_type, headcount, ...

listing_detail_resume
  listing_id, expected_cities, expected_categories, salary_floor, age, ...

listing_detail_item
  listing_id, category, price, condition, delivery, ...
```

公共字段用于生命周期、展示和权限；领域详情用于强校验、搜索和推荐。

### 1.1 公共生命周期

```text
draft
  -> pending_review
  -> published
  -> expired / delisted

pending_review -> rejected
rejected -> draft（允许修改后重新提交）
```

所有状态迁移必须由服务端 Policy 和事务执行，不能由模型直接指定。

### 1.2 推荐卡片

```python
class ListingCard(BaseModel):
    listing_id: str
    profile: str
    title: str
    body_summary: str
    location_text: str | None = None
    attributes: dict[str, object] = Field(default_factory=dict)
    contact_action: str
    explanation: str | None = None
```

企微对外默认渲染为“标题 + 摘要 + 关键属性 + 联系入口”。内部仍保留结构化属性，否则无法进行可靠过滤和排序。

## 2. Listing Profile

Profile 是领域配置和策略集合，至少包含：

- Profile key 和实体类型；
- 字段类型、是否必填、枚举和默认值；
- 字典/别名来源；
- 发布、搜索和联系角色；
- 审核策略和联系方式策略；
- 硬过滤字段、索引映射和排序特征；
- Skill 版本、示例和特殊校验插件。

### 2.1 岗位 Profile

```yaml
profile: recruitment.job
entity: job
operations: [publish, search, show_more, contact]
fields:
  title: {type: text, required: true}
  body: {type: text, required: true}
  city: {type: city, required: true}
  job_category: {type: taxonomy, required: true}
  salary_floor_monthly: {type: money, required: true, min: 0}
  pay_type: {type: enum, values: [monthly, daily, hourly], required: true}
  headcount: {type: integer, required: true, min: 1}
permissions:
  publish_roles: [factory, broker]
  search_roles: [worker, broker]
ranking: [category_match, city_match, salary_match, freshness]
moderation_policy: recruitment_default
contact_policy: role_based_masking
```

### 2.2 简历 Profile

```yaml
profile: recruitment.resume
entity: resume
operations: [publish, search, show_more, contact]
fields:
  title: {type: text, required: true}
  body: {type: text, required: true}
  expected_cities: {type: city_list, required: true}
  expected_job_categories: {type: taxonomy_list, required: true}
  salary_expect_floor_monthly: {type: money, required: true, min: 0}
  gender: {type: enum, required: true}
  age: {type: integer, required: true, min: 16, max: 70}
permissions:
  publish_roles: [worker]
  search_roles: [factory, broker]
ranking: [category_match, city_match, salary_match, freshness]
moderation_policy: recruitment_default
contact_policy: role_based_masking
```

### 2.3 二手物品 Profile

```yaml
profile: secondhand.item
entity: item
operations: [publish, search, show_more, contact]
fields:
  title: {type: text, required: true}
  body: {type: text, required: true}
  category: {type: taxonomy, required: true}
  city: {type: city, required: true}
  price: {type: money, required: true, min: 0}
  condition: {type: enum, values: [new, like_new, good, fair]}
  delivery: {type: enum, values: [pickup, shipping, negotiable]}
  images: {type: attachment_list, max_items: 9}
permissions:
  publish_roles: [user]
  search_roles: [user]
ranking: [text_relevance, price_match, distance, freshness]
moderation_policy: secondhand_default
contact_policy: authenticated_only
```

## 3. 统一状态机

```text
IDLE
 ├─ start_publish -> PUBLISH_COLLECTING
 ├─ start_search -> SEARCH_ACTIVE
 ├─ command/help -> RESPONSE
 └─ chitchat/unsupported -> RESPONSE

PUBLISH_COLLECTING
 ├─ answer_missing_slot -> 继续收集/追问
 ├─ all_required -> CONFIRMING
 ├─ cancel -> IDLE
 ├─ new operation -> CONFLICT
 └─ expired -> IDLE + 过期提示

CONFIRMING
 ├─ confirm -> MODERATION_PENDING
 ├─ modify -> PUBLISH_COLLECTING
 └─ cancel -> IDLE 或保留草稿

MODERATION_PENDING
 ├─ pass -> PUBLISHED
 ├─ reject -> REJECTED
 └─ error -> RETRY/DLQ

SEARCH_ACTIVE
 ├─ modify_search -> 刷新候选快照
 ├─ show_more -> 消费已有快照
 ├─ low_recall -> 固定放宽/询问
 ├─ start_publish -> CONFLICT
 └─ reset -> IDLE
```

公共状态包含草稿、TTL、确认、取消、冲突、搜索条件、候选快照、更多和会话恢复。Profile 只决定字段和策略。

## 4. 招聘领域特殊规则

招聘由两个相反方向的 Profile 构成：`recruitment.job` 和 `recruitment.resume`。除公共 Listing Flow 外，还需要：

- 岗位与简历的双向匹配映射；
- `worker/factory/broker` 权限矩阵；
- 联系方式、年龄等敏感字段脱敏；
- 岗位/简历审核和有效期策略；
- 发布后“顺便找工人/找岗位”的局部组合动作。

这些差异放在 `MatchingPolicy`、`AuthorizationPolicy` 和 Profile 配置中，不复制公共状态机。

## 5. 搜索和推荐

统一采用三层漏斗：

```text
Profile Schema + Authorization
  -> SQL/搜索引擎硬过滤
  -> 领域排序/有限重排
  -> 脱敏、去重、频控、卡片渲染
```

早期沿用当前 SQL + LLM rerank：

- 招聘：城市、工种、薪资、班次、时效；
- 二手：品类、文本相关性、价格、距离、成色、时效；
- 平台级：去重、频控、多样性和曝光反馈。

“更多”只消费候选快照，不重新运行完整规划。无结果放宽由 Profile 的有限策略枚举控制。

## 6. 联系和隐私

联系方式不是普通正文的一部分，而是受策略控制的受保护字段：

- 默认展示联系入口，不直接公开原始联系方式；
- 服务端根据 actor、角色、黑名单和审核状态决定是否脱敏；
- 联系行为写入事件和审计日志；
- 用户删除和信息过期时同步清理可见性和附件引用。

## 7. 知识库/向量检索的正确定位

把所有岗位、简历或二手物品直接维护成一个知识库，再让大模型自由检索，适合问答型内容，不适合作为分类信息平台的唯一存储和执行方案。

### 7.1 纯知识库方案的缺陷

- **时效性**：岗位和物品会修改、过期、下架，向量索引存在延迟时可能返回无效信息；
- **硬条件**：城市、薪资、价格、年龄、成色和状态需要精确过滤，不能依赖语义相似度；
- **权限和隐私**：联系方式、简历敏感字段和黑名单必须在服务端按 actor 过滤；
- **分页和一致性**：`更多`、候选快照、去重和曝光统计需要稳定的记录集；
- **写操作**：发布、审核、删除、下架、过期和审计需要事务和幂等；
- **可解释性**：运营后台需要查询准确状态和原始字段，不能只看到 embedding 或模型上下文；
- **数据安全**：一个共享知识库容易在检索或 Prompt 传递中产生跨用户数据泄露。

### 7.2 推荐的混合检索架构

```text
MySQL / Domain Store（唯一事实源）
  -> Outbox/Indexer
  -> OpenSearch / pgvector / Qdrant（检索索引）

用户消息
  -> LLM 解析结构化条件
  -> 元数据硬过滤（状态、权限、地区、价格）
  -> 关键词/向量召回正文和标题
  -> 领域排序/有限重排
  -> 服务端脱敏
  -> ListingCard
```

当前数据规模下，可以先继续使用 MySQL 的硬过滤和现有重排；只有当全文检索、同义表达或数据量成为瓶颈时，再增加 OpenSearch、pgvector 或 Qdrant。向量索引是派生数据，删除、下架和过期必须由业务事件驱动同步，不能反过来成为业务真相。

### 7.3 知识库适合承载的内容

知识库/RAG 可以用于：

- 标题和正文的语义召回；
- 工种、品类、品牌和口语别名扩展；
- 推荐结果的相似内容发现；
- 平台规则、帮助文档和客服问答；
- 对已经确定的候选生成自然语言推荐解释。

不应让知识库直接决定：

- 信息是否有效或已审核；
- 用户是否有权查看；
- 联系方式是否可以展示；
- 结果的最终分页和发布状态；
- 删除、发布、审核和下架操作。

因此，RAG 是 Listing Search 的召回组件，不是 Listing Runtime、Domain Store 或权限系统的替代品。
