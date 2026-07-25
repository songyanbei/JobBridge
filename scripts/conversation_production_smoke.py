"""Destructive-scoped full-service smoke for production dialogue flows.

Creates three uniquely named test users, drives the real Redis queue + worker + MySQL
pipeline, uses mock WeCom outbound, then deletes only rows/keys created by this run.
Media upload flows are intentionally out of scope.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from urllib.parse import parse_qs, unquote, urlparse

import pymysql
import redis

from demo_acceptance_smoke import (
    QUEUE_INCOMING,
    build_inbound_payload,
    create_mysql_inbound_event,
    health_check,
    wait_for_subscribe,
    wait_for_worker_done,
)


def _mysql(dsn: str):
    parsed = urlparse(dsn)
    query = parse_qs(parsed.query)
    return pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=unquote(parsed.path.lstrip("/")),
        charset=query.get("charset", ["utf8mb4"])[0],
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _create_user(conn, userid: str, role: str) -> None:
    can_jobs = role in {"worker", "broker"}
    can_workers = role in {"factory", "broker"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO `user` (
              external_userid, role, display_name, can_search_jobs,
              can_search_workers, status, last_active_at
            ) VALUES (%s, %s, %s, %s, %s, 'active', NOW())
            """,
            (userid, role, f"production-smoke-{role}", can_jobs, can_workers),
        )


def _reply_text(payloads: list[dict]) -> str:
    return "\n".join(
        str(item.get("text", {}).get("content", "")) for item in payloads
    )


