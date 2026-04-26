"""会话状态管理服务（Phase 3）。

负责 Redis session 的读写、criteria_patch merge、快照管理、翻页和重置。
不负责决定何时生成候选 ID 列表（由 search_service 决定）。
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from app.core.redis_client import (
    delete_session as redis_delete_session,
    get_session as redis_get_session,
    save_session as redis_save_session,
)
from app.schemas.conversation import CandidateSnapshot, SessionState

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 12  # 最近 6 轮对话 = 12 条 message


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

def load_session(userid: str) -> SessionState | None:
    """从 Redis 加载 session，不存在则返回 None。"""
    data = redis_get_session(userid)
    if data is None:
        return None
    return SessionState(**data)


def create_session(userid: str, role: str) -> SessionState:
    """创建新的空 session。"""
    now = datetime.now(timezone.utc).isoformat()
    return SessionState(role=role, updated_at=now)


def save_session(userid: str, session: SessionState) -> None:
    """保存 session 到 Redis（自动续期 TTL）。"""
    session.updated_at = datetime.now(timezone.utc).isoformat()
    redis_save_session(userid, session.model_dump(mode="json"))


def clear_session(userid: str) -> None:
    """完全删除 Redis session。"""
    redis_delete_session(userid)


# ---------------------------------------------------------------------------
# criteria_patch merge
# ---------------------------------------------------------------------------

def merge_criteria_patch(session: SessionState, patches: list[dict]) -> bool:
    """应用 criteria_patch 到 session.search_criteria。

    返回 True 如果 criteria 实际发生了变化（需要清空快照）。
    """
    if not patches:
        return False

    old_digest = compute_query_digest(session.search_criteria)
    criteria = dict(session.search_criteria)  # 浅拷贝

    for patch in patches:
        op = patch.get("op")
        field = patch.get("field")
        value = patch.get("value")

        if op == "add":
            # 仅用于列表型字段，做去重追加
            existing = criteria.get(field, [])
            if not isinstance(existing, list):
                existing = [existing]
            if isinstance(value, list):
                for v in value:
                    if v not in existing:
                        existing.append(v)
            elif value not in existing:
                existing.append(value)
            criteria[field] = existing

        elif op == "update":
            criteria[field] = value

        elif op == "remove":
            if value is None:
                # 删除整个字段
                criteria.pop(field, None)
            else:
                # 从列表移除指定值
                existing = criteria.get(field, [])
                if isinstance(existing, list):
                    criteria[field] = [v for v in existing if v != value]
                    if not criteria[field]:
                        criteria.pop(field, None)
                else:
                    criteria.pop(field, None)

    session.search_criteria = criteria
    new_digest = compute_query_digest(criteria)

    if new_digest != old_digest:
        session.candidate_snapshot = None
        session.shown_items = []
        session.updated_at = datetime.now(timezone.utc).isoformat()
        return True

    return False


def compute_query_digest(criteria: dict) -> str:
    """对 search_criteria 计算稳定的摘要（SHA256 前 12 位）。"""
    if not criteria:
        return ""
    stable_json = json.dumps(criteria, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(stable_json.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 搜索重置
# ---------------------------------------------------------------------------

def reset_search(session: SessionState) -> None:
    """清空当前检索状态（/重新找）。不清 broker_direction。

    Stage A：当 pending_upload 草稿仍在编辑时（spec §9.7 / §3 验收 7），
    保留 current_intent 和 follow_up_rounds：
      - current_intent 让后续图片仍能挂到 Job/Resume；
      - follow_up_rounds 不重置，否则用户可以靠 /重新找 把 max-rounds 计数清零、
        从而无限刷"答非所问"绕过退出。
    """
    session.search_criteria = {}
    session.candidate_snapshot = None
    session.shown_items = []
    has_pending = bool(session.pending_upload_intent)
    if not has_pending:
        session.follow_up_rounds = 0
        session.current_intent = None
    # history 清空（固定策略）
    session.history = []


# ---------------------------------------------------------------------------
# 对话历史
# ---------------------------------------------------------------------------

def record_history(session: SessionState, role: str, content: str) -> None:
    """追加一条对话记录，截断到 MAX_HISTORY_MESSAGES 条。"""
    session.history.append({"role": role, "content": content})
    if len(session.history) > MAX_HISTORY_MESSAGES:
        session.history = session.history[-MAX_HISTORY_MESSAGES:]


# ---------------------------------------------------------------------------
# 快照管理
# ---------------------------------------------------------------------------

def save_snapshot(
    session: SessionState,
    candidate_ids: list[str],
    query_digest: str,
) -> None:
    """保存候选 ID 快照（由 search_service 在 rerank 后调用）。"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=30)
    session.candidate_snapshot = CandidateSnapshot(
        candidate_ids=candidate_ids,
        ranking_version=(
            (session.candidate_snapshot.ranking_version + 1)
            if session.candidate_snapshot
            else 1
        ),
        query_digest=query_digest,
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )
    session.shown_items = []


