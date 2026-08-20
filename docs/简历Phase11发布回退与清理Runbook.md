# 简历 Phase 11 发布、回退与清理 Runbook

本文是执行清单，不授权连接预发布或生产。本阶段只交付代码、隔离测试和
操作步骤；所有真实环境命令、迁移、灰度及硬删除均由发布方审批后人工执行。

## 1. 发布前证据

- 记录部署清单中的 API/worker 实例、build number、SHA 和 capability；实际
  探针必须逐实例匹配清单且不低于 manifest 的 minimum build。
- 在脱敏快照完成 runner `check/apply/resume/verify/down` 演练，归档 ledger、
  checksum、双快照 verify 摘要和故障恢复结果。
- Phase 11 CI 三项必须全绿：backend unit、隔离 MySQL/Redis（0 skip）、
  frontend lint/unit/build。
- 五个开关保持关闭；备份和恢复演练可用；确认没有旧 writer。

### Runner 命令模板（仅供已审批人工执行）

以下命令从仓库根目录执行。尖括号内容必须替换为变更单中的真实值；DSN
应由密钥注入，不得提交到仓库或复制进工单正文。本次阶段 7 不执行这些命令。
`--build-probe-url` 要为部署清单中的每个实际 API/worker 实例重复提供一次，
不能只探测负载均衡地址。

```bash
export PHASE11_DSN='<mysql+pymysql://user:password@host:3306/database>'
export PHASE11_REDIS_DSN='<redis://host:6379/db>'
export PHASE11_REDIS_NAMESPACE='<approved-isolated-namespace>'
MANIFEST='backend/sql/migrations/phase11_manifest.json'
RUNNER='backend/scripts/apply_phase11_migrations.py'
EXECUTOR='<change-ticket-or-operator-id>'
CUTOVER_RESUME_ID='<recorded-cutover-resume-id>'
PROBES=(
  --build-probe-url '<https://api-instance-1/internal/phase11-build>'
  --build-probe-url '<https://worker-instance-1/internal/phase11-build>'
)

python "$RUNNER" manifest-check --manifest "$MANIFEST"
python "$RUNNER" check --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage pre_cutover
python "$RUNNER" apply --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage pre_cutover --executed-by "$EXECUTOR"

python "$RUNNER" check --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage post_cutover "${PROBES[@]}" \
  --redis-dsn "$PHASE11_REDIS_DSN" \
  --redis-namespace "$PHASE11_REDIS_NAMESPACE"
python "$RUNNER" apply --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage post_cutover "${PROBES[@]}" \
  --cutover-resume-id "$CUTOVER_RESUME_ID" --executed-by "$EXECUTOR" \
  --redis-dsn "$PHASE11_REDIS_DSN" \
  --redis-namespace "$PHASE11_REDIS_NAMESPACE"

# 仅在同一步 ledger 为 running/failed 且复核最后水位后使用 resume。
python "$RUNNER" resume --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage post_cutover "${PROBES[@]}" \
  --cutover-resume-id "$CUTOVER_RESUME_ID" --executed-by "$EXECUTOR" \
  --redis-dsn "$PHASE11_REDIS_DSN" \
  --redis-namespace "$PHASE11_REDIS_NAMESPACE"
python "$RUNNER" verify --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --executed-by "$EXECUTOR"

# down 是最后手段。完成停写、备份、门禁清零及单独审批后才能执行。
python "$RUNNER" check --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage down "${PROBES[@]}"
python "$RUNNER" apply --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage down "${PROBES[@]}" \
  --cutover-resume-id "$CUTOVER_RESUME_ID" --confirm-down \
  --executed-by "$EXECUTOR"
# down 中断后仍须保留同一水位、探针集合和显式确认。
python "$RUNNER" resume --manifest "$MANIFEST" --dsn "$PHASE11_DSN" \
  --stage down "${PROBES[@]}" \
  --cutover-resume-id "$CUTOVER_RESUME_ID" --confirm-down \
  --executed-by "$EXECUTOR"
```

## 2. 五开关顺序

1. additive/config 后部署最低兼容 build，执行停写屏障，摘除旧实例；兼容
   双写始终启用，不受业务开关控制。
