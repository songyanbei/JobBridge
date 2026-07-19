"""Phase 5 full-service async message smoke test.

This is not an HTTP end-to-end test: the real webhook returns an enqueue ACK,
not the business reply. The smoke test injects a mock WeCom text payload into
Redis, lets the worker consume it, and reads the mocked WeCom outbound reply
from Redis Pub/Sub.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen


QUEUE_INCOMING = "queue:incoming"
OUTBOUND_QUIET_SECONDS = 0.2
DEFAULT_SEARCH_MESSAGE = "帮我找苏州电子厂岗位，5500以上，最好包吃住"


def build_inbound_payload(userid: str, content: str) -> dict:
    return {
        "msg_id": f"phase5_smoke_{uuid.uuid4().hex}",
        "from_userid": userid,
        "msg_type": "text",
        "content": content,
        "media_id": None,
        "create_time": int(time.time()),
        "inbound_event_id": None,
    }


def health_check(base_url: str, timeout_seconds: int) -> None:
    url = base_url.rstrip("/") + "/health"
    with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310 - local smoke URL
        if response.status >= 400:
            raise RuntimeError(f"health check failed: HTTP {response.status}")


def _connect_mysql(mysql_dsn: str):
    if not mysql_dsn:
        raise RuntimeError(
            "--mysql-dsn is required: the acceptance smoke must verify worker completion"
        )
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("--mysql-dsn requires pymysql in the smoke runtime") from exc

    parsed = urlparse(mysql_dsn)
    query = parse_qs(parsed.query)
    database = unquote(parsed.path.lstrip("/"))
    return pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
        charset=query.get("charset", ["utf8mb4"])[0],
        connect_timeout=5,
        read_timeout=5,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def wait_for_subscribe(pubsub, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        message = pubsub.get_message(timeout=0.2)
        if message and message.get("type") == "subscribe":
            return
    raise TimeoutError("Pub/Sub subscribe ack not received before timeout")


def _decode_outbound_message(message: dict) -> dict | None:
    if not message or message.get("type") != "message":
        return None
    data = message.get("data")
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)


def wait_for_worker_done(
    pubsub,
    mysql_dsn: str,
    msg_id: str,
    timeout_seconds: int,
) -> list[dict]:
    """Collect this message's replies until worker DB completion is durable."""
    conn = _connect_mysql(mysql_dsn)
    deadline = time.time() + timeout_seconds
    payloads: list[dict] = []
    try:
        with conn.cursor() as cursor:
            while time.time() < deadline:
                message = pubsub.get_message(timeout=0.1)
                payload = _decode_outbound_message(message)
                if payload is not None:
                    payloads.append(payload)

                cursor.execute(
                    "SELECT status FROM wecom_inbound_event WHERE msg_id = %s",
                    (msg_id,),
                )
                row = cursor.fetchone() or {}
                status = row.get("status")
                if status in {"failed", "dead_letter"}:
                    raise RuntimeError(
                        f"worker failed for msg_id={msg_id}: status={status}"
                    )
                if status != "done":
                    continue

                # send_text publishes before conversation_log and status=done.
                # Drain the already ordered Pub/Sub tail before the next message.
                quiet_deadline = min(
                    deadline,
                    time.time() + OUTBOUND_QUIET_SECONDS,
                )
                while time.time() < quiet_deadline:
                    tail = pubsub.get_message(timeout=0.05)
                    tail_payload = _decode_outbound_message(tail)
                    if tail_payload is not None:
                        payloads.append(tail_payload)
                        quiet_deadline = min(
                            deadline,
                            time.time() + OUTBOUND_QUIET_SECONDS,
                        )
                if not payloads:
                    raise RuntimeError(
                        "worker reached status=done without an outbound business "
                        f"reply for msg_id={msg_id}"
                    )
                return payloads
    finally:
        conn.close()
    raise TimeoutError(
        f"worker did not reach status=done for msg_id={msg_id} before timeout"
    )


