"""字段级权限过滤服务（Phase 3）。

按角色对岗位/简历结果做字段脱敏，返回结构化数据供最终文本拼装。
过滤在 service 层执行，不依赖前端。
"""

# ---------------------------------------------------------------------------
# 工人侧需要隐藏的岗位字段
# ---------------------------------------------------------------------------

_WORKER_HIDDEN_JOB_FIELDS = frozenset({
    # 电话 / 联系方式
    "phone", "contact_person",
    # 歧视性展示字段
    "gender_required", "age_min", "age_max", "accept_minority",
})

# 岗位字段中属于关联用户的字段（需要从 user 数据中补充）
_JOB_USER_FIELDS = frozenset({
    "company", "contact_person", "phone",
})


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def filter_job_for_role(job_data: dict, viewer_role: str) -> dict:
    """过滤单条岗位结果。

    Args:
        job_data: 岗位字段字典（已包含关联用户数据）
        viewer_role: 查看者角色 (worker/factory/broker)

    Returns:
        过滤后的结构化字典
    """
    if viewer_role == "worker":
        return {
            k: v for k, v in job_data.items()
            if k not in _WORKER_HIDDEN_JOB_FIELDS
        }
    # factory / broker 看全量
    return dict(job_data)


def filter_resume_for_role(
    resume_data: dict,
    owner_user: dict | None,
    viewer_role: str,
) -> dict:
    """过滤单条简历结果。

    Args:
        resume_data: 简历字段字典
        owner_user: 简历所有者的用户数据 (display_name, phone 等)
        viewer_role: 查看者角色

    Returns:
        过滤后的结构化字典
    """
    result = dict(resume_data)

    if viewer_role in ("factory", "broker"):
        # 补充用户信息
        if owner_user:
            result["display_name"] = owner_user.get("display_name", "")
            phone = owner_user.get("phone")
            result["phone"] = phone if phone else None
            result["phone_placeholder"] = "联系方式待补充" if not phone else None
        else:
            result["phone"] = None
            result["phone_placeholder"] = "联系方式待补充"

    # worker 一般不会搜简历，但安全起见也做处理
    if viewer_role == "worker":
        result.pop("phone", None)
        result.pop("display_name", None)

    return result


def filter_jobs_batch(
    jobs: list[dict],
    viewer_role: str,
) -> list[dict]:
    """批量过滤岗位结果。"""
    return [filter_job_for_role(j, viewer_role) for j in jobs]


def filter_resumes_batch(
    resumes: list[dict],
    users_map: dict[str, dict],
    viewer_role: str,
) -> list[dict]:
    """批量过滤简历结果。

    Args:
        resumes: 简历字典列表
        users_map: {owner_userid: user_data} 映射
        viewer_role: 查看者角色
    """
    result = []
    for r in resumes:
        owner_userid = r.get("owner_userid", "")
        owner_user = users_map.get(owner_userid)
        result.append(filter_resume_for_role(r, owner_user, viewer_role))
    return result
