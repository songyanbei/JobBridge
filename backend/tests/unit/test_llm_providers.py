"""LLM provider 单测：请求构造、响应解析、fallback、超时重试。

所有外部 HTTP 调用通过 mock httpx 完成，不依赖真实 API。
"""
import json
import pytest
from unittest.mock import patch, MagicMock

import httpx

from app.llm.base import IntentResult, RerankResult
from app.llm.providers.qwen import QwenIntentExtractor, QwenReranker
from app.llm.providers.doubao import DoubaoIntentExtractor, DoubaoReranker
from app.llm.providers._base import (
    parse_intent_response,
    parse_rerank_response,
    VALID_INTENTS,
    call_llm_api,
)
from app.core.exceptions import LLMError, LLMParseError, LLMTimeout


# ---------------------------------------------------------------------------
# Helper: 构造 mock httpx 响应
# ---------------------------------------------------------------------------

def _make_chat_response(
    content: str,
    status_code: int = 200,
    usage: dict | None = None,
) -> httpx.Response:
    """构造 OpenAI 兼容的 chat/completions 响应。

    Phase 7：可选 ``usage`` 让测试断言 provider 从响应中提取 token。
    """
    body: dict = {
        "choices": [{"message": {"content": content}}],
    }
    if usage is not None:
        body["usage"] = usage
    resp = httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "https://example.com"),
    )
    return resp


# ---------------------------------------------------------------------------
# parse_intent_response 单元测试
# ---------------------------------------------------------------------------

class TestParseIntentResponse:

    def test_valid_json(self):
        raw = json.dumps({
            "intent": "search_job",
            "structured_data": {"city": "深圳"},
            "criteria_patch": [],
            "missing_fields": [],
            "confidence": 0.85,
        })
        result = parse_intent_response(raw)
        assert isinstance(result, IntentResult)
        assert result.intent == "search_job"
        assert result.confidence == 0.85
        assert result.structured_data == {"city": "深圳"}
        assert result.raw_response == raw

    def test_invalid_json_raises(self):
        """Phase 7 契约变更：非法 JSON 不再软兜底，抛 LLMParseError 让上层记 parse_failed。"""
        with pytest.raises(LLMParseError):
            parse_intent_response("this is not json")

    def test_unknown_intent_fallback(self):
        """intent 未知仍走软兜底（已是合法 JSON + dict，不算根结构错误）。"""
        raw = json.dumps({"intent": "unknown_intent", "confidence": 0.5})
        result = parse_intent_response(raw)
        assert result.intent == "chitchat"
        assert result.raw_response == raw

    def test_empty_string_raises(self):
        """空字符串 → json.loads 失败 → 抛 LLMParseError。"""
        with pytest.raises(LLMParseError):
            parse_intent_response("")

    def test_none_raises(self):
        """None → json.loads(TypeError) → 抛 LLMParseError。"""
        with pytest.raises(LLMParseError):
            parse_intent_response(None)

    def test_all_valid_intents(self):
        for intent in VALID_INTENTS:
            raw = json.dumps({"intent": intent, "confidence": 0.9})
            result = parse_intent_response(raw)
            assert result.intent == intent

    def test_parse_error_has_no_user_facing_text(self):
        """parse 失败抛出的异常消息不应含面向用户的中文文案（属于运维错误，不回显）。"""
        with pytest.raises(LLMParseError) as excinfo:
            parse_intent_response("bad json")
        msg = str(excinfo.value)
        assert "没太理解" not in msg
        assert "系统繁忙" not in msg

    def test_raw_response_preserved(self):
        raw = json.dumps({"intent": "chitchat", "confidence": 0.1})
        result = parse_intent_response(raw)
        assert result.raw_response == raw

    def test_confidence_clamped_above_one(self):
        raw = json.dumps({"intent": "search_job", "confidence": 1.5})
        result = parse_intent_response(raw)
        assert result.confidence == 1.0

    def test_confidence_clamped_below_zero(self):
        raw = json.dumps({"intent": "search_job", "confidence": -0.5})
        result = parse_intent_response(raw)
        assert result.confidence == 0.0

    def test_confidence_non_numeric_fallback(self):
        raw = json.dumps({"intent": "search_job", "confidence": "high"})
        result = parse_intent_response(raw)
        assert result.confidence == 0.0

    def test_structured_data_wrong_type_fallback(self):
        """structured_data 是 string 而非 dict 时不抛 ValidationError。"""
        raw = json.dumps({
            "intent": "search_job", "structured_data": "oops",
            "criteria_patch": [], "missing_fields": [], "confidence": 0.8,
        })
        result = parse_intent_response(raw)
        assert result.structured_data == {}
        assert result.raw_response == raw

    def test_missing_fields_wrong_type_fallback(self):
        """missing_fields 是 string 而非 list 时不抛 ValidationError。"""
        raw = json.dumps({
            "intent": "search_job", "structured_data": {},
            "criteria_patch": [], "missing_fields": "job_category", "confidence": 0.8,
        })
        result = parse_intent_response(raw)
        assert result.missing_fields == []

    def test_criteria_patch_wrong_type_fallback(self):
        """criteria_patch 是 string 而非 list 时不抛 ValidationError。"""
        raw = json.dumps({
            "intent": "search_job", "structured_data": {},
            "criteria_patch": "bad", "missing_fields": [], "confidence": 0.8,
        })
        result = parse_intent_response(raw)
        assert result.criteria_patch == []

    def test_top_level_not_dict_raises(self):
        """顶层不是 dict 属根结构错误 → 抛 LLMParseError。"""
        raw = json.dumps([{"intent": "search_job"}])
        with pytest.raises(LLMParseError):
            parse_intent_response(raw)

    def test_intent_non_string_fallback(self):
        """intent 是 int 而非 string 时 fallback。"""
        raw = json.dumps({"intent": 123, "confidence": 0.5})
        result = parse_intent_response(raw)
        assert result.intent == "chitchat"