def create_mysql_inbound_event(mysql_dsn: str, payload: dict) -> int:
    """Create the durable inbound boundary used by the real webhook path."""
    conn = _connect_mysql(mysql_dsn)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO wecom_inbound_event (
                  msg_id, from_userid, msg_type, media_id, content_brief, status
                ) VALUES (%s, %s, %s, %s, %s, 'received')
                """,
                (
                    payload["msg_id"],
                    payload["from_userid"],
                    payload["msg_type"],
                    payload.get("media_id"),
                    (payload.get("content") or "")[:500],
                ),
            )
            event_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return event_id


def check_mysql_seed(
    mysql_dsn: str,
    min_passed_jobs: int,
    *,
    city_like: str,
    job_category_like: str,
    seed_version: str = "demo_supp_v1",
    min_supplement_jobs: int = 7,
    min_step5_jobs: int = 2,
    min_step6_jobs: int = 2,
    min_step8_jobs: int = 3,
) -> dict:
    if not mysql_dsn:
        raise RuntimeError(
            "--mysql-dsn is required: the acceptance smoke must verify demo seed data"
        )
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("--mysql-dsn requires pymysql in the smoke runtime") from exc

    parsed = urlparse(mysql_dsn)
    query = parse_qs(parsed.query)
    database = unquote(parsed.path.lstrip("/"))
    conn = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
        charset=query.get("charset", ["utf8mb4"])[0],
        connect_timeout=5,
        read_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                  COUNT(*) AS passed_jobs,
                  SUM(CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(extra, '$.demo_tag')) = %s THEN 1 ELSE 0 END) AS supplement_jobs,
                  SUM(CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(extra, '$.demo_tag')) = %s AND JSON_UNQUOTE(JSON_EXTRACT(extra, '$.demo_step')) = '5' THEN 1 ELSE 0 END) AS step5_jobs,
                  SUM(CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(extra, '$.demo_tag')) = %s AND JSON_UNQUOTE(JSON_EXTRACT(extra, '$.demo_step')) = '6' THEN 1 ELSE 0 END) AS step6_jobs,
                  SUM(CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(extra, '$.demo_tag')) = %s AND JSON_UNQUOTE(JSON_EXTRACT(extra, '$.demo_step')) = '8' THEN 1 ELSE 0 END) AS step8_jobs
                FROM job
                WHERE audit_status = 'passed'
                  AND deleted_at IS NULL
                  AND city LIKE %s
                  AND job_category LIKE %s
                """,
                (
                    seed_version,
                    seed_version,
                    seed_version,
                    seed_version,
                    f"%{city_like}%",
                    f"%{job_category_like}%",
                ),
            )
            row = cursor.fetchone() or {}
            passed_jobs = int(row.get("passed_jobs") or row.get("count") or 0)
            supplement_jobs = int(row.get("supplement_jobs") or 0)
            scenario_counts = {
                "5": int(row.get("step5_jobs") or 0),
                "6": int(row.get("step6_jobs") or 0),
                "8": int(row.get("step8_jobs") or 0),
            }
    finally:
        conn.close()
    if passed_jobs < min_passed_jobs:
        raise RuntimeError(
            f"seed check failed: passed_jobs={passed_jobs}, "
            f"min_passed_jobs={min_passed_jobs}, "
            f"city_like={city_like}, job_category_like={job_category_like}"
        )
    required_scenarios = {"5": min_step5_jobs, "6": min_step6_jobs, "8": min_step8_jobs}
    if supplement_jobs < min_supplement_jobs or any(
        scenario_counts[key] < minimum
        for key, minimum in required_scenarios.items()
    ):
        raise RuntimeError(
            "seed check failed: "
            f"seed_version={seed_version}, supplement_jobs={supplement_jobs}, "
            f"scenario_counts={scenario_counts}, "
            f"required_supplement_jobs={min_supplement_jobs}, "
            f"required_scenarios={required_scenarios}"
        )
    return {
        "checked": True,
        "seed_version": seed_version,
        "passed_jobs": passed_jobs,
        "supplement_jobs": supplement_jobs,
        "scenario_counts": scenario_counts,
        "city_like": city_like,
        "job_category_like": job_category_like,
    }


