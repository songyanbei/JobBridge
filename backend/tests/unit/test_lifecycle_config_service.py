from types import SimpleNamespace
from app.services.lifecycle_config_service import get_job_ttl_days, get_job_candidate_ttl_days


class Query:
    def __init__(self, value): self.value = value
    def filter(self, *args): return self
    def first(self): return SimpleNamespace(config_value=self.value)
class DB:
    def __init__(self, value): self.value = value
    def query(self, *args): return Query(self.value)

def test_ttl_ranges_fallback_to_safe_defaults():
    assert get_job_ttl_days(DB("0")) == 30
    assert get_job_candidate_ttl_days(DB("366")) == 7
