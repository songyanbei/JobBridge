"""LLM 能力抽象层（对应方案 §4.3）。

业务代码只依赖本文件中的 ABC 和数据结构，不依赖具体 provider。
切换供应商只需在 llm/__init__.py 的工厂函数里改注册。
"""
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class IntentResult(BaseModel):
    """IntentExtractor 的返回值。"""
    intent: str = Field(
        ...,
        description="意图类型: upload_job / upload_resume / search_job / search_worker / "
                    "upload_and_search / follow_up / show_more / command / chitchat",
    )
    structured_data: dict = Field(
        default_factory=dict,
        description="从用户文本中抽取出的结构化字段（对齐 §7 字段清单）",
    )
    criteria_patch: list[dict] = Field(
        default_factory=list,
        description="多轮对话的 criteria 增量更新指令列表，每项格式: "
                    '{"op": "add|update|remove", "field": "字段名", "value": "新值"}',
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="缺失的必填字段列表（用于触发追问）",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="整体置信度 0-1",
    )
    raw_response: str = Field(
        default="",
        description="LLM 原始输出（调试 & 日志用）",
    )
    # Phase 7：token 用量（OpenAI 兼容响应的 usage.prompt_tokens / completion_tokens），
    # 由 provider 从响应体提取并回填；解析失败 / 无 usage 时保持 None。
    input_tokens: int | None = Field(default=None, description="prompt_tokens")
    output_tokens: int | None = Field(default=None, description="completion_tokens")


class RerankResult(BaseModel):
    """Reranker 的返回值。"""
    ranked_items: list[dict] = Field(
        default_factory=list,
        description="排序后的候选集，每项含 id + score + 原始字段",
    )
    reply_text: str = Field(
        default="",
        description="LLM 生成的自然语言推荐回复（已按 §10.5 格式化）",
    )
    raw_response: str = Field(
        default="",
        description="LLM 原始输出",
    )
    # Phase 7：同 IntentResult。
    input_tokens: int | None = Field(default=None, description="prompt_tokens")
    output_tokens: int | None = Field(default=None, description="completion_tokens")


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------

class IntentExtractor(ABC):
    """意图抽取档：把用户自由文本解析为结构化 JSON + 意图分类。

    职责：
    - 判断 intent 类型（上传 / 检索 / 追问 / 闲聊 / 命令）
    - 从文本中抽取结构化字段
    - 检查必填字段缺失
    - 生成 criteria patch（多轮对话场景）
    """

    @abstractmethod
    def extract(
        self,
        text: str,
        role: str,
        history: list[dict] | None = None,
        current_criteria: dict | None = None,
        session_hint: dict | None = None,
    ) -> IntentResult:
        """解析一条用户消息。

        Args:
            text: 用户原始文本
            role: 用户角色 (worker / factory / broker)
            history: 最近 N 轮对话历史 [{"role":"user","content":"..."}, ...]
            current_criteria: 当前会话的累积检索条件（多轮 merge 用）
            session_hint: 当前会话状态摘要（active_flow / awaiting_fields / search_criteria 等）；
                Phase 1 起 provider 应把它结构化拼入 system prompt，未实现的旧
                provider 可忽略不报错。

        Returns:
            IntentResult
        """
        ...


class Reranker(ABC):
    """重排生成档：对候选集语义排序 + 生成自然语言推荐回复。"""

    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: list[dict],
        role: str,
        top_n: int = 3,
    ) -> RerankResult:
        """对候选集重排并生成回复。

        Args:
            query: 用户原始检索文本
            candidates: SQL 硬过滤后的候选集（字典列表，含全部字段）
            role: 用户角色（决定回复视角和可见字段）
            top_n: 返回的 Top N 条数

        Returns:
            RerankResult
        """
        ...
