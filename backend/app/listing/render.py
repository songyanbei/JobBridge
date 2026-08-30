"""Fixed, privacy-safe rendering for worker job listing cards."""
from __future__ import annotations

from collections.abc import Iterable

from app.schemas.search import ListingCard


def redact_resume_for_viewer(resume, *, role: str) -> dict:
    """Return the bounded candidate-search projection for factory/broker."""
    if role not in {"factory", "broker"}:
        return {"id": getattr(resume, "id", None)}
    age = getattr(resume, "age", None)
    age_band = None
    if age is not None:
        age_band = f"{(int(age)//5)*5}-{(int(age)//5)*5+4}岁"
    return {
        "id": getattr(resume, "id", None),
        "gender": getattr(resume, "gender", None),
        "age_band": age_band,
        "expected_job_categories": list(getattr(resume, "expected_job_categories", None) or []),
        "expected_cities": list(getattr(resume, "expected_cities", None) or []),
        "salary_expect_floor_monthly": getattr(resume, "salary_expect_floor_monthly", None),
        "work_experience": str(getattr(resume, "work_experience", None) or "")[:120],
        "contact_placeholder": "回复“联系”获取沟通入口",
    }


def render_listing_card(card: ListingCard, *, position: int | None = None) -> str:
    """Render one card using a stable template owned by the application."""
    marker = f"{position}. " if position is not None else ""
    lines = [f"{marker}{card.title}"]
    if card.body_summary:
        lines.append(f"   {card.body_summary}")
    if card.location_text:
        lines.append(f"   地点：{card.location_text}")
    attributes = card.attributes
    if attributes.get("salary"):
        lines.append(f"   薪资：{attributes['salary']}")
    for label, key in (("班次", "shift_pattern"), ("福利", "benefits"), ("用工类型", "employment_type")):
        value = attributes.get(key)
        if value:
            if isinstance(value, (list, tuple)):
                value = "、".join(str(item) for item in value)
            lines.append(f"   {label}：{value}")
    if card.explanation:
        lines.append(f"   匹配依据：{card.explanation}")
    lines.append(f"   {card.contact_action}")
    return "\n".join(lines)


def render_listing_cards(cards: Iterable[ListingCard], *, has_more: bool = False) -> str:
    """Render cards in input order; no model-generated text is interpolated."""
    materialized = list(cards)
    if not materialized:
        return "暂无匹配结果。"
    lines = [f"为您找到 {len(materialized)} 个匹配岗位：", ""]
    for index, card in enumerate(materialized, 1):
        lines.append(render_listing_card(card, position=index))
        lines.append("")
    if has_more:
        lines.append('还有更多相关岗位，回复"更多"继续查看。')
    return "\n".join(lines).rstrip()


render_cards = render_listing_cards
render_listing_card_text = render_listing_card
render_listing_cards_text = render_listing_cards
