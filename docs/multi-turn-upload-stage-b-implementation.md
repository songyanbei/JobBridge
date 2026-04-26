# 多轮上传 Stage B 开发实施与验收说明

> 面向开发分发。目标是在 Stage A 修复演示阻塞 bug 后，提升字段抽取、类目规整、搜索默认条件和 0 命中 fallback 的质量。

---

## 1. 目标

Stage B 解决的是“能跑之后推荐质量不稳”的问题：

```text
用户：北京饭店招聘厨师，底薪7500+绩效，包吃不包住，招2人

期望：
1. 厨师稳定规整到餐饮或餐饮/厨师类目。
2. 发布岗位后搜索工人时，能用 city/job_category/salary 等字段形成有效 criteria。
3. 搜索 0 命中时按明确 fallback 顺序放宽，不直接给空泛文案。
4. 兜底文案不伪装成推荐结果。
```

Stage B 不做完整状态机重构，不引入 `active_flow + UploadDraft + SearchState`，不做 `upload_conflict` 确认态。

---

## 2. 改动范围

建议改动范围：

```text
backend/app/llm/prompts.py
backend/app/services/intent_service.py
backend/app/services/search_service.py
backend/app/services/message_router.py
backend/app/services/dict_service.py
backend/tests/unit/
```

可选数据补充：

```text
mock-testbed seed 或本地测试 seed：北京/深圳/餐饮/厨师相关岗位和简历
```

---

## 3. 实施步骤

### 3.1 prompt 增加类目映射 few-shot

文件：`backend/app/llm/prompts.py`

在 intent prompt 中补充 closed enum 和 few-shot，重点覆盖演示和常见蓝领场景：

```text
厨师 / 服务员 / 后厨 / 饭店 / 餐厅 -> 餐饮
打包工 / 分拣 / 仓库 / 快递 / 装卸 -> 物流仓储
普工 / 操作工 / 产线 / 流水线 -> 普工
电子厂 / SMT / 组装 / 质检 -> 电子厂
保洁 / 清洁 / 客房清洁 -> 保洁
保安 / 门岗 / 巡逻 -> 保安
```

要求：

1. prompt 仍输出 canonical key。
2. 搜索条件中的 `city`、`job_category` 仍统一可接受 list。
3. 上传字段中的 `job_category` 保持 str。
4. 修改 prompt 后 bump `PROMPT_VERSION` 或对应 intent prompt version，便于日志回溯。

### 3.2 intent_service 字段规整层

文件：`backend/app/services/intent_service.py`

在 `_sanitize_intent_result` 后增加字段规整，或在 `_sanitize_intent_result` 内分段处理。

建议新增 helper：

```python
def _normalize_structured_data(data: dict, role: str, intent: str) -> dict:
    ...

def _normalize_criteria_patch(patches: list[dict]) -> list[dict]:
    ...
```

规整规则：

| 字段 | 规则 |
|---|---|
| `city` | str/list 都规整为业务期望形态；去空值、去重 |
| `job_category` | 映射到字典标准类目；无法映射时保留原值但记录 warning |
| `expected_cities` | list，去空值、去重 |
| `expected_job_categories` | list，映射到字典标准类目 |
| `salary_floor_monthly` | 尽量转 int；非法值丢弃 |
| `salary_ceiling_monthly` | 尽量转 int；小于 floor 时丢弃 ceiling 或记录 warning |
| `headcount` | int 且 > 0 |

如果已有 `dict_service` 支持城市/工种字典，优先复用；如果没有最近邻能力，Stage B 可先用静态 synonym map，后续再接字典表。

### 3.3 criteria 默认补齐

文件：`backend/app/services/message_router.py`

落点：`_run_search` 或新 helper。

固定合并顺序，已有有效值不覆盖：

```text
1. 当前请求 criteria
2. session.search_criteria 或后续 search_state.last_criteria
3. 仅 worker 角色：用户最近一份 passed resume 的 expected_cities / expected_job_categories
```

