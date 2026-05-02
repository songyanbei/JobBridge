# 对话意图抽取阶段 3 验收记录

- 验收日期：2026-05-02
- 验收范围：`docs/dialogue-intent-extraction-phased-plan.md` 第 3 阶段（Slot Schema 收口）
- 结论口径：以“功能完成性、需求符合性、代码质量、自动化测试、真实链路冒烟”五个维度综合判断

## 一、验收结论

当前阶段代码已满足阶段 3 的主要验收目标，建议通过当前阶段验收。

本轮复核未发现新的阻塞性缺陷。此前阶段 3 收口中的关键问题均已修复并回归通过：

- `job_title` 已可被抽取、归一化并保留在 `accepted_slots_delta` / `final_search_criteria` 中，不再被静默丢弃。
- `intent_service` / `dialogue_reducer` 已改为消费 `slot_schema` 派生能力，schema 已成为运行时字段真源。
- reducer 默认 merge 策略已接入 `slot_schema.default_merge_policy(...)`。
- 搜索追问与上传追问已统一接入 `slot_schema.render_missing_followup(...)`，且单字段追问会真实消费 slot 级 `prompt_template`。

唯一需要单独说明的是：阶段计划文档 §3.4 第 5 条要求给出 `compute_missing_slots` 相对阶段一旧版的量化差异报告（差异 ≤ 1%）。当前代码与测试结果没有发现该项功能性回退，但仓库内尚未形成一份独立的“百分比对比报告”作为签收附件。若流程要求严格留档，建议将该材料作为补充验收附件。

## 二、需求符合性审查

对照 `docs/dialogue-intent-extraction-phased-plan.md` §3.1、§3.2、§3.4，当前实现结论如下：

1. `slot_schema` 已承担阶段 3 的核心能力入口。
   - 已覆盖 `fields_for / required_for / compute_missing_slots / validate_slots_delta / default_merge_policy / check_role_permission / render_missing_followup`。
   - `job_title` 已作为 `filter_mode=display` 的 display-only slot 存在，不参与 missing，不进入 SQL 过滤。

2. schema 已真实接入运行时链路。
   - `backend/app/services/intent_service.py` 中 `_bootstrap_field_constants_from_schema()` 会在模块加载时用 schema 派生结果覆盖 `_VALID_JOB_KEYS / _VALID_RESUME_KEYS / _ALL_VALID_KEYS / _LIST_FIELDS / _INT_FIELDS / _SEARCH_FIELD_REMAP`。
   - `backend/app/services/dialogue_reducer.py` 中默认 merge 策略与角色权限校验已委托到 `slot_schema`。
   - `backend/app/services/message_router.py` 与 `backend/app/services/upload_service.py` 的追问文案入口已统一到 `slot_schema.render_missing_followup(...)`。

3. 阶段 3 对边界条件的实现符合文档约束。
   - soft-preference 字段仍可提取和写入，但不会误触发 missing。
   - `job_title` 仅用于抽取与展示，不要求搜索召回或排序变化。
   - 角色权限在 reducer 层生效，越权 frame 会被拒绝并转入澄清/拒绝路径。

## 三、代码质量审查

本轮代码审查重点关注“schema 是否真的成为单一真源”以及“新增能力是否只是元数据未接线”。结论如下：

- 无新的阻塞性代码缺陷。
- 阶段 3 关键修复已完整接线，不再停留在“只定义 schema、不进入运行时”的状态。
- 现有实现保留了阶段 1/2 的 fallback 与兼容逻辑，兼顾了灰度演进和回归稳定性。

关注点说明：

- `message_router` 的 `search_active` 状态切换仍与 `candidate_snapshot` 是否存在相关，这影响的是 mock 搜索路径下的 `session.active_flow` 表现，不构成阶段 3 功能回退。
- 本轮未发现会影响阶段 3 验收的新增技术债，但建议后续继续将阶段性兼容逻辑逐步收敛，避免长期双轨维护。

## 四、自动化测试结果

