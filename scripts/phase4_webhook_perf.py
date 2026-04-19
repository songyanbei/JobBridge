"""Phase 4 webhook 响应时间实测（真实 MySQL + Redis）。

验证：phase4-main §5.1 / TC-4.1.9 要求 webhook 端到端响应 < 100ms。

测试路径（用 FastAPI TestClient 驱动，in-process，不含网络 RTT）：
- verify_signature / decrypt_message 跳过（Phase 2 已测过；不是本阶段瓶颈）
- parse_message → 幂等检查（Redis）→ 限流检查（Redis）→ 写 inbound_event（MySQL）
  → 入队（Redis）→ 返回 200
  全部真实走库

三条路径各跑 N=200：
A. Happy path — 每次新 msg_id、新用户：写库 + 入队 + 返回
B. Dedup path — 重复 msg_id：Redis SETNX 命中 → 直接 200
C. Rate-limited path — 同一用户超频：触发限流提示路径

运行：
    cd /mnt/d/work/JobBridge/backend
    source .venv-wsl/bin/activate
    PYTHONPATH=. python /mnt/d/work/JobBridge/scripts/phase4_webhook_perf.py
"""
from __future__ import annotations

import os
import statistics
import sys
import time
from unittest.mock import patch

# ---------------------------------------------------------------------------

ITER = 200
SLA_MS = 100


def _setup_paths():
    backend = "/mnt/d/work/JobBridge/backend"
    if backend not in sys.path:
        sys.path.insert(0, backend)
    os.chdir(backend)


def _cleanup_redis():
    from app.core.redis_client import (
        QUEUE_DEAD_LETTER, QUEUE_INCOMING,
        QUEUE_RATE_LIMIT_NOTIFY, QUEUE_SEND_RETRY, get_redis,
    )
    r = get_redis()
    for pat in ("msg:perf_*", "rate:perf_*", "session:perf_*",
                "lock:perf_*", "rate_limit_notified:perf_*"):
        for k in r.scan_iter(pat):
            r.delete(k)
    for q in (QUEUE_INCOMING, QUEUE_DEAD_LETTER,
              QUEUE_SEND_RETRY, QUEUE_RATE_LIMIT_NOTIFY):
        # 只清本脚本产生的数据不太好做；直接 drain incoming 到 0 简化统计
        pass


def _cleanup_db():
    from sqlalchemy import text as sql_text
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(sql_text(
            "DELETE FROM wecom_inbound_event WHERE from_userid LIKE 'perf_%' "
            "OR msg_id LIKE 'perf_%'"
        ))
        db.commit()
    finally:
        db.close()


def _make_plaintext(msg_id: str, from_user: str, content: str = "hi") -> str:
    return (
        f"<xml>"
        f"<ToUserName>bot</ToUserName>"
        f"<FromUserName>{from_user}</FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        f"<MsgType>text</MsgType>"
        f"<Content>{content}</Content>"
        f"<MsgId>{msg_id}</MsgId>"
        f"</xml>"
    )


def _summary(name: str, samples_ms: list[float]) -> dict:
    samples_sorted = sorted(samples_ms)
    n = len(samples_sorted)
    p50 = samples_sorted[int(n * 0.50)]
    p95 = samples_sorted[int(n * 0.95)]
    p99 = samples_sorted[int(n * 0.99)]
    avg = statistics.mean(samples_ms)
    return {
        "name": name, "n": n,
        "avg": avg, "p50": p50, "p95": p95, "p99": p99,
        "min": min(samples_ms), "max": max(samples_ms),
        "over_sla": sum(1 for s in samples_ms if s > SLA_MS),
    }


def _print_summary(s: dict) -> None:
    ok = "✓" if s["p99"] < SLA_MS else "✗"
    print(
        f"  {ok} {s['name']:<30} "
        f"n={s['n']:3d}  "
        f"avg={s['avg']:6.2f}ms  "
        f"p50={s['p50']:6.2f}  "
        f"p95={s['p95']:6.2f}  "
        f"p99={s['p99']:6.2f}  "
        f"max={s['max']:6.2f}  "
        f"超 SLA={s['over_sla']}"
    )


def bench_happy_path(client, n: int) -> list[float]:
    samples: list[float] = []
    for i in range(n):
        msg_id = f"perf_happy_{time.time_ns()}_{i}"
        from_user = f"perf_user_happy_{i}"  # 每次换用户避免触发限流
        plaintext = _make_plaintext(msg_id, from_user)

        with patch("app.api.webhook.verify_signature", return_value=True), \
             patch("app.api.webhook.decrypt_message", return_value=plaintext):
            t0 = time.perf_counter()
            resp = client.post(
                "/webhook/wecom",
                params={"msg_signature": "x", "timestamp": "t", "nonce": "n"},
                content="<xml><Encrypt>e</Encrypt></xml>",
            )
            dt = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200
        samples.append(dt)
    return samples


