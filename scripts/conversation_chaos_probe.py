"""Scoped infrastructure-chaos probe for the durable text conversation pipeline.

The companion shell runner pauses Redis or MySQL after this probe has persisted and
enqueued a burst. The probe then verifies eventual recovery, no router replay, sent
outbox rows, and per-user session consistency before deleting only probe-owned data.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid

import redis

from conversation_production_smoke import _create_user, _mysql
from demo_acceptance_smoke import (
    QUEUE_INCOMING,
    build_inbound_payload,
    create_mysql_inbound_event,
)


def _remove_owned_queue_payloads(r, userids: set[str]) -> None:
    for raw in r.lrange(QUEUE_INCOMING, 0, -1):
        try:
            payload = json.loads(
                raw.decode("utf-8") if isinstance(raw, bytes) else raw,
            )
        except Exception:
            continue
        if payload.get("from_userid") in userids:
            r.lrem(QUEUE_INCOMING, 0, raw)


def _cleanup(conn, r, userids: list[str]) -> None:
    owned = set(userids)
    _remove_owned_queue_payloads(r, owned)
    for userid in userids:
        # Do not delete lock:{userid}; event done can become visible immediately
        # before the worker unwinds and releases its lease.
        r.delete(f"session:{userid}")
    placeholders = ",".join(["%s"] * len(userids))
    with conn.cursor() as cur:
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


def run(args) -> dict:
    r = redis.Redis.from_url(args.redis_url, decode_responses=False)
    conn = _mysql(args.mysql_dsn)
    suffix = uuid.uuid4().hex[:10]
    userids = [
        f"chaos_{args.mode}_{suffix}_{index:02d}"
        for index in range(args.users)
    ]
    events: dict[int, str] = {}
    started = time.monotonic()
    marker = f"chaos:ready:{args.run_id}"
    try:
        for userid in userids:
            _create_user(conn, userid, "worker")
            message = "帮我找杭州焊工岗位，月薪六千以上"
            if args.mode == "llm_mixed":
                # The deterministic fake provider keys each injected failure to
                # the user marker, so retries see the same fault.
                message = f"帮我找深圳普工岗位，测试标记 {userid}"
            payload = build_inbound_payload(
                userid,
                message,
            )
            payload["_enqueued_at"] = time.time()
            event_id = create_mysql_inbound_event(args.mysql_dsn, payload)
            payload["inbound_event_id"] = event_id
            events[event_id] = payload["msg_id"]
            r.rpush(QUEUE_INCOMING, json.dumps(payload, ensure_ascii=False))

        # The host runner waits for this marker before pausing infrastructure.
        r.setex(marker, 60, b"1")

        statuses: dict[int, str] = {}
        deadline = time.monotonic() + args.timeout_seconds
        ids = tuple(events)
        placeholders = ",".join(["%s"] * len(ids))
        while time.monotonic() < deadline:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""SELECT id, status FROM wecom_inbound_event
                             WHERE id IN ({placeholders})""",
                        ids,
                    )
                    statuses = {
                        int(row["id"]): str(row["status"])
                        for row in cur.fetchall()
                    }
            except Exception:
                # The expected MySQL pause can invalidate this connection.
                try:
                    conn.close()
                except Exception:
                    pass
                time.sleep(0.5)
                conn = _mysql(args.mysql_dsn)
                continue
            if len(statuses) == len(events) and all(
                status in {"done", "failed", "dead_letter"}
                for status in statuses.values()
            ):
                break
            time.sleep(0.1)

        # Event completion and the outbox sender are intentionally decoupled.
        # Wait for the externally visible delivery state instead of racing the
        # sender immediately after the final event changes to done.
        while time.monotonic() < deadline:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT COUNT(*) AS n
                          FROM wecom_outbound_outbox o
                          JOIN wecom_inbound_event e ON e.id=o.inbound_event_id
                         WHERE e.id IN ({placeholders}) AND o.status='sent'""",
                    ids,
                )
                if int((cur.fetchone() or {}).get("n") or 0) == len(events):
                    break
            time.sleep(0.1)

        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT wecom_msg_id, COUNT(*) AS n
                      FROM conversation_log
                     WHERE wecom_msg_id IN ({placeholders})
                     GROUP BY wecom_msg_id""",
                tuple(events.values()),
            )
            log_counts = {
                str(row["wecom_msg_id"]): int(row["n"])
                for row in cur.fetchall()
            }
            cur.execute(
                f"""SELECT o.status, COUNT(*) AS n
                      FROM wecom_outbound_outbox o
                      JOIN wecom_inbound_event e ON e.id=o.inbound_event_id
                     WHERE e.id IN ({placeholders})
                     GROUP BY o.status""",
                ids,
            )
            outbox_counts = {
                str(row["status"]): int(row["n"])
                for row in cur.fetchall()
            }
            cur.execute(
                f"""SELECT COUNT(*) AS n
                      FROM wecom_inbound_event
                     WHERE id IN ({placeholders})
                       AND session_payload IS NOT NULL""",
                ids,
            )
            retained_payloads = int((cur.fetchone() or {}).get("n") or 0)

        sessions = []
        for userid in userids:
            raw = r.get(f"session:{userid}")
            if raw:
                sessions.append(json.loads(
                    raw.decode("utf-8") if isinstance(raw, bytes) else raw,
                ))

        done = sum(status == "done" for status in statuses.values())
        duplicate_logs = {
            msg_id: count for msg_id, count in log_counts.items() if count != 1
        }
        session_versions = [
            int(session.get("session_version") or 0) for session in sessions
        ]
        result = {
            "mode": args.mode,
            "requested": len(events),
            "done": done,
            "statuses": {
                status: list(statuses.values()).count(status)
                for status in sorted(set(statuses.values()))
            },
            "outbox": outbox_counts,
            "duplicate_or_missing_input_logs": duplicate_logs,
            "session_count": len(sessions),
            "session_version_min": min(session_versions) if session_versions else 0,
            "retained_session_payloads": retained_payloads,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        assert done == len(events), result
        assert not duplicate_logs, result
        assert outbox_counts.get("sent") == len(events), result
        assert sum(outbox_counts.values()) == len(events), result
        assert len(sessions) == len(events), result
        assert min(session_versions) >= 1, result
        assert retained_payloads == 0, result
        return result
    finally:
        try:
            r.delete(marker)
            _cleanup(conn, r, userids)
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", choices=("redis", "mysql", "llm_mixed"), required=True,
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--mysql-dsn", required=True)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
