"""Closed-loop sustained load acceptance for the durable conversation pipeline.

Each disposable user has at most one in-flight event. A completed event is
replaced until the active duration expires, then the probe drains the pipeline
and verifies delivery, per-user order, latency, and queue-growth indicators.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from collections import defaultdict

import redis

from conversation_load_probe import (
    SEARCHES,
    _cleanup,
    _enqueue,
    _percentile,
)
from conversation_production_smoke import _create_user, _mysql


TERMINAL = {"done", "failed", "dead_letter"}


def _timings(conn, event_ids: list[int]) -> dict[int, tuple[float | None, float | None]]:
    timings: dict[int, tuple[float | None, float | None]] = {}
    for start in range(0, len(event_ids), 1000):
        chunk = tuple(event_ids[start:start + 1000])
        placeholders = ",".join(["%s"] * len(chunk))
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id,
                      TIMESTAMPDIFF(
                        MICROSECOND, created_at, worker_started_at
                      ) / 1000000 AS queue_s,
                      TIMESTAMPDIFF(
                        MICROSECOND, worker_started_at, worker_finished_at
                      ) / 1000000 AS process_s
                    FROM wecom_inbound_event
                   WHERE id IN ({placeholders})""",
                chunk,
            )
            for row in cur.fetchall():
                timings[int(row["id"])] = (
                    float(row["queue_s"]) if row["queue_s"] is not None else None,
                    float(row["process_s"]) if row["process_s"] is not None else None,
                )
    return timings


def _wait_for_outbox(conn, userids: list[str], expected: int, deadline: float) -> int:
    placeholders = ",".join(["%s"] * len(userids))
    sent = 0
    while time.monotonic() < deadline:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT COUNT(*) AS n FROM wecom_outbound_outbox
                     WHERE userid IN ({placeholders}) AND status='sent'""",
                tuple(userids),
            )
            sent = int((cur.fetchone() or {}).get("n") or 0)
        if sent == expected:
            break
        time.sleep(0.1)
    return sent


def _cleanup_safely(conn, r, userids: list[str]) -> None:
    owned = set(userids)
    for raw in r.lrange("queue:wecom:incoming", 0, -1):
        try:
            payload = json.loads(
                raw.decode("utf-8") if isinstance(raw, bytes) else raw,
            )
        except Exception:
            continue
        if payload.get("from_userid") in owned:
            r.lrem("queue:wecom:incoming", 0, raw)
    placeholders = ",".join(["%s"] * len(userids))
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT DISTINCT from_userid FROM wecom_inbound_event
                 WHERE from_userid IN ({placeholders})
                   AND status NOT IN ('done','failed','dead_letter')""",
            tuple(userids),
        )
        active = {str(row["from_userid"]) for row in cur.fetchall()}
    safe = [userid for userid in userids if userid not in active]
    if safe:
        _cleanup(conn, r, safe)


