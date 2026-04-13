import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.config import settings
from app.core.redis_client import (
    QUEUE_DEAD_LETTER,
    QUEUE_INCOMING,
    check_msg_duplicate,
    check_rate_limit,
    dequeue_message,
    enqueue_message,
    get_redis,
)
from app.llm.providers.qwen import QwenIntentExtractor, QwenReranker
from app.storage.local import LocalStorage
from app.wecom.callback import parse_message
from app.wecom.client import WeComClient
from app.wecom.crypto import decrypt_message, encrypt_message, generate_signature, verify_signature


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * ratio) - 1))
    return ordered[idx]


def summarize(values: list[float], total: int, unit_key: str, wall_duration: float) -> dict:
    if not values:
        return {"count": 0, "avg_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, unit_key: 0.0}
    return {
        "count": len(values),
        "avg_ms": round((sum(values) / len(values)) * 1000, 3),
        "p50_ms": round(percentile(values, 0.5) * 1000, 3),
        "p95_ms": round(percentile(values, 0.95) * 1000, 3),
        unit_key: round(total / max(wall_duration, 0.0001), 2),
    }


def cleanup_redis(prefixes: list[str]) -> None:
    redis_client = get_redis()
    for prefix in prefixes:
        for key in redis_client.scan_iter(f"{prefix}*"):
            redis_client.delete(key)


def build_chat_response(url: str, content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", url),
    )


def fake_llm_api(*, url: str, headers: dict, payload: dict, timeout: int) -> httpx.Response:
    model = payload.get("model")
    if model == settings.llm_intent_model:
        content = json.dumps(
            {
                "intent": "search_job",
                "structured_data": {"city": "shanghai", "job_category": "general_worker"},
                "criteria_patch": [],
                "missing_fields": [],
                "confidence": 0.91,
            }
        )
    else:
        content = json.dumps(
            {
                "ranked_items": [{"id": 1, "score": 0.93, "city": "shanghai", "job_category": "general_worker"}],
                "reply_text": "top 1 result",
            }
        )
    return build_chat_response(url, content)


def run_infra_flow(messages: int, ingress_workers: int, consumer_workers: int) -> dict:
    redis_client = get_redis()
    redis_client.delete(QUEUE_INCOMING)
    redis_client.delete(QUEUE_DEAD_LETTER)
    cleanup_redis(["rate:pressure_", "msg:pressure_"])

    temp_dir = tempfile.mkdtemp(prefix="jb_phase2_pressure_", dir=os.getcwd())
    storage = LocalStorage(base_dir=temp_dir, base_url="/files")
    token = "phase2_token"
    corp_id = "wx_phase2_pressure"
    aes_key = base64.b64encode(os.urandom(32)).decode("utf-8").rstrip("=")[:43]

    ingress_latencies: list[float] = []
    consumer_latencies: list[float] = []
    errors: list[str] = []

    def ingress_task(i: int) -> float:
        msg_id = f"pressure_{i}"
        user = f"pressure_user_{i % 80}"
        xml = (
            f"<xml><MsgId>{msg_id}</MsgId><FromUserName><![CDATA[{user}]]></FromUserName>"
            f"<ToUserName><![CDATA[corp001]]></ToUserName><MsgType><![CDATA[text]]></MsgType>"
            f"<Content><![CDATA[I need a job in shanghai {i}]]></Content><CreateTime>{1700000000 + i}</CreateTime></xml>"
        )
        encrypt = encrypt_message(aes_key, xml, corp_id)
        signature = generate_signature(token, "1700000000", f"nonce_{i}", encrypt)

        started = time.perf_counter()
        assert verify_signature(token, "1700000000", f"nonce_{i}", encrypt, signature)
        plain = decrypt_message(aes_key, encrypt, corp_id)
        msg = parse_message(plain)
        assert check_rate_limit(msg.from_user, window=120, max_count=100000) is True
        assert check_msg_duplicate(msg.msg_id) is False

        enqueue_message(
            json.dumps(
                {
                    "msg_id": msg.msg_id,
                    "from_user": msg.from_user,
                    "msg_type": msg.msg_type,
                    "content": msg.content,
                    "media_id": msg.media_id,
                    "create_time": msg.create_time,
                }
            )
        )
        return time.perf_counter() - started

    ingress_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=ingress_workers) as executor:
        futures = [executor.submit(ingress_task, i) for i in range(messages)]
        for future in as_completed(futures):
            try:
                ingress_latencies.append(future.result())
            except Exception as exc:
                errors.append(f"ingress:{type(exc).__name__}:{exc}")
    ingress_wall = time.perf_counter() - ingress_started

    with patch("app.llm.providers.qwen.call_llm_api", side_effect=fake_llm_api):

        def consumer_task(expected_count: int) -> list[float]:
            extractor = QwenIntentExtractor()
            reranker = QwenReranker()
            latencies: list[float] = []
            for _ in range(expected_count):
                started = time.perf_counter()
                raw = dequeue_message(timeout=5)
                if raw is None:
                    raise RuntimeError("queue drained too early")
                payload = json.loads(raw)
                extract_result = extractor.extract(payload["content"], role="worker")
                rerank_result = reranker.rerank(
                    payload["content"],
                    [{"id": 1, "city": "shanghai", "job_category": "general_worker"}],
                    role="worker",
                    top_n=1,
                )
                assert extract_result.intent == "search_job"
                assert rerank_result.ranked_items[0]["id"] == 1
                key = f"jobs/{payload['msg_id']}/{payload['msg_id']}.txt"
                storage.save(key, payload["content"].encode("utf-8"), content_type="text/plain")
                assert storage.exists(key) is True
                assert storage.delete(key) is True
                latencies.append(time.perf_counter() - started)
            return latencies

        base = messages // consumer_workers
        extra = messages % consumer_workers
        consumer_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=consumer_workers) as executor:
            futures = []
            for idx in range(consumer_workers):
                count = base + (1 if idx < extra else 0)
                if count:
                    futures.append(executor.submit(consumer_task, count))
            for future in as_completed(futures):
                try:
                    consumer_latencies.extend(future.result())
                except Exception as exc:
                    errors.append(f"consumer:{type(exc).__name__}:{exc}")
        consumer_wall = time.perf_counter() - consumer_started

    remaining = redis_client.llen(QUEUE_INCOMING)
    dead_letter = redis_client.llen(QUEUE_DEAD_LETTER)
    shutil.rmtree(temp_dir, ignore_errors=True)
    cleanup_redis(["rate:pressure_", "msg:pressure_"])
    redis_client.delete(QUEUE_INCOMING)
    redis_client.delete(QUEUE_DEAD_LETTER)

    return {
        "messages": messages,
        "ingress": summarize(ingress_latencies, messages, "throughput_msg_s", ingress_wall),
        "consumer": summarize(consumer_latencies, messages, "throughput_msg_s", consumer_wall),
        "queue_remaining": remaining,
        "dead_letter": dead_letter,
        "errors": errors,
    }


