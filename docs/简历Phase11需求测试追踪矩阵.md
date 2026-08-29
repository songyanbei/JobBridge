# 简历 Phase 11 需求—测试追踪矩阵

本矩阵以“最小可执行单元”为原则：每行只证明一个可观察边界，阶段 7
的发布门禁串行调用既有单元，不建立难以定位失败的巨型测试。CI 入口为
`python backend/scripts/phase11_release_gate.py release`；任何 integration skip
均视为失败。

## 发布旅程

同一个 release gate 按小单元串联以下可定位旅程：更新命令和空白多轮草稿
（stage 3 unit）→ 审核及原子替换（stage 4 unit/MySQL）→ 搜索排除旧、候选、
过期记录（stage 2 visibility）→ cleanup/fence（stage 5 MySQL/Redis）→ 延迟
硬删及媒体/关系/ledger 门禁（stage 5 cleanup unit）。每个节点仍可单独运行；
任一节点失败时不需要从巨型场景日志反推原因。

| 需求边界 | 自动化证据 | 门禁 |
|---|---|---|
| additive DDL、manifest checksum、ledger 断点、up/down | `test_phase11_stage1_migration_mysql.py` | Phase 11 MySQL |
| 激活时 UTC 生命周期双写、事务失败回滚 | `test_phase11_stage2_activation_mysql.py` | Phase 11 MySQL |
| `/更新简历 [ID]`、精确别名、空白草稿、无字段继承 | `test_resume_replacement_stage3_units.py` | backend unit |
| 同一旧简历只产生一个活动关系 | `test_resume_replacement_stage3_mysql.py` | Phase 11 MySQL |
| 审核/过期锁顺序、`base+1/expired` 例外、全图回滚 | `test_resume_replacement_stage4_mysql.py` | Phase 11 MySQL |
| 推荐晚到写复核、outbox 二次复核、Redis fence | `test_resume_phase11_stage5_fences.py` | Phase 11 MySQL/Redis |
| 过期、候选回收、租约、continuation、硬删门禁 | `test_resume_phase11_cleanup_units.py` | backend unit |
| dead-letter 限批/限速和媒体双人处置 | `test_resume_admin_stage6_mysql.py` | Phase 11 MySQL |
| 后台权限、候选/历史操作限制和稳定冲突原因 | `test_resume_admin_stage6_routes.py`、`test_resume_admin_stage6_units.py` | backend unit |
| 空时间、状态、按钮禁用、权限与冲突 | `frontend/src/views/**/*.spec.js` | frontend unit |
| 岗位生命周期不回归 | `test_job_expiry_cleanup.py`、`test_job_candidate_cleanup.py`、`test_job_media_hard_delete_delay_mysql.py` | backend unit / Phase 10 MySQL |
| 媒体对象存储失败可重试且不假成功 | `test_media_cleanup_worker.py` | backend unit |
| worker commit 与 Redis 故障边界 | `test_target_cleanup_checkpoint_redis_mysql.py`、`test_session_commit_redis_unavailable_mysql.py` | Phase 10 MySQL/Redis |

## 人工容量证据

十万条积压和生产量级 `EXPLAIN ANALYZE` 不进入默认 CI，避免把容量结论
误当成功能单测。发布方必须在隔离的 MySQL 8 数据库或脱敏快照上完成：

1. 建立至少 100,000 条到期/候选测试数据，分别运行过期和候选 worker；
2. 记录每批数量、continuation、水位、租约续期、总耗时与失败重试次数；
3. 对 `deleted_at,id` keyset 扫描运行 `EXPLAIN ANALYZE`，确认使用
   `idx_resume_hard_delete`，无 filesort/全表扫描；
4. 归档脱敏计划和数据规模。计划不稳定时保持
   `RESUME_HARD_DELETE_ENABLED=false`。

这些结果属于发布证据，不在仓库中伪造“已通过”。