“已有有效值”定义：

```text
key 存在且 value 非 None、非空字符串、非空列表。
```

要求：

1. 合并后的 criteria 再交给 `search_service`。
2. `_query_jobs/_query_resumes` 底层不查用户简历。
3. 每次有效搜索，不论是否命中，都写入 `last_criteria` 或当前过渡字段。

### 3.4 search_service 0 命中 fallback

文件：`backend/app/services/search_service.py`

建议将 fallback 做成明确步骤，便于测试：

```text
Step 0: 原始 criteria 精确查
Step 1: 薪资放宽 10%
Step 2: 同城无结果时，可考虑同省/周边城市（如果已有城市字典支持）
Step 3: 工种细分类放宽到大类，例如 餐饮/厨师 -> 餐饮
Step 4: 仍无结果，返回空并给清晰兜底文案
```

Stage B 可以先实现 Step 1 和 Step 3；同省/周边城市依赖城市字典，不具备数据时不要硬做。

要求：

1. fallback 必须记录日志，包含 fallback step 和候选数量。
2. 不允许 fallback 到无 city/job_category 的全表召回。
3. fallback 后仍需过权限过滤和 rerank。

### 3.5 兜底文案优化

文件：`backend/app/services/message_router.py` 或 `search_service.py`

0 命中文案要求：

```text
不要写成“为您推荐以下...”。
要明确说明暂未找到，并引导用户补充或放宽条件。
```

示例：

```text
您的岗位信息已入库，将进入匹配池。
暂未找到完全匹配的求职者。可以补充期望年龄、经验，或稍后再查看新的求职者。
```

搜索场景：

```text
暂未找到符合条件的结果。您可以放宽城市、工种或薪资范围后再试。
```

---

## 4. 必测用例

单测至少覆盖：

| Case | 期望 |
|---|---|
| “厨师/饭店/后厨” | 规整为餐饮相关类目 |
| “打包/分拣/仓库” | 规整为物流仓储 |
| 非法薪资 | 被丢弃或不进入查询 |
| `city=[]` + 简历默认城市 | 可用简历城市补齐 |
| criteria 只有 headcount | 仍返回空，不全表召回 |
| 精确搜索 0 命中，薪资放宽命中 | 返回 fallback 后结果并记录 step |
| 精确搜索 0 命中，工种大类放宽命中 | 返回 fallback 后结果 |
| 全部 fallback 仍 0 命中 | 返回明确“暂未找到”文案 |

建议运行：

```powershell
cd backend
pytest tests/unit/test_intent_service.py tests/unit/test_search_service.py tests/unit/test_message_router.py
```

---

## 5. 手测验收

### 场景 1：餐饮类目规整

```text
factory：北京饭店招聘厨师，底薪7500+绩效，包吃不包住，招2人
```

通过标准：

```text
job_category 稳定落到餐饮类目
岗位入库成功
不会被识别成电子厂/其他
```

### 场景 2：默认条件补齐

```text
worker 已有 passed 简历：expected_cities=["无锡"], expected_job_categories=["电子厂"]
worker：看看新岗位
```

通过标准：

```text
系统能从简历补 city/job_category
不会因为当前文本缺 city/job_category 直接失败
不会全表召回
```

### 场景 3：0 命中 fallback

```text
worker：北京找厨师，9000以上
```

通过标准：

```text
精确无结果时按步骤放宽
若仍无结果，回复“暂未找到”，不伪装推荐
```

---

## 6. 验收标准

1. 常见工种表达能稳定映射到标准类目。
2. 字段规整不会引入未知 key，也不会让非法值进入查询。
3. 默认 criteria 合并顺序固定，空值可被下层默认补齐，已有有效值不被覆盖。
4. 搜索 fallback 有明确步骤和日志。
5. 0 命中不会全表召回。
6. 0 命中文案不会伪装成推荐结果。
7. Stage A 的多轮补字段链路不回退。

