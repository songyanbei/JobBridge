"""Stress driver: concurrent POST /mock/wework/inbound with latency stats.

Run from inside WSL:
    /tmp/mock-testbed-venv/bin/python wsl-stress-driver.py \\
        --total 1000 --concurrency 50 --target http://127.0.0.1:8001

Reports:
- Throughput (req/s)
- Latency percentiles (p50 / p90 / p95 / p99 / max)
- HTTP status counts
- Redis queue:incoming growth (before/after snapshot)
- Worker drain observation (queue depth sampled every 2s for 30s)
"""
import argparse
import asyncio
import json
import statistics
import subprocess
import time
import uuid

import httpx


_USERS = [
    "wm_mock_worker_001",
    "wm_mock_worker_002",
    "wm_mock_factory_001",
    "wm_mock_broker_001",
]


def make_payload(i: int) -> dict:
    return {
        "ToUserName":   "wwmock_corpid",
        "FromUserName": _USERS[i % len(_USERS)],
        "CreateTime":   int(time.time()),
        "MsgType":      "text",
        "Content":      f"stress#{i} 我想找深圳打包工 你好",
        "MsgId":        f"stress_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}",
        "AgentID":      "1000002",
    }


def redis_llen(queue: str = "queue:incoming") -> int:
    """Query Redis via docker exec (no direct Redis client dep)."""
    try:
        r = subprocess.run(
            ["docker", "exec", "jobbridge-redis", "redis-cli", "LLEN", queue],
            capture_output=True, text=True, timeout=5,
        )
        return int(r.stdout.strip())
    except Exception:
        return -1


async def one_request(client: httpx.AsyncClient, target: str, i: int) -> tuple[int, float]:
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{target}/mock/wework/inbound", json=make_payload(i), timeout=10)
        dt_ms = (time.perf_counter() - t0) * 1000
        return r.status_code, dt_ms
    except Exception as exc:
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"  req {i}: {exc!r}")
        return 0, dt_ms


async def run(total: int, concurrency: int, target: str) -> None:
    print(f"== Stress test: {total} requests @ concurrency {concurrency} → {target}")

    queue_before = redis_llen()
    print(f"Redis queue:incoming before: {queue_before}")

    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    latencies: list[float] = []
    status_counts: dict[int, int] = {}

    async with httpx.AsyncClient(limits=limits) as client:
        # Prime connection
        probe = await client.get(f"{target}/health", timeout=5)
        print(f"/health probe: {probe.status_code} {probe.json()}")

        sem = asyncio.Semaphore(concurrency)

        async def _task(i: int):
            async with sem:
                status, dt = await one_request(client, target, i)
                latencies.append(dt)
                status_counts[status] = status_counts.get(status, 0) + 1

        t0 = time.perf_counter()
        await asyncio.gather(*(_task(i) for i in range(total)))
        elapsed = time.perf_counter() - t0

    queue_after = redis_llen()
    print()
    print("=== Ingress Results ===")
    print(f"Total:        {total}")
    print(f"Elapsed:      {elapsed:.2f}s")
    print(f"Throughput:   {total/elapsed:.1f} req/s")
    print(f"Statuses:     {dict(sorted(status_counts.items()))}")
    if latencies:
        sorted_lat = sorted(latencies)
        def pct(p): return sorted_lat[min(len(sorted_lat)-1, int(len(sorted_lat)*p))]
        print(f"Latency ms:   min={min(latencies):.1f} p50={pct(0.50):.1f} p90={pct(0.90):.1f} "
              f"p95={pct(0.95):.1f} p99={pct(0.99):.1f} max={max(latencies):.1f} "
              f"mean={statistics.mean(latencies):.1f}")
    print(f"Redis queue:incoming: before={queue_before}  after={queue_after}  delta={queue_after - queue_before}")

    # Observe Worker drain for 30s
    print()
    print("=== Worker drain observation (30s @ 2s sample) ===")
    for t in range(0, 32, 2):
        llen = redis_llen()
        print(f"  t+{t:2d}s  queue:incoming = {llen}")
        if t < 30:
            await asyncio.sleep(2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--total", type=int, default=1000)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--target", default="http://127.0.0.1:8001")
    args = ap.parse_args()
    asyncio.run(run(args.total, args.concurrency, args.target))