class TestParseRerankResponse:

    def test_valid_json(self):
        raw = json.dumps({
            "ranked_items": [{"id": 1, "score": 0.95}],
            "reply_text": "推荐如下",
        })
        result = parse_rerank_response(raw)
        assert isinstance(result, RerankResult)
        assert len(result.ranked_items) == 1
        assert result.reply_text == "推荐如下"
        assert result.raw_response == raw

    def test_invalid_json_raises(self):
        """Phase 7 契约变更：非法 JSON 抛 LLMParseError 让上层记 parse_failed。"""
        with pytest.raises(LLMParseError):
            parse_rerank_response("not json")

    def test_ranked_items_wrong_type_fallback(self):
        """ranked_items 是 string 而非 list 时不抛 ValidationError。"""
        raw = json.dumps({"ranked_items": "oops", "reply_text": "hi"})
        result = parse_rerank_response(raw)
        assert result.ranked_items == []
        assert result.reply_text == "hi"
        assert result.raw_response == raw

    def test_reply_text_wrong_type_coerced(self):
        """reply_text 是 int 时转为 string。"""
        raw = json.dumps({"ranked_items": [], "reply_text": 42})
        result = parse_rerank_response(raw)
        assert result.reply_text == "42"

    def test_top_level_list_raises(self):
        """顶层是 list 属根结构错误 → 抛 LLMParseError。"""
        raw = json.dumps([1, 2, 3])
        with pytest.raises(LLMParseError):
            parse_rerank_response(raw)


# ---------------------------------------------------------------------------
# call_llm_api 重试测试
# ---------------------------------------------------------------------------

class TestCallLlmApi:

    @patch("app.llm.providers._base.httpx.post")
    def test_success_first_try(self, mock_post):
        mock_post.return_value = httpx.Response(
            200, json={"ok": True},
            request=httpx.Request("POST", "https://example.com"),
        )
        resp = call_llm_api(url="https://example.com", headers={}, payload={}, timeout=10)
        assert resp.status_code == 200
        assert mock_post.call_count == 1

    @patch("app.llm.providers._base.httpx.post")
    def test_retry_once_on_timeout(self, mock_post):
        mock_post.side_effect = [
            httpx.TimeoutException("timeout"),
            httpx.Response(
                200, json={"ok": True},
                request=httpx.Request("POST", "https://example.com"),
            ),
        ]
        resp = call_llm_api(url="https://example.com", headers={}, payload={}, timeout=10)
        assert resp.status_code == 200
        assert mock_post.call_count == 2

    @patch("app.llm.providers._base.httpx.post")
    def test_raises_after_two_timeouts(self, mock_post):
        mock_post.side_effect = httpx.TimeoutException("timeout")
        with pytest.raises(httpx.TimeoutException):
            call_llm_api(url="https://example.com", headers={}, payload={}, timeout=10)
        assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# Qwen Provider 测试