def _latest_intent(conn, userid: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT intent FROM conversation_log
               WHERE userid=%s AND direction='out'
               ORDER BY id DESC LIMIT 1""",
            (userid,),
        )
        row = cur.fetchone() or {}
    return row.get("intent")


def _job_count(conn, userid: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM job WHERE owner_userid=%s", (userid,))
        return int((cur.fetchone() or {}).get("n") or 0)


class Harness:
    def __init__(self, redis_url: str, mysql_dsn: str, timeout: int):
        self.r = redis.Redis.from_url(redis_url, decode_responses=False)
        self.conn = _mysql(mysql_dsn)
        self.mysql_dsn = mysql_dsn
        self.timeout = timeout
        self.msg_ids: list[str] = []

    def session(self, userid: str) -> dict:
        raw = self.r.get(f"session:{userid}")
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def send(self, userid: str, content: str) -> tuple[str, str | None, dict]:
        payload = build_inbound_payload(userid, content)
        payload["_enqueued_at"] = time.time()
        payload["inbound_event_id"] = create_mysql_inbound_event(
            self.mysql_dsn, payload,
        )
        self.msg_ids.append(payload["msg_id"])
        pubsub = self.r.pubsub()
        pubsub.subscribe(f"mock:outbound:{userid}")
        wait_for_subscribe(pubsub, self.timeout)
        try:
            self.r.rpush(QUEUE_INCOMING, json.dumps(payload, ensure_ascii=False))
            outbound = wait_for_worker_done(
                pubsub, self.mysql_dsn, payload["msg_id"], self.timeout,
            )
        finally:
            pubsub.close()
        return _reply_text(outbound), _latest_intent(self.conn, userid), self.session(userid)

    def recover_without_enqueue(
        self, userid: str, content: str,
    ) -> tuple[str, int, int]:
        """Persist a stale received event and prove periodic recovery handles it once."""
        payload = build_inbound_payload(userid, content)
        payload["_enqueued_at"] = time.time()
        payload["inbound_event_id"] = create_mysql_inbound_event(
            self.mysql_dsn, payload,
        )
        self.msg_ids.append(payload["msg_id"])
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE wecom_inbound_event
                      SET created_at=DATE_SUB(NOW(6), INTERVAL 20 SECOND)
                    WHERE id=%s""",
                (payload["inbound_event_id"],),
            )
        pubsub = self.r.pubsub()
        pubsub.subscribe(f"mock:outbound:{userid}")
        wait_for_subscribe(pubsub, self.timeout)
        try:
            outbound = wait_for_worker_done(
                pubsub, self.mysql_dsn, payload["msg_id"], self.timeout,
            )
        finally:
            pubsub.close()
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS n FROM conversation_log
                    WHERE wecom_msg_id=%s""",
                (payload["msg_id"],),
            )
            inbound_count = int((cur.fetchone() or {}).get("n") or 0)
            cur.execute(
                """SELECT COUNT(*) AS n FROM conversation_log
                    WHERE userid=%s AND direction='out'""",
                (userid,),
            )
            outbound_count = int((cur.fetchone() or {}).get("n") or 0)
        return _reply_text(outbound), inbound_count, outbound_count

    def recover_stale_outbox(self, userid: str) -> tuple[str, int, str]:
        """Prove a crash-claimed reply is delivered without rerunning its event."""
        payload = build_inbound_payload(userid, "outbox recovery probe")
        event_id = create_mysql_inbound_event(self.mysql_dsn, payload)
        self.msg_ids.append(payload["msg_id"])
        marker = f"outbox-recovered-{uuid.uuid4().hex[:8]}"
        pubsub = self.r.pubsub()
        pubsub.subscribe(f"mock:outbound:{userid}")
        wait_for_subscribe(pubsub, self.timeout)
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """UPDATE wecom_inbound_event
                          SET status='done', worker_finished_at=NOW(6)
                        WHERE id=%s""",
                    (event_id,),
                )
                cur.execute(
                    """INSERT INTO wecom_outbound_outbox (
                         inbound_event_id, reply_index, userid, msg_type, content,
                         status, attempt_count, locked_at
                       ) VALUES (%s, 0, %s, 'text', %s, 'sending', 1,
                                 DATE_SUB(NOW(6), INTERVAL 4 MINUTE))""",
                    (event_id, userid, marker),
                )

            observed: list[dict] = []
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                item = pubsub.get_message(timeout=1)
                if item and item.get("type") == "message":
                    raw = item["data"]
                    observed.append(json.loads(
                        raw.decode() if isinstance(raw, bytes) else raw
                    ))
                    break
            # A second delivery in the immediate recovery window is a regression.
            grace = time.time() + 2
            while time.time() < grace:
                item = pubsub.get_message(timeout=0.2)
                if item and item.get("type") == "message":
                    raw = item["data"]
                    observed.append(json.loads(
                        raw.decode() if isinstance(raw, bytes) else raw
                    ))
        finally:
            pubsub.close()

        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT status, attempt_count FROM wecom_outbound_outbox
                    WHERE inbound_event_id=%s AND reply_index=0""",
                (event_id,),
            )
            row = cur.fetchone() or {}
        return _reply_text(observed), int(row.get("attempt_count") or 0), str(
            row.get("status") or ""
        )

    def recover_pending_session_commit(self, userid: str) -> dict:
        """Recover a DB-committed turn whose Redis session write never happened."""
        payload = build_inbound_payload(userid, "session commit recovery probe")
        event_id = create_mysql_inbound_event(self.mysql_dsn, payload)
        self.msg_ids.append(payload["msg_id"])
        marker = f"session-recovered-{uuid.uuid4().hex[:8]}"
        expected_session = {
            "role": "worker",
            "session_version": 1,
            "active_flow": "idle",
            "search_criteria": {"city": ["常州市"]},
        }
        self.r.delete(f"session:{userid}")
        pubsub = self.r.pubsub()
        pubsub.subscribe(f"mock:outbound:{userid}")
        wait_for_subscribe(pubsub, self.timeout)
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """UPDATE wecom_inbound_event
                          SET status='session_pending',
                              session_operation='save',
                              session_expected_version=0,
                              session_payload=%s,
                              session_apply_attempts=0,
                              session_apply_locked_at=NULL,
                              session_next_attempt_at=NULL
                        WHERE id=%s""",
                    # ASCII escapes avoid making this fault injector dependent on
                    # the local mysql client/terminal character set.
                    (json.dumps(expected_session, ensure_ascii=True), event_id),
                )
                cur.execute(
                    """INSERT INTO wecom_outbound_outbox (
                         inbound_event_id, reply_index, userid, msg_type, content,
                         status, attempt_count
                       ) VALUES (%s, 0, %s, 'text', %s, 'pending', 0)""",
                    (event_id, userid, marker),
                )

            observed: list[dict] = []
            row: dict = {}
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                item = pubsub.get_message(timeout=0.5)
                if item and item.get("type") == "message":
                    raw = item["data"]
                    observed.append(json.loads(
                        raw.decode() if isinstance(raw, bytes) else raw
                    ))
                with self.conn.cursor() as cur:
                    cur.execute(
                        """SELECT status, session_payload, session_applied_at
                             FROM wecom_inbound_event WHERE id=%s""",
                        (event_id,),
                    )
                    row = cur.fetchone() or {}
                    cur.execute(
                        """SELECT status FROM wecom_outbound_outbox
                            WHERE inbound_event_id=%s AND reply_index=0""",
                        (event_id,),
                    )
                    outbox = cur.fetchone() or {}
                if row.get("status") == "done" and outbox.get("status") == "sent":
                    break
        finally:
            pubsub.close()

        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS n FROM conversation_log
                    WHERE wecom_msg_id=%s""",
                (payload["msg_id"],),
            )
            replayed = int((cur.fetchone() or {}).get("n") or 0)
        return {
            "event_status": row.get("status"),
            "payload_cleared": row.get("session_payload") is None,
            "session_applied": row.get("session_applied_at") is not None,
            "outbox_status": outbox.get("status"),
            "reply": _reply_text(observed),
            "session": self.session(userid),
            "business_replayed": replayed,
            "marker": marker,
        }

    def assert_outbox_healthy(self, userids: list[str]) -> int:
        placeholders = ",".join(["%s"] * len(userids))
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT COUNT(*) AS total,
                           SUM(status <> 'sent') AS unsent
                      FROM wecom_outbound_outbox
                     WHERE userid IN ({placeholders})""",
                tuple(userids),
            )
            row = cur.fetchone() or {}
        assert int(row.get("unsent") or 0) == 0, row
        return int(row.get("total") or 0)

    def cleanup(self, userids: list[str]) -> None:
        for userid in userids:
            # Never delete a live distributed lock from a test cleanup path.
            # The worker may have committed done but still be unwinding its lease.
            self.r.delete(f"session:{userid}")
        # Remove any unconsumed payload belonging to this run before deleting DB boundaries.
        for raw in self.r.lrange(QUEUE_INCOMING, 0, -1):
            try:
                payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            except Exception:
                continue
            if payload.get("from_userid") in userids:
                self.r.lrem(QUEUE_INCOMING, 0, raw)
        with self.conn.cursor() as cur:
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
        self.conn.close()


