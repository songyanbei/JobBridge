"""简历相关 DTO。"""
from datetime import date, datetime

from pydantic import BaseModel, Field


class ResumeBase(BaseModel):
    """简历公共字段。"""
    expected_cities: list[str] = Field(..., min_length=1)
    expected_job_categories: list[str] = Field(..., min_length=1)
    salary_expect_floor_monthly: int
    gender: str = Field(..., description="男/女")
    age: int
    accept_long_term: bool = True
    accept_short_term: bool = False
    raw_text: str


class ResumeCreate(ResumeBase):
    """创建简历。"""
    owner_userid: str = Field(..., max_length=64)
    expires_at: datetime

    # 软匹配字段（可选）
    expected_districts: list[str] | None = None
    height: int | None = None
    weight: int | None = None
    education: str | None = "不限"
    work_experience: str | None = None
    accept_night_shift: bool | None = None
    accept_standing_work: bool | None = None
    accept_overtime: bool | None = None
    accept_outside_province: bool | None = None
    couple_seeking_together: bool | None = None
    has_health_certificate: bool | None = None
    ethnicity: str | None = None
    available_from: date | None = None
    has_tattoo: bool | None = None
    taboo: str | None = None
    description: str | None = None
    images: list[str] | None = None
    miniprogram_url: str | None = None


class ResumeUpdate(BaseModel):
    """更新简历（所有字段可选）。"""
    expected_cities: list[str] | None = None
    expected_job_categories: list[str] | None = None
    salary_expect_floor_monthly: int | None = None
    gender: str | None = None
    age: int | None = None
    accept_long_term: bool | None = None
    accept_short_term: bool | None = None
    expected_districts: list[str] | None = None
    height: int | None = None
    weight: int | None = None
    education: str | None = None
    work_experience: str | None = None
    accept_night_shift: bool | None = None
    accept_standing_work: bool | None = None
    accept_overtime: bool | None = None
    accept_outside_province: bool | None = None
    couple_seeking_together: bool | None = None
    has_health_certificate: bool | None = None
    ethnicity: str | None = None
    available_from: date | None = None
    has_tattoo: bool | None = None
    taboo: str | None = None
    raw_text: str | None = None
    description: str | None = None
    images: list[str] | None = None
    miniprogram_url: str | None = None
    expires_at: datetime | None = None


class ResumeRead(BaseModel):
    """简历完整输出 DTO。"""
    id: int
    owner_userid: str

    # 硬过滤
    expected_cities: list[str]
    expected_job_categories: list[str]
    salary_expect_floor_monthly: int
    gender: str
    age: int
    accept_long_term: bool
    accept_short_term: bool

    # 软匹配
    expected_districts: list[str] | None = None
    height: int | None = None
    weight: int | None = None
    education: str | None = None
    work_experience: str | None = None
    accept_night_shift: bool | None = None
    accept_standing_work: bool | None = None
    accept_overtime: bool | None = None
    accept_outside_province: bool | None = None
    couple_seeking_together: bool | None = None
    has_health_certificate: bool | None = None
    ethnicity: str | None = None
    available_from: date | None = None
    has_tattoo: bool | None = None
    taboo: str | None = None

    # 描述
    raw_text: str
    description: str | None = None

    # 媒体
    images: list[str] | None = None
    miniprogram_url: str | None = None

    # 审核
    audit_status: str
    audit_reason: str | None = None
    audited_by: str | None = None
    audited_at: datetime | None = None

    # 生命周期
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    deleted_at: datetime | None = None

    version: int
    extra: dict | None = None

    model_config = {"from_attributes": True}


class ResumeBrief(BaseModel):
    """简历摘要 DTO（用于搜索结果列表）。"""
    id: int
    expected_cities: list[str]
    expected_job_categories: list[str]
    salary_expect_floor_monthly: int
    gender: str
    age: int
    education: str | None = None
    description: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
