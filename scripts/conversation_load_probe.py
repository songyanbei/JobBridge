"""Scoped real-queue load probe for the text conversation pipeline.

Creates disposable users/events, pushes a burst through Redis, measures durable MySQL
completion, verifies same-user commit order, and removes only probe-owned state.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid

import redis

from conversation_production_smoke import _create_user, _mysql
from demo_acceptance_smoke import QUEUE_INCOMING, build_inbound_payload, create_mysql_inbound_event


SEARCHES = (
    "帮我找苏州电子厂普工，月薪5500以上，最好包吃住",
    "杭州有焊工岗位吗，六千以上",
    "想去无锡做仓管，最好长白班",
    "上海服务员工作，包住优先",
)


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * ratio))], 3)


def _enqueue(r, mysql_dsn: str, userid: str, text: str) -> tuple[str, int]:
    payload = build_inbound_payload(userid, text)
    payload["_enqueued_at"] = time.time()
    event_id = create_mysql_inbound_event(mysql_dsn, payload)
    payload["inbound_event_id"] = event_id
    r.rpush(QUEUE_INCOMING, json.dumps(payload, ensure_ascii=False))
    return payload["msg_id"], event_id


def _wait(conn, events: dict[int, float], timeout: int) -> tuple[dict[int, float], dict[int, str]]:
    finished: dict[int, float] = {}
    statuses: dict[int, str] = {}
    deadline = time.monotonic() + timeout
    ids = tuple(events)
    placeholders = ",".join(["%s"] * len(ids))
    while time.monotonic() < deadline and len(finished) < len(events):
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id,status FROM wecom_inbound_event WHERE id IN ({placeholders})",
                ids,
            )
            for row in cur.fetchall():
                statuses[int(row["id"])] = row["status"]
                if row["status"] in {"done", "failed", "dead_letter"}:
                    finished.setdefault(int(row["id"]), time.monotonic())
        if len(finished) < len(events):
            time.sleep(0.05)
    return finished, statuses


def _cleanup(conn, r, userids: list[str]) -> None:
    for userid in userids:
        # Event done is visible just before the worker releases its user lock.
        # Deleting that lock here can create a false lock-lost signal and would be
        # unsafe if another probe message arrived during cleanup.
        r.delete(f"session:{userid}")
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(userids))
        for table, column in (
            ("wecom_outbound_outbox", "userid"),
            ("conversation_log", "userid"),
            ("wecom_inbound_event", "from_userid"),
            ("resume", "owner_userid"),
            ("job", "owner_userid"),
            ("user", "external_userid"),
        ):
            cur.execute(
                f"DELETE FROM `{table}` WHERE `{column}` IN ({placeholders})",
                tuple(userids),
            )


def _durable_timings(conn, event_ids: list[int]) -> tuple[list[float], list[float]]:
    placeholders = ",".join(["%s"] * len(event_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT
                  TIMESTAMPDIFF(MICROSECOND, created_at, worker_started_at) / 1000000 AS queue_s,
                  TIMESTAMPDIFF(MICROSECOND, worker_started_at, worker_finished_at) / 1000000 AS process_s
                FROM wecom_inbound_event WHERE id IN ({placeholders})""",
            tuple(event_ids),
        )
        rows = cur.fetchall()
    queue = [float(row["queue_s"]) for row in rows if row["queue_s"] is not None]
    process = [float(row["process_s"]) for row in rows if row["process_s"] is not None]
    return queue, process


def run(args) -> dict:
    r = redis.Redis.from_url(args.redis_url, decode_responses=False)
    conn = _mysql(args.mysql_dsn)
    suffix = uuid.uuid4().hex[:10]
    unique_users = [f"load_probe_{suffix}_{i}" for i in range(args.users)]
    ordered_user = f"order_probe_{suffix}"
    all_users = unique_users + [ordered_user]
    try:
        for userid in all_users:
            _create_user(conn, userid, "worker")

        events: dict[int, float] = {}
        msg_ids: dict[int, str] = {}
        burst_started = time.monotonic()
        for index, userid in enumerate(unique_users):
            msg_id, event_id = _enqueue(
                r, args.mysql_dsn, userid, SEARCHES[index % len(SEARCHES)],
            )
            events[event_id] = time.monotonic()
            msg_ids[event_id] = msg_id
        finished, statuses = _wait(conn, events, args.timeout_seconds)
        elapsed = time.monotonic() - burst_started
        latencies = [finished[eid] - started for eid, started in events.items() if eid in finished]
        queue_times, process_times = _durable_timings(conn, list(events))

        # A dependent three-turn burst on one user must commit in receive/event-id order.
        ordered: list[tuple[int, str]] = []
        for text in (SEARCHES[0], "工资改成6000以上", "更多"):
            msg_id, event_id = _enqueue(r, args.mysql_dsn, ordered_user, text)
            ordered.append((event_id, msg_id))
        ordered_events = {event_id: time.monotonic() for event_id, _ in ordered}
        ordered_finished, ordered_status = _wait(conn, ordered_events, args.timeout_seconds)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT wecom_msg_id FROM conversation_log
                   WHERE userid=%s AND direction='in' ORDER BY id""",
                (ordered_user,),
            )
            committed = [row["wecom_msg_id"] for row in cur.fetchall()]
        expected = [msg_id for _, msg_id in ordered]

        return {
            "unique_user_burst": {
                "requested": len(events),
                "done": sum(statuses.get(eid) == "done" for eid in events),
                "failed": sum(statuses.get(eid) in {"failed", "dead_letter"} for eid in events),
                "throughput_per_second": round(len(finished) / elapsed, 3) if elapsed else None,
                "latency_seconds": {
                    "mean": round(statistics.mean(latencies), 3) if latencies else None,
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "max": round(max(latencies), 3) if latencies else None,
                },
                "durable_queue_seconds": {
                    "p50": _percentile(queue_times, 0.50),
                    "p95": _percentile(queue_times, 0.95),
                    "max": round(max(queue_times), 3) if queue_times else None,
                },
                "durable_process_seconds": {
                    "p50": _percentile(process_times, 0.50),
                    "p95": _percentile(process_times, 0.95),
                    "max": round(max(process_times), 3) if process_times else None,
                },
                "final_statuses": {str(k): statuses.get(k, "timeout") for k in events},
            },
            "same_user_order": {
                "all_done": all(ordered_status.get(eid) == "done" for eid, _ in ordered),
                "expected": expected,
                "committed": committed,
                "in_order": committed == expected,
                "completion_count": len(ordered_finished),
            },
        }
    finally:
        _cleanup(conn, r, all_users)
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--mysql-dsn", required=True)
    parser.add_argument("--users", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
