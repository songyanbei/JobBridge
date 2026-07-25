"""Deterministic PII-free intent matrix for broad provider regression.

This matrix deliberately trades linguistic depth for breadth across roles, cities and
job categories. It complements (and must never be reported as a replacement for) the
manually labelled curated cases or anonymized historical replay.
"""
from __future__ import annotations


_CITIES = (
    "苏州", "杭州", "无锡", "常州", "上海",
    "北京", "宁波", "昆山", "东莞", "南京",
)
_JOBS = (
    "普工", "焊工", "电工", "仓管", "服务员",
    "保安", "叉车工", "操作工", "厨师", "装配工",
)


def _text(templates: tuple[str, ...], city: str, job: str, index: int) -> str:
    return templates[index % len(templates)].format(city=city, job=job)


def build_synthetic_intent_matrix() -> list[dict]:
    cases: list[dict] = []
    families = (
        (
            "worker-search",
            "worker",
            "search_job",
            (
                "帮我找{city}的{job}岗位",
                "我想去{city}做{job}",
                "{city}有没有{job}的活",
                "看看{city}{job}工作",
            ),
        ),
        (
            "worker-resume",
            "worker",
            "upload_resume",
            (
                "帮我登记简历，期望在{city}做{job}",
                "我要发布求职简历：{city}{job}",
                "提交个人资料，想找{city}的{job}工作",
                "这是我的简历，目标{city}{job}",
            ),
        ),
        (
            "factory-search",
            "factory",
            "search_worker",
            (
                "帮我找一名在{city}做{job}的工人",
                "想招{city}{job}师傅",
                "有没有愿意去{city}的{job}",
                "给公司找一个{city}{job}",
            ),
        ),
        (
            "factory-job",
            "factory",
            "upload_job",
            (
                "发布岗位：{city}{job}，招5人",
                "登记招聘信息，工作地{city}，工种{job}",
                "把这个岗位发出去：{city}{job}",
                "新增一条{city}{job}招聘岗位",
            ),
        ),
        (
            "broker-job",
            "broker",
            "search_job",
            (
                "帮这位工人找{city}{job}岗位",
                "给师傅看看{city}的{job}工作",
                "有个工人想去{city}做{job}",
                "替求职者找一份{city}{job}的活",
            ),
        ),
        (
            "broker-worker",
            "broker",
            "search_worker",
            (
                "帮企业找一个{city}{job}工人",
                "给厂家招一名{city}{job}",
                "有公司需要{city}{job}师傅",
                "替招聘方找{city}的{job}候选人",
            ),
        ),
    )
    for prefix, role, expected_intent, templates in families:
        for city_index, city in enumerate(_CITIES):
            for job_index, job in enumerate(_JOBS):
                index = city_index * len(_JOBS) + job_index
                cases.append({
                    "case_id": f"{prefix}-{city_index:02d}-{job_index:02d}",
                    "role": role,
                    "text": _text(templates, city, job, index),
                    "expected_intent": expected_intent,
                })
    return cases


SYNTHETIC_INTENT_MATRIX = build_synthetic_intent_matrix()