def run(args) -> dict:
    if args.concurrency < 1 or args.duration_seconds < 1:
        raise ValueError("concurrency and duration-seconds must be positive")
    r = redis.Redis.from_url(args.redis_url, decode_responses=False)
    conn = _mysql(args.mysql_dsn)
    suffix = uuid.uuid4().hex[:10]
    userids = [
        f"sustained_probe_{suffix}_{index:03d}"
        for index in range(args.concurrency)
    ]
    expected_by_user: dict[str, list[str]] = defaultdict(list)
    inflight: dict[int, tuple[str, float]] = {}
    all_event_ids: list[int] = []
    enqueued_offsets: dict[int, float] = {}
    completion_times: list[float] = []
    latencies: list[float] = []
    statuses: dict[int, str] = {}
    started = time.monotonic()
    active_deadline = started + args.duration_seconds
    drain_deadline = active_deadline + args.drain_timeout_seconds

    def enqueue_next(userid: str) -> None:
        turn = len(expected_by_user[userid])
        msg_id, event_id = _enqueue(
            r,
            args.mysql_dsn,
            userid,
            SEARCHES[turn % len(SEARCHES)],
        )
        expected_by_user[userid].append(msg_id)
        inflight[event_id] = (userid, time.monotonic())
        all_event_ids.append(event_id)
        enqueued_offsets[event_id] = time.monotonic() - started

    try:
        for userid in userids:
            _create_user(conn, userid, "worker")
            enqueue_next(userid)

        while inflight and time.monotonic() < drain_deadline:
            ids = tuple(inflight)
            placeholders = ",".join(["%s"] * len(ids))
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT id,status FROM wecom_inbound_event
                         WHERE id IN ({placeholders})""",
                    ids,
                )
                rows = cur.fetchall()
            now = time.monotonic()
            for row in rows:
                event_id = int(row["id"])
                status = str(row["status"])
                if status not in TERMINAL or event_id not in inflight:
                    continue
                userid, enqueued_at = inflight.pop(event_id)
                statuses[event_id] = status
                completion_times.append(now - started)
                latencies.append(now - enqueued_at)
                if now < active_deadline:
                    enqueue_next(userid)
            if inflight:
                time.sleep(args.poll_interval_seconds)

        # Include any undrained events as explicit timeouts in the report.
        for event_id in inflight:
            statuses[event_id] = "timeout"

        sent = _wait_for_outbox(
            conn,
            userids,
            sum(status == "done" for status in statuses.values()),
            time.monotonic() + args.drain_timeout_seconds,
        )
        timings = _timings(conn, all_event_ids)
        queue_times = [
            item[0] for item in timings.values() if item[0] is not None
        ]
        process_times = [
            item[1] for item in timings.values() if item[1] is not None
        ]

        midpoint = args.duration_seconds / 2
        first_queue = [
            timings[event_id][0]
            for event_id, offset in enqueued_offsets.items()
            if offset <= midpoint
            and event_id in timings
            and timings[event_id][0] is not None
        ]
        second_queue = [
            timings[event_id][0]
            for event_id, offset in enqueued_offsets.items()
            if midpoint < offset <= args.duration_seconds
            and event_id in timings
            and timings[event_id][0] is not None
        ]
        order_errors = []
        # Query one user at a time so a four-hour run does not materialize the
        # entire conversation log result set in memory.
        with conn.cursor() as cur:
            for userid in userids:
                cur.execute(
                    """SELECT wecom_msg_id FROM conversation_log
                         WHERE userid=%s AND direction='in' ORDER BY id""",
                    (userid,),
                )
                committed = [
                    str(row["wecom_msg_id"]) for row in cur.fetchall()
                ]
                if committed != expected_by_user[userid]:
                    order_errors.append(userid)

        first_count = sum(value <= midpoint for value in completion_times)
        second_count = sum(
            midpoint < value <= args.duration_seconds
            for value in completion_times
        )
        done = sum(status == "done" for status in statuses.values())
        failed = sum(status in {"failed", "dead_letter"} for status in statuses.values())
        timed_out = sum(status == "timeout" for status in statuses.values())
        result = {
            "concurrency": args.concurrency,
            "active_duration_seconds": args.duration_seconds,
            "requested": len(all_event_ids),
            "done": done,
            "failed": failed,
            "timeout": timed_out,
            "outbox_sent": sent,
            "throughput_per_second": round(
                sum(value <= args.duration_seconds for value in completion_times)
                / args.duration_seconds,
                3,
            ),
            "completion_count_by_half": {
                "first": first_count,
                "second": second_count,
                "second_minus_first": second_count - first_count,
            },
            "queue_p95_by_half": {
                "first": _percentile(first_queue, 0.95),
                "second": _percentile(second_queue, 0.95),
                "second_minus_first": (
                    round(
                        _percentile(second_queue, 0.95)
                        - _percentile(first_queue, 0.95),
                        3,
                    )
                    if first_queue and second_queue
                    else None
                ),
            },
            "latency_seconds": {
                "mean": round(statistics.mean(latencies), 3) if latencies else None,
                "p50": _percentile(latencies, 0.50),
                "p95": _percentile(latencies, 0.95),
                "p99": _percentile(latencies, 0.99),
                "max": round(max(latencies), 3) if latencies else None,
            },
            "durable_queue_seconds": {
                "p50": _percentile(queue_times, 0.50),
                "p95": _percentile(queue_times, 0.95),
                "p99": _percentile(queue_times, 0.99),
                "max": round(max(queue_times), 3) if queue_times else None,
            },
            "durable_process_seconds": {
                "p50": _percentile(process_times, 0.50),
                "p95": _percentile(process_times, 0.95),
                "p99": _percentile(process_times, 0.99),
                "max": round(max(process_times), 3) if process_times else None,
            },
            "same_user_order_errors": order_errors,
        }
        assert done == len(all_event_ids), result
        assert failed == 0 and timed_out == 0, result
        assert sent == done, result
        assert not order_errors, result
        return result
    finally:
        _cleanup_safely(conn, r, userids)
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--mysql-dsn", required=True)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--duration-seconds", type=int, default=30)
    parser.add_argument("--drain-timeout-seconds", type=int, default=240)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.05)
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
