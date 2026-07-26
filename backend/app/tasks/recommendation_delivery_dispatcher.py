"""独立 delivery dispatcher（§10.4.1 行 2365-2375）。

每个 Worker 启动一条**独立**线程执行 :func:`run_once`：

- 固定每 250ms 扫描一次，每批最多 100 条；
- 使用 ``FOR UPDATE SKIP LOCKED`` + 条件 UPDATE claim，多 Worker 靠 DB lease 竞争，
  不依赖 Redis 唤醒消息的正确性；
- **不读取 incoming 队列**：恢复扫描一旦挂在主循环上，一条慢 LLM 消息期间整个
  delivery 恢复就会停摆（评审 P1-15）。

正常路径仍然是新建 pending delivery 在用户锁内立即 claim/send，本模块只负责恢复
prepared→pending 之后遗留的 pending/retry_wait 积压。
"""
from __future__ import annotations

from typing import Any

SCAN_INTERVAL_SECONDS = 0.25
BATCH_SIZE = 100


def run_once(worker: Any) -> int:
    """执行一次 dispatcher 扫描，返回本次 claim 到的条数。"""
    return worker.dispatch_deliveries_once(limit=BATCH_SIZE)
