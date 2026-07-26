"""独立 prepared session reconciler（§10.4.1 行 2370）。

与 delivery dispatcher 一样是 Worker 内的独立线程，固定每 250ms 扫描一次：先幂等
完成 Redis session CAS，再把 delivery 从 prepared 转 pending 并清空
``session_patch_ciphertext``（§9.11 行 2110）。

CAS 失败或 patch 无法解密时保持 prepared 并告警（§10.6），绝不发送未提交 session
的推荐消息。
"""
from __future__ import annotations

from typing import Any

SCAN_INTERVAL_SECONDS = 0.25
BATCH_SIZE = 100


def run_once(worker: Any) -> int:
    """执行一次 prepared session 恢复扫描，返回本次 claim 到的条数。"""
    return worker.reconcile_sessions_once(limit=BATCH_SIZE)