def run(args) -> dict:
    health_check(args.base_url, args.timeout_seconds)
    suffix = uuid.uuid4().hex[:10]
    users = {
        role: f"production_smoke_{role}_{suffix}"
        for role in ("worker", "factory", "broker")
    }
    h = Harness(args.redis_url, args.mysql_dsn, args.timeout_seconds)
    checks: list[str] = []
    try:
        for role, userid in users.items():
            _create_user(h.conn, userid, role)

        # Durable DB recovery must work with many workers without duplicate
        # enqueue/processing. This event is intentionally never pushed to Redis.
        text, inbound_count, outbound_count = h.recover_without_enqueue(
            users["worker"], "你好",
        )
        assert text
        assert inbound_count == 1
        assert outbound_count == 1
        checks.append("durable_recovery_exactly_once")

        text, attempts, status = h.recover_stale_outbox(users["worker"])
        assert text.startswith("outbox-recovered-"), text
        assert attempts == 2
        assert status == "sent"
        checks.append("outbox_stale_claim_recovery")

        recovered = h.recover_pending_session_commit(users["worker"])
        assert recovered["event_status"] == "done", recovered
        assert recovered["session_applied"], recovered
        assert recovered["payload_cleared"], recovered
        assert recovered["outbox_status"] == "sent", recovered
        assert recovered["reply"] == recovered["marker"], recovered
        assert recovered["session"].get("search_criteria") == {
            "city": ["常州市"],
        }, recovered
        assert recovered["business_replayed"] == 0, recovered
        checks.append("durable_session_commit_recovery")

        # Worker search → follow-up → pagination.
        text, intent, session = h.send(
            users["worker"], "帮我找苏州电子厂岗位，5500以上，最好包吃住",
        )
        assert intent == "search_job", (intent, text)
        assert session.get("search_criteria", {}).get("city") == ["苏州市"]
        assert "系统繁忙" not in text
        checks.append("worker_search")

        text, intent, session = h.send(users["worker"], "工资改成6000以上")
        assert intent == "follow_up", (intent, text)
        assert session.get("search_criteria", {}).get("salary_floor_monthly") == 6000
        checks.append("worker_follow_up")

        text, intent, session = h.send(users["worker"], "更多")
        assert intent == "show_more", (intent, text)
        checks.append("worker_show_more")

        before = dict(session.get("search_criteria") or {})
        text, _intent, session = h.send(
            users["worker"], "先找北京普工，不行再看无锡，顺便把我的简历发了",
        )
        assert "多个先后操作" in text
        assert session.get("search_criteria") == before
        checks.append("complex_action_safe_clarification")

        text, _intent, session = h.send(
            users["worker"], "先找苏州普工，然后找杭州焊工岗位",
        )
        assert session.get("pending_action", {}).get("raw_text") == "找杭州焊工岗位"
        assert "已记住下一步" in text
        text, _intent, session = h.send(users["worker"], "/下一步")
        assert "找杭州焊工岗位" in text
        text, intent, session = h.send(users["worker"], "找杭州焊工岗位")
        assert intent in {"search_job", "follow_up"}, (intent, text)
        assert session.get("search_criteria", {}).get("city") == ["杭州市"], (
            intent, text, session,
        )
        assert not session.get("pending_action")
        checks.append("bounded_two_action_plan")

        # Factory complete publication.
        jobs_before = _job_count(h.conn, users["factory"])
        text, _intent, session = h.send(
            users["factory"],
            "帮我发布岗位：苏州电子厂普工，月薪5500，计薪方式月薪，招5人，长期工",
        )
        assert _job_count(h.conn, users["factory"]) == jobs_before + 1, text
        assert not session.get("pending_upload_intent")
        checks.append("factory_publish_job")

        # Draft → search conflict → resume original → explicit cancel; draft must survive
        # every unrecognized/choice transition until explicit cancel.
        _text, _intent, session = h.send(users["factory"], "帮我发布岗位：杭州焊工")
        assert session.get("pending_upload_intent") == "upload_job"
        original_draft = dict(session.get("pending_upload") or {})
        _text, _intent, session = h.send(users["factory"], "先找两个焊工")
        assert session.get("active_flow") == "upload_conflict"
        assert session.get("pending_upload") == original_draft
        _text, _intent, session = h.send(users["factory"], "继续发布")
        assert session.get("active_flow") == "upload_collecting"
        assert session.get("pending_upload") == original_draft
        _text, _intent, session = h.send(users["factory"], "/取消")
        assert not session.get("pending_upload_intent")
        checks.append("factory_draft_conflict_resume_cancel")

        # Broker explicit object switches direction in both directions.
        text, intent, session = h.send(
            users["broker"], "帮工厂找一个苏州焊工师傅",
        )
        assert intent == "search_worker", (intent, text)
        assert session.get("broker_direction") == "search_worker"
        text, intent, session = h.send(
            users["broker"], "给这位师傅找个杭州焊工岗位",
        )
        assert intent in {"search_job", "follow_up"}, (intent, text)
        assert session.get("broker_direction") == "search_job"
        checks.append("broker_direction_switch")

        # Unknown category must remain safe and return a business response, not corrupt state.
        text, _intent, session = h.send(users["worker"], "想找火星矿工，月薪三万")
        assert text and "系统繁忙" not in text
        assert int(session.get("session_version") or 0) > 0
        checks.append("unknown_expression_safe")

        assert h.assert_outbox_healthy(list(users.values())) >= len(checks)
        checks.append("transactional_outbox_sent")

        return {"ok": True, "checks": checks, "check_count": len(checks)}
    finally:
        h.cleanup(list(users.values()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--mysql-dsn", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False))


if __name__ == "__main__":
    main()