# ---------------------------------------------------------------------------

class TestQwenIntentExtractor:

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_normal_response(self, mock_call):
        content = json.dumps({
            "intent": "search_job",
            "structured_data": {"city": "上海"},
            "criteria_patch": [],
            "missing_fields": ["job_category"],
            "confidence": 0.9,
        })
        mock_call.return_value = _make_chat_response(content)

        ext = QwenIntentExtractor()
        result = ext.extract("我想找上海的工作", role="worker")

        assert result.intent == "search_job"
        assert result.structured_data["city"] == "上海"
        assert result.missing_fields == ["job_category"]
        assert result.confidence == 0.9

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_non_json_raises_parse_error(self, mock_call):
        """Phase 7 契约：LLM 返回非 JSON 抛 LLMParseError（上层降级 + 记 parse_failed）。"""
        mock_call.return_value = _make_chat_response(
            "I don't understand",
            usage={"prompt_tokens": 123, "completion_tokens": 45},
        )

        ext = QwenIntentExtractor()
        with pytest.raises(LLMParseError) as excinfo:
            ext.extract("随便聊聊", role="worker")
        # Phase 7：usage 已在 provider 层提取并挂到异常上，让上层仍能记录 token 用量
        assert getattr(excinfo.value, "input_tokens", None) == 123
        assert getattr(excinfo.value, "output_tokens", None) == 45

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_timeout_raises_llm_timeout(self, mock_call):
        mock_call.side_effect = httpx.TimeoutException("timeout")

        ext = QwenIntentExtractor()
        with pytest.raises(LLMTimeout):
            ext.extract("test", role="worker")

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_http_status_error_raises_llm_error(self, mock_call):
        mock_call.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "https://example.com"),
            response=httpx.Response(500),
        )

        ext = QwenIntentExtractor()
        with pytest.raises(LLMError):
            ext.extract("test", role="worker")

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_request_uses_settings(self, mock_call):
        mock_call.return_value = _make_chat_response('{"intent":"chitchat","confidence":0.5}')

        ext = QwenIntentExtractor()
        ext.extract("hello", role="worker", history=[{"role": "user", "content": "hi"}])

        call_kwargs = mock_call.call_args
        assert "messages" in call_kwargs.kwargs["payload"]

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_success_populates_token_usage(self, mock_call):
        """Phase 7：成功解析时把 response.usage 回填到 IntentResult。"""
        mock_call.return_value = _make_chat_response(
            '{"intent":"chitchat","confidence":0.5}',
            usage={"prompt_tokens": 77, "completion_tokens": 12},
        )
        ext = QwenIntentExtractor()
        result = ext.extract("hi", role="worker")
        assert result.input_tokens == 77
        assert result.output_tokens == 12

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_success_missing_usage_yields_none(self, mock_call):
        """响应里没有 usage 字段时，token 保持 None（不报错）。"""
        mock_call.return_value = _make_chat_response(
            '{"intent":"chitchat","confidence":0.5}',
            usage=None,
        )
        ext = QwenIntentExtractor()
        result = ext.extract("hi", role="worker")
        assert result.input_tokens is None
        assert result.output_tokens is None


class TestQwenReranker:

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_normal_response(self, mock_call):
        content = json.dumps({
            "ranked_items": [{"id": 1, "score": 0.9}],
            "reply_text": "推荐岗位",
        })
        mock_call.return_value = _make_chat_response(content)

        rnk = QwenReranker()
        result = rnk.rerank("找工作", candidates=[{"id": 1}], role="worker")

        assert len(result.ranked_items) == 1
        assert result.reply_text == "推荐岗位"

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_timeout_raises(self, mock_call):
        mock_call.side_effect = httpx.TimeoutException("timeout")

        rnk = QwenReranker()
        with pytest.raises(LLMTimeout):
            rnk.rerank("test", candidates=[], role="worker")

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_success_populates_token_usage(self, mock_call):
        """Phase 7：成功解析时把 response.usage 回填到 RerankResult。"""
        content = json.dumps({"ranked_items": [{"id": 1}], "reply_text": "ok"})
        mock_call.return_value = _make_chat_response(
            content,
            usage={"prompt_tokens": 200, "completion_tokens": 30},
        )
        rnk = QwenReranker()
        result = rnk.rerank("test", candidates=[{"id": 1}], role="worker")
        assert result.input_tokens == 200
        assert result.output_tokens == 30

    @patch("app.llm.providers.qwen.call_llm_api")
    def test_non_json_raises_parse_error_with_usage(self, mock_call):
        """Phase 7 契约：rerank 同样在 parse 失败时把 usage 挂到 LLMParseError。"""
        mock_call.return_value = _make_chat_response(
            "not a json",
            usage={"prompt_tokens": 150, "completion_tokens": 0},
        )
        rnk = QwenReranker()
        with pytest.raises(LLMParseError) as excinfo:
            rnk.rerank("test", candidates=[{"id": 1}], role="worker")
        assert getattr(excinfo.value, "input_tokens", None) == 150
        assert getattr(excinfo.value, "output_tokens", None) == 0


