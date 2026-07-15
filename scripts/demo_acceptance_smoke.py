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
from urllib.request import urlopen


QUEUE_INCOMING = "queue:incoming"


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


def wait_for_subscribe(pubsub, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        message = pubsub.get_message(timeout=0.2)
        if message and message.get("type") == "subscribe":
            return
    raise TimeoutError("Pub/Sub subscribe ack not received before timeout")


def wait_for_outbound(pubsub, timeout_seconds: int) -> list[dict]:
    deadline = time.time() + timeout_seconds
    payloads: list[dict] = []
    while time.time() < deadline:
        message = pubsub.get_message(timeout=0.5)
        if not message or message.get("type") != "message":
            continue
        data = message.get("data")
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        payloads.append(json.loads(data))
        return payloads
    raise TimeoutError("No outbound mock WeCom message received before timeout")


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

    health_check(args.base_url, args.timeout_seconds)

    channel = f"mock:outbound:{userid}"
    pubsub = r.pubsub()
    try:
        pubsub.subscribe(channel)
        wait_for_subscribe(pubsub, args.timeout_seconds)
        payload = build_inbound_payload(userid, args.message)
        r.rpush(QUEUE_INCOMING, json.dumps(payload, ensure_ascii=False))
        outbound = wait_for_outbound(pubsub, args.timeout_seconds)
        assert_text(outbound, args.expect, args.reject)
        print(json.dumps({
            "ok": True,
            "userid": userid,
            "channel": channel,
            "reply_count": len(outbound),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should print diagnostics
        diagnostics = {
            "ok": False,
            "userid": userid,
            "channel": channel,
            "queue_length": r.llen(QUEUE_INCOMING),
            "session_exists": bool(r.exists(f"session:{userid}")),
            "error": str(exc),
        }
        print(json.dumps(diagnostics, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        try:
            pubsub.unsubscribe(channel)
            pubsub.close()
        except Exception:
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--mysql-dsn", default="", help="Accepted for runbook parity; app/worker own DB access")
    parser.add_argument("--user-prefix", default="phase5_smoke")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--message", default="帮我找苏州电子厂岗位，5500以上，最好包吃住")
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--reject", action="append", default=[])
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