def bench_dedup_path(client, n: int) -> list[float]:
    """相同 msg_id 重复 POST — Redis L1 SETNX 短路（不写库不入队）。"""
    msg_id = f"perf_dedup_fixed_{time.time_ns()}"
    from_user = "perf_user_dedup"
    plaintext = _make_plaintext(msg_id, from_user)
    # 预热一次让 Redis 里存在 msg:{msg_id}
    with patch("app.api.webhook.verify_signature", return_value=True), \
         patch("app.api.webhook.decrypt_message", return_value=plaintext):
        client.post(
            "/webhook/wecom",
            params={"msg_signature": "x", "timestamp": "t", "nonce": "n"},
            content="<xml><Encrypt>e</Encrypt></xml>",
        )

    samples: list[float] = []
    for i in range(n):
        with patch("app.api.webhook.verify_signature", return_value=True), \
             patch("app.api.webhook.decrypt_message", return_value=plaintext):
            t0 = time.perf_counter()
            resp = client.post(
                "/webhook/wecom",
                params={"msg_signature": "x", "timestamp": "t", "nonce": "n"},
                content="<xml><Encrypt>e</Encrypt></xml>",
            )
            dt = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200
        samples.append(dt)
    return samples


def bench_rate_limited_path(client, n: int) -> list[float]:
    """同一用户快速连发 — 超过 5 条后走限流路径（不写库、不入队、异步 notify）。"""
    from_user = f"perf_user_rl_{time.time_ns()}"
    # 先刷满限流窗口
    for i in range(6):
        msg_id = f"perf_rl_prewarm_{time.time_ns()}_{i}"
        plaintext = _make_plaintext(msg_id, from_user)
        with patch("app.api.webhook.verify_signature", return_value=True), \
             patch("app.api.webhook.decrypt_message", return_value=plaintext):
            client.post(
                "/webhook/wecom",
                params={"msg_signature": "x", "timestamp": "t", "nonce": "n"},
                content="<xml><Encrypt>e</Encrypt></xml>",
            )

    # 现在该用户应已被限流
    samples: list[float] = []
    for i in range(n):
        msg_id = f"perf_rl_{time.time_ns()}_{i}"
        plaintext = _make_plaintext(msg_id, from_user)
        with patch("app.api.webhook.verify_signature", return_value=True), \
             patch("app.api.webhook.decrypt_message", return_value=plaintext):
            t0 = time.perf_counter()
            resp = client.post(
                "/webhook/wecom",
                params={"msg_signature": "x", "timestamp": "t", "nonce": "n"},
                content="<xml><Encrypt>e</Encrypt></xml>",
            )
            dt = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200
        samples.append(dt)
    return samples


def main():
    _setup_paths()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.webhook import router as webhook_router

    print(f"=== Phase 4 Webhook Perf (真实 MySQL + Redis, in-process) ===")
    print(f"SLA: p99 < {SLA_MS}ms (phase4-main §5.1)")
    print(f"每路径迭代数 = {ITER}")
    print()

    _cleanup_redis()
    _cleanup_db()

    app = FastAPI()
    app.include_router(webhook_router)
    client = TestClient(app)

    # 先打一轮预热（建连接池、触发 SQLAlchemy 编译缓存）
    print("预热 20 次 ...")
    bench_happy_path(client, 20)
    _cleanup_db()

    results = []
    print("\nA. Happy path（每次新 msg_id + 新用户 → 写库 + 入队）")
    s = _summary("A. happy", bench_happy_path(client, ITER))
    _print_summary(s); results.append(s)

    print("\nB. Dedup path（Redis SETNX 命中 → 直接 200）")
    s = _summary("B. dedup", bench_dedup_path(client, ITER))
    _print_summary(s); results.append(s)

    print("\nC. Rate-limited path（超频 → 不写库 + notify 异步 push）")
    s = _summary("C. rate-limited", bench_rate_limited_path(client, ITER))
    _print_summary(s); results.append(s)

    print("\n" + "=" * 60)
    all_ok = all(s["p99"] < SLA_MS for s in results)
    if all_ok:
        print(f"✓ 全部路径 p99 < {SLA_MS}ms，满足 SLA")
    else:
        print(f"✗ 至少一条路径 p99 超过 {SLA_MS}ms：")
        for s in results:
            if s["p99"] >= SLA_MS:
                print(f"  - {s['name']}: p99={s['p99']:.2f}ms")

    # 清理
    _cleanup_db()

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