2. post-cutover backfill/reconcile 完成且 runner `verify` 为 verified 后开启
   `RESUME_LIFECYCLE_V2_ENABLED`。
3. 数据库 allowlist revision 更新后开启 `RESUME_REPLACEMENT_ENABLED`，先测试
   cohort，再逐步扩大；既有 assignment 不重新分组。
4. 开启 `RESUME_CANDIDATE_CLEANUP_ENABLED`，确认候选水位收敛；再开启
   `RESUME_EXPIRY_CLEANUP_ENABLED`，观察至少两个 10 分钟周期。
5. 最后才开启 `RESUME_HARD_DELETE_ENABLED`。前置条件：ledger verified、活动
   媒体问题/target dead-letter/media dead-letter/orphan 均为 0，容量演练的
   `EXPLAIN ANALYZE` 稳定使用 `idx_resume_hard_delete`。

任何异常先按相反顺序关停，第一步永远是关闭硬删除。关闭 replacement 只
阻止新候选；已有 `awaiting_review/conflict` 必须 drain、取消或经授权 close。

## 3. 监控阈值

| 指标 | 告警/阻断 |
|---|---|
| `resume_lifecycle_invalid_count` | `>0`：暂停写入并关闭严格开关 |
| writer build skew | 任一实例低于 minimum build：不得 cutover |
| expiry due / oldest lag | 连续两个周期 `>0` / `>1200s` |
| candidate oldest lag | `>30min` 或积压持续增长 |
| replacement conflict ratio | `>5%` 或突增 |
| target/media dead-letter | `>0` 告警并禁止硬删 |
| unresolved media issue | `>0` 禁止硬删 |
| orphan cleanup pending | 超过一个重试窗口禁止 verify |
| hard-delete gate blocked | 持续超过一个硬删延迟周期 |
| update command failure | 连续 15 分钟超过基线 2 倍 |

## 4. dead-letter 处置

1. `operator` 可查询；只有 `super_admin` 可重驱或处置媒体。
2. target/media 重驱必须填写脱敏原因；每批最多 50 条，每管理员每分钟最多
   2 批。活动租约和非 dead-letter 行不得重驱。
3. 逐项核对返回结果和审计：只记录 ID、前后状态、attempt count、操作人和
   原因，不复制 URL、object key、简历正文或用户标识。
4. 媒体处置必须由两个不同管理员分别审批、执行；执行人不得自批。
5. 重驱失败继续按既有上限进入 dead-letter。不得用“审批例外”绕过最终
   verify 或开启硬删除。

## 5. 回退

- 业务回退：关闭硬删、expiry/candidate cleanup、replacement、严格 lifecycle；
  兼容 DTO/双读/无条件双写不得回退。target/media worker 保持运行至 drain。
- 二进制只能回退到支持 nullable DTO 和双写的 minimum build。需要更旧版本时，
  必须先停写并完成数据库 down。
- 数据库 down 是最后手段：停写和 worker 屏障、活动关系归零、清理门禁归零、
  down backup/行数/SHA-256 完成后运行 runner down。固定策略是恢复 legacy
  非空 TTL 并保留软删候选；不得临时物理删除。
- checksum 漂移、未知 schema、NULL TTL、备份摘要不一致均 fail closed；不得
  手工改 ledger 水位。

## 6. 真实环境人工清单

- [ ] 变更单、执行人/复核人、维护窗口和回退负责人已批准
- [ ] 脱敏快照预检、容量计划、备份恢复演练证据已归档
- [ ] 部署清单与逐实例 API/worker 探针 SHA/capability 一致
- [ ] 停写屏障确认在途事务为 0，记录 cutover resume ID
- [ ] pre/post-cutover runner 每一步 ledger 和 checksum 经双人复核
- [ ] verify 连续两轮摘要一致，媒体/orphan/dead-letter 均为 0
- [ ] 每次 allowlist revision 和五开关变更均有时间、操作者、观测结果
- [ ] 硬删开启前 `EXPLAIN ANALYZE` 证据已批准
- [ ] 连续观察至少 7 天或一个完整硬删延迟周期
- [ ] 未自动删除迁移备份、down 备份或 ledger