def is_snapshot_expired(session: SessionState) -> bool:
    """判断快照是否已过期。"""
    if session.candidate_snapshot is None:
        return True
    expires_at = session.candidate_snapshot.expires_at
    if not expires_at:
        return True
    try:
        expires_dt = datetime.fromisoformat(expires_at)
        return datetime.now(timezone.utc) > expires_dt
    except (ValueError, TypeError):
        return True


def invalidate_snapshot_if_expired(session: SessionState) -> bool:
    """如果快照已过期则清空，返回 True 表示已过期并被清空。"""
    if is_snapshot_expired(session):
        session.candidate_snapshot = None
        session.shown_items = []
        return True
    return False


def get_next_candidate_ids(session: SessionState, count: int) -> list[str]:
    """从快照中取下一批未展示的候选 ID。快照过期则返回空。"""
    if session.candidate_snapshot is None:
        return []
    if is_snapshot_expired(session):
        session.candidate_snapshot = None
        session.shown_items = []
        return []

    shown_set = set(session.shown_items)
    remaining = [
        cid for cid in session.candidate_snapshot.candidate_ids
        if cid not in shown_set
    ]
    return remaining[:count]


def get_remaining_count(session: SessionState) -> int:
    """获取快照中尚未展示的候选总数。"""
    if session.candidate_snapshot is None:
        return 0
    shown_set = set(session.shown_items)
    return sum(
        1 for cid in session.candidate_snapshot.candidate_ids
        if cid not in shown_set
    )


# ---------------------------------------------------------------------------
# shown_items 管理
# ---------------------------------------------------------------------------

def record_shown(session: SessionState, item_ids: list[str]) -> None:
    """记录已展示项，去重并保留顺序。"""
    existing = set(session.shown_items)
    for item_id in item_ids:
        if item_id not in existing:
            session.shown_items.append(item_id)
            existing.add(item_id)


# ---------------------------------------------------------------------------
# 中介方向切换
# ---------------------------------------------------------------------------

def set_broker_direction(
    session: SessionState,
    direction: str,
) -> str | None:
    """设置中介搜索方向。返回 None 表示成功，否则返回错误信息。"""
    if session.role != "broker":
        return "只有中介账号可以切换搜索方向"
    if direction not in ("search_job", "search_worker"):
        return f"无效的搜索方向: {direction}"
    session.broker_direction = direction
    # 切换方向时重置检索状态
    session.search_criteria = {}
    session.candidate_snapshot = None
    session.shown_items = []
    session.follow_up_rounds = 0
    return None


# ---------------------------------------------------------------------------
# 追问轮数
# ---------------------------------------------------------------------------

def increment_follow_up(session: SessionState) -> int:
    """累加追问轮数，返回新的轮数。"""
    session.follow_up_rounds += 1
    return session.follow_up_rounds
