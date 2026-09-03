#!/usr/bin/env bash
# Legacy demo cleanup entrypoint.
#
# This command is intentionally disabled. The old implementation guessed
# ownership from userid prefixes and cleared shared Redis queues, which could
# affect non-demo traffic. Demo data must be cleaned through the isolated
# /admin/demo/{demo_id} control plane, where the workspace scope is previewed
# and every stage is checkpointed and retryable.

set -euo pipefail

cat >&2 <<'EOF'
[拒绝] scripts/demo_env_cleanup.sh 已停用，未执行任何数据库或 Redis 操作。

请使用演示工作区控制面完成下架与清理：
  1. POST /admin/demo/{demo_id}/disable
  2. GET  /admin/demo/{demo_id}/preview
  3. POST /admin/demo/{demo_id}/cleanup
  4. 失败时 POST /admin/demo/{demo_id}/cleanup/retry
  5. 确认 workspace 状态为 cleaned 后，再由发布负责人按迁移流程人工执行
     phase17_down_001_demo_control_plane.sql。

该脚本不会按 userid 前缀删除数据，也不会清空共享 Redis 队列。
EOF
exit 2
