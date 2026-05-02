"""岗位相关 DTO。"""
from datetime import datetime

from pydantic import BaseModel, Field


class JobBase(BaseModel):
    """岗位公共字段。"""
    city: str = Field(..., max_length=32)
    job_category: str = Field(..., max_length=32)
    salary_floor_monthly: int
    pay_type: str = Field(..., description="月薪/时薪/计件")
    headcount: int
    gender_required: str = "不限"
    age_min: int | None = None
    age_max: int | None = None
    is_long_term: bool = True
    raw_text: str


class JobCreate(JobBase):
    """创建岗位。"""
    owner_userid: str = Field(..., max_length=64)
    expires_at: datetime

    # 软匹配字段（可选）
    district: str | None = None
    salary_ceiling_monthly: int | None = None
    provide_meal: bool | None = None
    provide_housing: bool | None = None
    dorm_condition: str | None = None
    shift_pattern: str | None = None
    work_hours: str | None = None
    accept_couple: bool | None = None
    accept_student: bool | None = None
    accept_minority: bool | None = None
    height_required: str | None = None
    experience_required: str | None = None
    education_required: str | None = "不限"
    rebate: str | None = None
    employment_type: str | None = None
    contract_type: str | None = None
    min_duration: str | None = None
    job_sub_category: str | None = None
    description: str | None = None
    images: list[str] | None = None
    miniprogram_url: str | None = None


class JobUpdate(BaseModel):
    """更新岗位（所有字段可选）。"""
    city: str | None = None
    job_category: str | None = None
    salary_floor_monthly: int | None = None
    salary_ceiling_monthly: int | None = None
    pay_type: str | None = None
    headcount: int | None = None
    gender_required: str | None = None
    age_min: int | None = None
    age_max: int | None = None
    is_long_term: bool | None = None
    district: str | None = None
    provide_meal: bool | None = None
    provide_housing: bool | None = None
    dorm_condition: str | None = None
    shift_pattern: str | None = None
    work_hours: str | None = None
    accept_couple: bool | None = None
    accept_student: bool | None = None
    accept_minority: bool | None = None
    height_required: str | None = None
    experience_required: str | None = None
    education_required: str | None = None
    rebate: str | None = None
    employment_type: str | None = None
    contract_type: str | None = None
    min_duration: str | None = None
    job_sub_category: str | None = None
    raw_text: str | None = None
    description: str | None = None
    images: list[str] | None = None
    miniprogram_url: str | None = None
    expires_at: datetime | None = None
    delist_reason: str | None = None


class JobRead(BaseModel):
    """岗位完整输出 DTO。"""
    id: int
    owner_userid: str

    # 硬过滤
    city: str
    job_category: str
    salary_floor_monthly: int
    pay_type: str
    headcount: int
    gender_required: str
    age_min: int | None = None
    age_max: int | None = None
    is_long_term: bool

    # 软匹配
    district: str | None = None
    address: str | None = None
    salary_ceiling_monthly: int | None = None
    provide_meal: bool | None = None
    provide_housing: bool | None = None
    dorm_condition: str | None = None
    shift_pattern: str | None = None
    work_hours: str | None = None
    accept_couple: bool | None = None
    accept_student: bool | None = None
    accept_minority: bool | None = None
    height_required: str | None = None
    experience_required: str | None = None
    education_required: str | None = None
    rebate: str | None = None
    employment_type: str | None = None
    contract_type: str | None = None
    min_duration: str | None = None
    job_sub_category: str | None = None

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
    delist_reason: str | None = None
    deleted_at: datetime | None = None

    version: int
    extra: dict | None = None

    # ---- 发布者（owner）信息：admin 后台展示用，由 router 层 join 注入 ----
    owner_phone: str | None = None
    owner_company: str | None = None
    owner_contact_person: str | None = None
    owner_address: str | None = None
    owner_role: str | None = None
    owner_display_name: str | None = None

    model_config = {"from_attributes": True}


class JobBrief(BaseModel):
    """岗位摘要 DTO（用于搜索结果列表）。"""
    id: int
    city: str
    job_category: str
    salary_floor_monthly: int
    salary_ceiling_monthly: int | None = None
    pay_type: str
    headcount: int
    gender_required: str
    is_long_term: bool
    district: str | None = None
    provide_meal: bool | None = None
    provide_housing: bool | None = None
    description: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