def run_wecom_client_mock(iterations: int, workers: int) -> dict:
    client = WeComClient(corp_id="corp", secret="secret", agent_id="1000001", timeout=5)
    client._access_token = "token"
    client._token_expires_at = time.time() + 3600
    latencies: list[float] = []
    errors: list[str] = []

    def fake_post(url, params=None, json=None, timeout=None):
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}, request=httpx.Request("POST", url))

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/media/get"):
            return httpx.Response(
                200,
                content=b"binary",
                headers={"content-type": "image/jpeg"},
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200,
            json={"errcode": 0, "external_contact": {"external_userid": params.get("external_userid"), "name": "tester"}},
            request=httpx.Request("GET", url),
        )

    def task(i: int) -> float:
        started = time.perf_counter()
        if i % 3 == 0:
            client.send_text(f"user_{i}", "hello")
        elif i % 3 == 1:
            client.download_media(f"media_{i}")
        else:
            client.get_external_contact(f"ext_{i}")
        return time.perf_counter() - started

    started = time.perf_counter()
    with patch("app.wecom.client.httpx.post", side_effect=fake_post), patch(
        "app.wecom.client.httpx.get", side_effect=fake_get
    ):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(task, i) for i in range(iterations)]
            for future in as_completed(futures):
                try:
                    latencies.append(future.result())
                except Exception as exc:
                    errors.append(f"client:{type(exc).__name__}:{exc}")
    wall_duration = time.perf_counter() - started

    result = summarize(latencies, iterations, "throughput_req_s", wall_duration)
    result["iterations"] = iterations
    result["errors"] = errors
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 pressure test runner")
    parser.add_argument("--messages", type=int, default=1200, help="Composite infra-flow message count")
    parser.add_argument("--ingress-workers", type=int, default=24, help="Ingress thread count")
    parser.add_argument("--consumer-workers", type=int, default=12, help="Consumer thread count")
    parser.add_argument("--client-iterations", type=int, default=900, help="Mock WeCom client request count")
    parser.add_argument("--client-workers", type=int, default=30, help="Mock WeCom client thread count")
    args = parser.parse_args()

    summary = {
        "infra_flow": run_infra_flow(args.messages, args.ingress_workers, args.consumer_workers),
        "wecom_client_mock": run_wecom_client_mock(args.client_iterations, args.client_workers),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