def cleanup_redis_smoke_data(
    redis_client,
    userid: str,
    msg_ids: list[str],
    *,
    delete_session: bool = True,
) -> dict:
    """Remove only this run's session and queued payloads."""
    deleted_queue_items = 0
    for raw_item in redis_client.lrange(QUEUE_INCOMING, 0, -1):
        try:
            item = raw_item.decode("utf-8") if isinstance(raw_item, bytes) else raw_item
            payload = json.loads(item)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if payload.get("from_userid") != userid and payload.get("msg_id") not in msg_ids:
            continue
        deleted_queue_items += int(redis_client.lrem(QUEUE_INCOMING, 0, raw_item) or 0)
    session_deleted = (
        int(redis_client.delete(f"session:{userid}") or 0)
        if delete_session else 0
    )
    return {
        "session_deleted": bool(session_deleted),
        "queue_items_deleted": deleted_queue_items,
    }


def cleanup_mysql_smoke_data(mysql_dsn: str, userid: str, msg_ids: list[str]) -> dict:
    """Delete rows created by the fresh smoke userid, including failed runs."""
    if not mysql_dsn:
        return {"checked": False, "reason": "mysql_dsn_not_set"}
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("MySQL cleanup requires pymysql in the smoke runtime") from exc

    parsed = urlparse(mysql_dsn)
    query = parse_qs(parsed.query)
    database = unquote(parsed.path.lstrip("/"))
    conn = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
        charset=query.get("charset", ["utf8mb4"])[0],
        connect_timeout=5,
        read_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )
    deleted: dict[str, int] = {}
    try:
        with conn.cursor() as cursor:
            statements = (
                ("conversation_log", "DELETE FROM conversation_log WHERE userid = %s", (userid,)),
                ("wecom_inbound_event", "DELETE FROM wecom_inbound_event WHERE from_userid = %s", (userid,)),
                ("audit_log", "DELETE FROM audit_log WHERE target_type = 'user' AND target_id = %s", (userid,)),
                ("resume", "DELETE FROM resume WHERE owner_userid = %s", (userid,)),
                ("job", "DELETE FROM job WHERE owner_userid = %s", (userid,)),
                ("user", "DELETE FROM `user` WHERE external_userid = %s", (userid,)),
            )
            for table, statement, params in statements:
                cursor.execute(statement, params)
                deleted[table] = int(cursor.rowcount or 0)
        conn.commit()
    finally:
        conn.close()
    return {"checked": True, "deleted": deleted, "msg_ids": len(msg_ids)}


def assert_text(payloads: list[dict], expects: list[str], rejects: list[str]) -> None:
    text = "\n".join(
        str(payload.get("text", {}).get("content", ""))
        for payload in payloads
    )
    missing = [needle for needle in expects if needle not in text]
    forbidden = [needle for needle in rejects if needle and needle in text]
    if missing or forbidden:
        raise AssertionError(
            "unexpected smoke reply\n"
            f"missing={missing}\n"
            f"forbidden={forbidden}\n"
            f"reply={text}"
        )


