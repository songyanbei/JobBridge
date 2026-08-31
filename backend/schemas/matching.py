"""Search direction and MatchingPolicy DTOs."""
from typing import Literal
from pydantic import BaseModel, Field

SearchDirection = Literal["search_job", "search_worker"]
RecruitmentDirection = Literal["worker_to_job", "factory_to_worker", "broker_to_job", "broker_to_worker"]


class MatchingRequest(BaseModel):
    direction: RecruitmentDirection
    criteria: dict = Field(default_factory=dict)
    policy_version: str = "matching-policy-v1"