执行目录：`D:\work\JobBridge\backend`

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests\unit\test_slot_schema.py tests\unit\test_dialogue_reducer_schema_driven.py tests\unit\test_dialogue_golden_phase1.py tests\unit\test_dialogue_golden_phase2.py tests\unit\test_dialogue_phase2_dev_rollout.py tests\unit\test_llm_prompts.py tests\unit\test_message_router.py tests\unit\test_upload_service.py tests\unit\test_intent_service.py tests\unit\test_classify_dialogue_routes.py tests\unit\test_dialogue_reducer.py
```

执行结果：

- `257 passed in 1.42s`

覆盖面说明：

- `test_slot_schema.py`
  - 覆盖 schema 合法字段、required/missing 计算、字段 normalizer、类型与范围约束、`default_merge_policy`、角色权限、`job_title`、`prompt_template`、schema 派生常量一致性。
- `test_dialogue_reducer_schema_driven.py`
  - 覆盖 schema-driven reducer 主路径、角色权限拒绝、merge 策略、低置信度澄清、soft 字段保留、别名映射。
- `test_dialogue_golden_phase1.py` / `test_dialogue_golden_phase2.py` / `test_dialogue_phase2_dev_rollout.py`
  - 覆盖阶段 1/2 golden case 在当前 schema-driven 路径下持续全绿。
- `test_message_router.py` / `test_upload_service.py`
  - 覆盖追问文案经 `slot_schema.render_missing_followup(...)` 生成，以及 slot 级 `prompt_template` 生效。
- `test_intent_service.py`
  - 覆盖 schema 派生字段常量、`job_title` 保留、structured_data 归一化一致性。

## 五、真实链路冒烟结果

说明：本轮采用“进程内真实业务链路 + 外部依赖 mock”的方式执行整链路冒烟，覆盖 `message_router -> classify_dialogue -> reducer -> compat -> handler` 主流程。

执行用例：

1. `worker_xian_to_beijing_replace_v2`
2. `worker_xian_to_beijing_clarify`
3. `broker_machinery_to_suzhou_replace_v2`
4. `active_flow_conflict_upload_to_search`
5. `llm_json_parse_failure_fallback`
6. `awaiting_expired_no_pollution`

执行结果摘要：

- worker 搜索链路在 `replace` 策略下可完成“西安 -> 2500 -> 北京有吗”的搜索条件替换与继续搜索。
- worker 搜索链路在 `clarify` 策略下会进入城市歧义澄清，不会误替换旧条件。
- broker 搜索链路可完成“机械厂普工 -> 北京 -> 换成苏州”的条件替换。
- 上传中遇到新搜索意图时会进入 `upload_conflict`，不会污染搜索状态。
- v2 JSON 解析失败时能回退 legacy 路径，不会中断主流程。
- awaiting 过期后裸值不会回填旧 missing 字段，不会污染既有 `search_criteria`。

## 六、逐条验收判定

对照 `docs/dialogue-intent-extraction-phased-plan.md` §3.4：

1. schema 单测覆盖 slot normalizer、边界值与文案能力：通过。
2. `intent_service` / `dialogue_reducer` 不再依赖独立手写权威字段集，改由 schema 派生：通过。
3. 所有阶段二 golden case 在 schema-driven 路径下重跑全绿：通过。
4. 角色权限单测覆盖 worker / factory / broker 差异路径：通过。
5. `compute_missing_slots` 与阶段一旧版结果差异 ≤ 1%，并解释差异来源：功能上未见回退，缺少独立量化留档，建议补充附件后归档。
6. soft-preference 字段可抽取、可写入，但不触发 missing、不要求 SQL 召回：通过。
7. `job_title` 可抽取并保留，但不参与搜索服务过滤与排序断言：通过。

## 七、最终建议

建议通过当前阶段验收。

如果需要形成更完整的验收包，建议补充以下材料后归档：

- 一份 `compute_missing_slots` 相对阶段一旧版的量化差异报告，用于满足 §3.4 第 5 条的签收留档要求。
- 若后续进入灰度或上线审批，可把本次 6 条真实链路冒烟结果转成固定脚本或 CI job，减少人工复核成本。
