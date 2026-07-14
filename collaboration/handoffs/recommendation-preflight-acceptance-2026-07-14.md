# 推荐优化前置修复验收记录

> 状态：已通过
> 验收日期：2026-07-14
> 验收范围：F0 / F3 / F4 / F5 前置修复
> 运行工作树：`/mnt/d/work/JobBridge-main`
> 验收 commit：`5800a3e Merge pull request #8 from songyanbei/codex/recommendation-preflight-fixes`

---

## 1. 验收结论

本轮推荐优化前置修复已通过验收。

验收覆盖后端 CI 门禁、搜索 SQL 修复、薪资区间召回、Phase5 rollout gate 统一、演示脚本与运行数据一致性。当前服务运行正常，演示库已补齐 9 步演示所需数据，`9500+` 薪资区间直接命中场景成立。

---

## 2. 修复范围

| 阶段 | 内容 | 验收状态 |
|---|---|---|
| F5 | 新增后端 unit + MySQL 搜索集成 CI 门禁 | 通过 |
| F0 | 修复 `JSON_CONTAINS` JSON 标量序列化问题 | 通过 |
| F4 | 修复岗位薪资按区间上限覆盖匹配 | 通过 |
| F3 | 统一 Phase5 search takeover 与 post-search dispatch rollout gate | 通过 |
| 文档/演示 | 修正 9 步 demo 脚本、补充种子脚本和实际输出口径 | 通过 |

---

## 3. 运行环境核验

| 项 | 结果 |
|---|---|
| 后端服务 | 正常 |
| 前端服务 | 正常 |
| MySQL | 正常 |
| Redis | 正常 |
| Worker | 正常 |
| `/health` | `db.ok=true` |
| 前端入口 | `5173` 正常返回并跳转 `/admin/` |

运行进程包含：

- `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- `vite --host 0.0.0.0`
- `mariadbd`
- `redis-server`
- `python -m app.services.worker`

---

## 4. 测试证据

### 4.1 GitHub Actions

Backend CI 已通过：

- Run: `https://github.com/songyanbei/JobBridge/actions/runs/29193952883`
- 结论：`success`
- 说明：CI 隔离 MySQL 门禁已验证 F0 / F4 相关真实 MySQL 行为。

### 4.2 本地 / WSL 验证

在 `/mnt/d/work/JobBridge-main/backend` 执行：

```bash
.venv-wsl/bin/python -m pytest -p no:cacheprovider tests/unit -q
```

结果：

```text
1119 passed
```

重点搜索与 Phase5 单测：

```bash
.venv-wsl/bin/python -m pytest -p no:cacheprovider \
  tests/unit/test_search_service.py \
  tests/unit/test_phase5_p1_fixes.py -q
```

结果：

```text
64 passed
```

---

## 5. 演示数据核验

已执行 `scripts/demo_seed_supplement.sql`，当前演示库关键数据如下：

| 检查项 | 结果 |
|---|---:|
| `demo_supp_v1` 补充岗位 | 7 条 |
| 苏州电子厂 5500+ 区间命中 | 8 条 |
| 苏州电子厂 7000+ 区间命中 | 8 条 |
| 苏州电子厂 9500+ 区间命中 | 4 条 |

关键 F4 场景成立：

- `8600~11000`
- `8800~11500`
- `9000~12000`

这些岗位虽然薪资下限低于 `9500`，但薪资上限覆盖 `9500`，应被 `9500+` 查询直接召回。当前演示库已验证该口径成立。

---

## 6. 审核与文档口径核验

| 项 | 结果 |
|---|---|
| `代开发票` 敏感词 | 存在 |
| 敏感词等级 | `high` |
| 第 3 步审核口径 | 自动驳回 |
| LLM 安全检查 | 当前为预留 no-op，文档已避免描述为已启用 |
| 岗位前台格式 | 已按当前 formatter 对齐 |
| show_more 数量 | 文档已改为以实际回复提示为准 |

---

## 7. 已知说明

`RUN_INTEGRATION=1 pytest tests/integration/test_search_sql_mysql.py` 不建议直接跑在当前演示库上作为最终判断依据。

原因：当前演示库已有大量基础简历和岗位种子数据，其中 1 个集成测试假设运行在隔离空库中；在演示库直接运行会因为既有简历被一起召回而出现断言偏差。该问题不代表业务逻辑失败，也不代表 F0 / F4 修复失败。

验收时以 GitHub Actions 的隔离 MySQL 门禁结果为准。

---

## 8. 结论

F0 / F3 / F4 / F5 前置修复阶段通过验收。

当前可进入下一阶段：Phase5 对话体验与推荐解释优化，包括低召回提示、放宽策略解释、show_more 翻完后的建议，以及推荐理由展示能力的产品化设计。