# ---------------------------------------------------------------------------
# Doubao Provider 测试
# ---------------------------------------------------------------------------

class TestDoubaoIntentExtractor:

    @patch("app.llm.providers.doubao.call_llm_api")
    def test_normal_response(self, mock_call):
        content = json.dumps({
            "intent": "upload_resume",
            "structured_data": {"gender": "男"},
            "criteria_patch": [],
            "missing_fields": [],
            "confidence": 0.88,
        })
        mock_call.return_value = _make_chat_response(content)

        ext = DoubaoIntentExtractor()
        result = ext.extract("我是男的，想找工作", role="worker")

        assert result.intent == "upload_resume"
        assert result.confidence == 0.88

    @patch("app.llm.providers.doubao.call_llm_api")
    def test_non_json_raises_parse_error(self, mock_call):
        """Phase 7 契约：LLM 返回非 JSON 抛 LLMParseError + usage 回传到异常上。"""
        mock_call.return_value = _make_chat_response(
            "some garbage",
            usage={"prompt_tokens": 50, "completion_tokens": 8},
        )

        ext = DoubaoIntentExtractor()
        with pytest.raises(LLMParseError) as excinfo:
            ext.extract("test", role="worker")
        assert getattr(excinfo.value, "input_tokens", None) == 50
        assert getattr(excinfo.value, "output_tokens", None) == 8

    @patch("app.llm.providers.doubao.call_llm_api")
    def test_timeout_raises(self, mock_call):
        mock_call.side_effect = httpx.TimeoutException("timeout")

        ext = DoubaoIntentExtractor()
        with pytest.raises(LLMTimeout):
            ext.extract("test", role="worker")

    @patch("app.llm.providers.doubao.call_llm_api")
    def test_http_status_error_raises_llm_error(self, mock_call):
        mock_call.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "https://example.com"),
            response=httpx.Response(500),
        )

        ext = DoubaoIntentExtractor()
        with pytest.raises(LLMError):
            ext.extract("test", role="worker")

    @patch("app.llm.providers.doubao.call_llm_api")
    def test_mock_test_can_pass_without_real_key(self, mock_call):
        """doubao 在无真实 API Key 时也可通过 mock 测试。"""
        content = json.dumps({"intent": "chitchat", "confidence": 0.5})
        mock_call.return_value = _make_chat_response(content)

        ext = DoubaoIntentExtractor()
        result = ext.extract("hello", role="worker")
        assert result.intent == "chitchat"


class TestDoubaoReranker:

    @patch("app.llm.providers.doubao.call_llm_api")
    def test_normal_response(self, mock_call):
        content = json.dumps({
            "ranked_items": [{"id": 2, "score": 0.8}],
            "reply_text": "推荐",
        })
        mock_call.return_value = _make_chat_response(content)

        rnk = DoubaoReranker()
        result = rnk.rerank("找人", candidates=[{"id": 2}], role="factory")

        assert len(result.ranked_items) == 1
        assert result.ranked_items[0]["id"] == 2

    @patch("app.llm.providers.doubao.call_llm_api")
    def test_non_json_raises_parse_error_with_usage(self, mock_call):
        """Phase 7 契约：rerank 在 parse 失败时把 usage 挂到异常。"""
        mock_call.return_value = _make_chat_response(
            "garbage",
            usage={"prompt_tokens": 80, "completion_tokens": 0},
        )
        rnk = DoubaoReranker()
        with pytest.raises(LLMParseError) as excinfo:
            rnk.rerank("test", candidates=[{"id": 1}], role="worker")
        assert getattr(excinfo.value, "input_tokens", None) == 80
        assert getattr(excinfo.value, "output_tokens", None) == 0
