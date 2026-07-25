"""Deterministic OpenAI-compatible server for conversation LLM chaos tests."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNTS = Counter()
COUNTS_LOCK = threading.Lock()


def _fault_for_index(index: int) -> str:
    """Inject one logical fault per five users, cycling all four fault classes."""
    if index < 0 or index % 5:
        return "success"
    return ("timeout", "http_429", "http_500", "bad_json")[(index // 5) % 4]


def _response_content(body: dict) -> str:
    prompt = "\n".join(
        str(item.get("content", "")) for item in body.get("messages", [])
    )
    match = re.search(r"chaos_llm_mixed_[a-f0-9]+_(\d+)", prompt)
    index = int(match.group(1)) if match else -1
    if _fault_for_index(index) == "bad_json":
        return "not-json"
    if "ranked_items" in prompt:
        return json.dumps({"ranked_items": [], "reply_text": ""})
    if "dialogue_act" in prompt:
        return json.dumps({
            "dialogue_act": "start_search",
            "frame_hint": "job_search",
            "slots_delta": {"city": ["深圳"], "job_category": ["普工"]},
            "merge_hint": {},
            "needs_clarification": False,
            "confidence": 0.95,
        }, ensure_ascii=False)
    return json.dumps({
        "intent": "search_job",
        "structured_data": {"city": ["深圳"], "job_category": ["普工"]},
        "criteria_patch": [],
        "missing_fields": [],
        "confidence": 0.95,
    }, ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/shutdown":
            payload = b"stopping"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        with COUNTS_LOCK:
            payload = json.dumps(dict(COUNTS), sort_keys=True).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        prompt = "\n".join(
            str(item.get("content", "")) for item in body.get("messages", [])
        )
        match = re.search(r"chaos_llm_mixed_[a-f0-9]+_(\d+)", prompt)
        index = int(match.group(1)) if match else -1
        fault = _fault_for_index(index)
        with COUNTS_LOCK:
            COUNTS[fault] += 1
        if fault == "timeout":
            time.sleep(3)
        if fault in {"http_429", "http_500"}:
            self.send_response(429 if fault == "http_429" else 500)
            self.end_headers()
            return
        content = _response_content(body)
        payload = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        }, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def log_message(self, _format: str, *_args) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18080), Handler).serve_forever()