def run(args: argparse.Namespace) -> int:
    import redis

    userid = f"{args.user_prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    r = redis.Redis.from_url(args.redis_url, decode_responses=False)
    r.delete(f"session:{userid}")

    channel = f"mock:outbound:{userid}"
    pubsub = None
    message_ids: list[str] = []
    enqueued_message_ids: set[str] = set()
    completed_message_ids: set[str] = set()
    result: dict | None = None
    error: Exception | None = None
    try:
        health_check(args.base_url, args.timeout_seconds)
        if args.skip_seed_check:
            raise RuntimeError(
                "--skip-seed-check is not allowed for a successful acceptance smoke"
            )
        seed_status = check_mysql_seed(
            args.mysql_dsn,
            args.min_passed_jobs,
            city_like=args.seed_city_like,
            job_category_like=args.seed_job_category_like,
            seed_version=args.seed_version,
            min_supplement_jobs=args.min_supplement_jobs,
            min_step5_jobs=args.min_step5_jobs,
            min_step6_jobs=args.min_step6_jobs,
            min_step8_jobs=args.min_step8_jobs,
        )
        pubsub = r.pubsub()
        pubsub.subscribe(channel)
        wait_for_subscribe(pubsub, args.timeout_seconds)
        messages = list(args.message or [DEFAULT_SEARCH_MESSAGE])
        if not args.no_warmup:
            messages.insert(0, args.warmup_message)
        outbound: list[dict] = []
        for content in messages:
            payload = build_inbound_payload(userid, content)
            payload["inbound_event_id"] = create_mysql_inbound_event(args.mysql_dsn, payload)
            message_ids.append(payload["msg_id"])
            r.rpush(QUEUE_INCOMING, json.dumps(payload, ensure_ascii=False))
            enqueued_message_ids.add(payload["msg_id"])
            outbound.extend(
                wait_for_worker_done(
                    pubsub,
                    args.mysql_dsn,
                    payload["msg_id"],
                    args.timeout_seconds,
                )
            )
            completed_message_ids.add(payload["msg_id"])
        assert_text(outbound, args.expect, args.reject)
        result = {
            "ok": True,
            "userid": userid,
            "channel": channel,
            "reply_count": len(outbound),
            "message_count": len(messages),
            "seed": seed_status,
        }
    except Exception as exc:  # noqa: BLE001 - CLI should print diagnostics
        error = exc
    finally:
        cleanup_errors: list[str] = []
        pending_message_ids = enqueued_message_ids - completed_message_ids
        if pending_message_ids:
            redis_cleanup = {
                "skipped": True,
                "reason": "worker_completion_not_confirmed",
            }
            mysql_cleanup = {
                "skipped": True,
                "reason": "worker_completion_not_confirmed",
            }
            cleanup_errors.append(
                "cleanup skipped because worker completion was not confirmed for "
                f"msg_ids={sorted(pending_message_ids)}"
            )
        else:
            try:
                redis_cleanup = cleanup_redis_smoke_data(
                    r,
                    userid,
                    message_ids,
                    delete_session=not args.keep_session,
                )
            except Exception as exc:  # noqa: BLE001 - cleanup must be reported
                redis_cleanup = None
                cleanup_errors.append(f"Redis cleanup failed: {exc}")
            try:
                mysql_cleanup = cleanup_mysql_smoke_data(
                    args.mysql_dsn,
                    userid,
                    message_ids,
                )
            except Exception as exc:  # noqa: BLE001 - cleanup must be reported
                mysql_cleanup = None
                cleanup_errors.append(f"MySQL cleanup failed: {exc}")
        if pubsub is not None:
            try:
                pubsub.unsubscribe(channel)
                pubsub.close()
            except Exception:
                pass
        if error is None and cleanup_errors:
            error = RuntimeError("; ".join(cleanup_errors))
        if error is not None:
            diagnostics = {
                "ok": False,
                "userid": userid,
                "channel": channel,
                "queue_length": r.llen(QUEUE_INCOMING),
                "session_exists": bool(r.exists(f"session:{userid}")),
                "redis_cleanup": redis_cleanup,
                "mysql_cleanup": mysql_cleanup,
                "cleanup_errors": cleanup_errors,
                "error": str(error),
            }
            print(json.dumps(diagnostics, ensure_ascii=False), file=sys.stderr)
            return 1
        result["redis_cleanup"] = redis_cleanup
        result["mysql_cleanup"] = mysql_cleanup
        print(json.dumps(result, ensure_ascii=False))
        return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument(
        "--mysql-dsn",
        default="",
        help="MySQL DSN used to verify and clean demo seed data (required)",
    )
    parser.add_argument("--user-prefix", default="phase5_smoke")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--message", action="append", default=None)
    parser.add_argument("--warmup-message", default="你好")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--min-passed-jobs", type=int, default=1)
    parser.add_argument("--seed-city-like", default="苏州")
    parser.add_argument("--seed-job-category-like", default="电子")
    parser.add_argument("--seed-version", default="demo_supp_v1")
    parser.add_argument("--min-supplement-jobs", type=int, default=7)
    parser.add_argument("--min-step5-jobs", type=int, default=2)
    parser.add_argument("--min-step6-jobs", type=int, default=2)
    parser.add_argument("--min-step8-jobs", type=int, default=3)
    parser.add_argument("--skip-seed-check", action="store_true")
    parser.add_argument("--keep-session", action="store_true")
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--reject", action="append", default=[])
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
