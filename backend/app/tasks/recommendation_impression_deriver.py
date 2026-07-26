"""独立 impression deriver（§10.5 行 2400-2403）。

sent 状态提交后当前 Worker 会先把派生任务投给有界 impression executor，但无论那次
即时派生是否完成，本线程都固定每 250ms 扫描恢复一次，SLO 为
``sent → completed`` P95 ≤ 500ms、P99 ≤ 2s。

派生一律经过 ``claim_impression_deliveries`` 的条件 claim + ``impression_lease_*``
租约；任何绕过 claim 的内联派生都会和本线程对同一条 delivery 并发执行（评审 P1-15）。
"""
from __future__ import annotations

from typing import Any

SCAN_INTERVAL_SECONDS = 0.25
BATCH_SIZE = 100


def run_once(worker: Any) -> int:
    """执行一次曝光派生扫描，返回本次 claim 到的条数。"""
    return worker.derive_impressions_once(limit=BATCH_SIZE)
