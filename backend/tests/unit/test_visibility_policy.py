"""P2 acceptance tests for the recommendation visibility policy service."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.services import permission_service, visibility_policy
from app.services.visibility_contract import ViewerRole, VisibilityScene
from app.services.visibility_policy import (
    BUILTIN_SAFE_POLICY_ID,
    PRIMARY_READ_EXECUTION_OPTION,
    EffectivePolicySnapshot,
    NormalizedPolicy,
    VisibilityPolicyValidationError,
    builtin_safe_snapshot,
    default_policy_document,
    load,
    normalize_policy,
    project_for_reranker,
    project_for_safe_log,
    project_soft_preferences,
    snapshot_from_policy,
)


class _ScalarResult:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _FakePrimarySession:
    def __init__(self, *rows, error: Exception | None = None):
        self.rows = list(rows)
        self.error = error
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if self.error:
            raise self.error
        row = self.rows.pop(0) if self.rows else None
        return _ScalarResult(row)


def _row(document: dict, *, value_type: str = "json"):
    return SimpleNamespace(
        config_value=json.dumps(document, ensure_ascii=False),
        value_type=value_type,
    )


def test_default_document_round_trips_and_is_registry_ordered() -> None:
    normalized = normalize_policy(default_policy_document())
    assert normalized.schema_version == 1
    assert normalized.revision == 1
    assert normalized.matrix[VisibilityScene.JOB_SEARCH][ViewerRole.WORKER] == (
        "hiring_company", "job_category", "salary",
    )
    assert normalized.as_dict() == default_policy_document()


def test_legal_empty_array_is_distinct_from_missing_config() -> None:
    document = default_policy_document()
    document["candidate_search"]["broker"] = []
    normalized = normalize_policy(document)
    snapshot = snapshot_from_policy(
        normalized, VisibilityScene.CANDIDATE_SEARCH, ViewerRole.BROKER,
    )
    assert snapshot.policy_source == "database"
    assert snapshot.visible_fields == ()
    assert snapshot.fallback_policy_id is None


def test_fields_removed_by_hard_limit_yield_database_empty_not_fallback() -> None:
    # Simulates a once-valid stored policy after a code-side safety ceiling was
    # tightened. Runtime intersection stays distinguishable from load failure.
    policy = NormalizedPolicy(
        schema_version=1,
        revision=4,
        matrix={
            VisibilityScene.JOB_SEARCH: {
                ViewerRole.FACTORY: ("job_category",),
            },
        },
    )
    snapshot = snapshot_from_policy(policy, "job_search", "factory")
    assert snapshot.policy_source == "database"
    assert snapshot.policy_revision == 4
    assert snapshot.visible_fields == ()
    assert snapshot.fallback_policy_id is None


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda doc: doc.update(schema_version=2), "unsupported_schema_version"),
        (lambda doc: doc.update(revision=0), "invalid_revision"),
        (lambda doc: doc.update(extra_scene={}), "unknown_scene"),
        (lambda doc: doc["job_search"].pop("broker"), "missing_role"),
        (lambda doc: doc["job_search"].update(intruder=[]), "unknown_role"),
        (
            lambda doc: doc["job_search"].update(worker=["job_category", "salary"]),
            "worker_job_fields_fixed",
        ),
        (
            lambda doc: doc["job_search"].update(
                worker=["hiring_company", "job_category", "salary", "city"],
            ),
            "field_exceeds_hard_limit",
        ),
        (
            lambda doc: doc["job_search"].update(factory=["job_category"]),
            "field_exceeds_hard_limit",
        ),
        (
            lambda doc: doc["job_search"].update(
                broker=["job_category", "job_category"],
            ),
            "duplicate_field",
        ),
        (
            lambda doc: doc["job_search"].update(broker=["database_column"]),
            "unknown_field",
        ),
    ],
)
def test_semantic_validation_rejects_unsafe_or_ambiguous_documents(mutate, code) -> None:
    document = default_policy_document()
    mutate(document)
    with pytest.raises(VisibilityPolicyValidationError) as exc:
        normalize_policy(document)
    assert exc.value.code == code


@pytest.mark.parametrize("raw", ["{broken", "[]", None])
def test_malformed_documents_are_rejected(raw) -> None:
    with pytest.raises(VisibilityPolicyValidationError):
        normalize_policy(raw)


def test_loader_reads_database_revision_from_primary_on_every_request() -> None:
    first = default_policy_document(revision=12)
    second = default_policy_document(revision=13)
    second["job_search"]["broker"] = ["job_category", "salary"]
    db = _FakePrimarySession(_row(first), _row(second))

    snapshot_12 = load(db, "job_search", "broker")
    snapshot_13 = load(db, "job_search", "broker")

    assert snapshot_12.policy_revision == 12
    assert snapshot_13.policy_revision == 13
    assert snapshot_13.visible_fields == ("job_category", "salary")
    assert len(db.statements) == 2
    assert all(
        statement.get_execution_options().get(PRIMARY_READ_EXECUTION_OPTION) is True
        for statement in db.statements
    )


@pytest.mark.parametrize(
    "db",
    [
        _FakePrimarySession(),
        _FakePrimarySession(_row(default_policy_document(), value_type="string")),
        _FakePrimarySession(SimpleNamespace(config_value="{broken", value_type="json")),
        _FakePrimarySession(error=RuntimeError("database unavailable")),
    ],
)
def test_loader_failure_never_reuses_old_policy_and_uses_builtin_safe(db) -> None:
    snapshot = load(db, "job_search", "broker")
    assert snapshot.policy_source == "builtin_safe_fallback"
    assert snapshot.policy_revision is None
    assert snapshot.fallback_policy_id == BUILTIN_SAFE_POLICY_ID
    assert "phone" not in snapshot.visible_fields
    assert "contact_person" not in snapshot.visible_fields
    assert "address" not in snapshot.visible_fields


def test_loader_failure_log_excludes_raw_config_and_sensitive_values(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(
        visibility_policy, "log_event", lambda event, **fields: events.append((event, fields)),
    )
    raw = '{"phone":"13800000000","address":"敏感门牌","broken":'
    snapshot = load(
        _FakePrimarySession(SimpleNamespace(config_value=raw, value_type="json")),
        "job_search",
        "broker",
    )
    assert snapshot.policy_source == "builtin_safe_fallback"
    serialized = repr(events)
    assert "13800000000" not in serialized
    assert "敏感门牌" not in serialized
    assert events[0][1]["loaded_revision"] is None


def test_load_failure_metric_and_deduplicated_threshold_alert(monkeypatch) -> None:
    events = []
    alerts = []
    monkeypatch.setattr(visibility_policy, "_load_failure_total", 0)
    monkeypatch.setattr(visibility_policy, "_consecutive_load_failures", 0)
    monkeypatch.setattr(
        visibility_policy, "log_event", lambda event, **fields: events.append((event, fields)),
    )
    from app.tasks import worker_monitor
    monkeypatch.setattr(worker_monitor, "_alert", lambda event, message: alerts.append((event, message)))
    for _ in range(4):
        load(_FakePrimarySession(None), "job_search", "broker")
    metrics = visibility_policy.visibility_policy_load_metrics()
    assert metrics["visibility_policy_load_failure_total"] == 4
    assert metrics["consecutive_failures"] == 4
    assert len(alerts) == 2
    assert all(event == "visibility_policy_load_alert" for event, _ in alerts)


def test_snapshot_is_immutable_and_unknown_role_has_empty_visibility() -> None:
    snapshot = builtin_safe_snapshot("job_search", "unknown")
    assert snapshot.visible_fields == ()
    assert snapshot.reranker_fields == ("id",)
    with pytest.raises(FrozenInstanceError):
        snapshot.visible_fields = ("phone",)  # type: ignore[misc]


def test_reranker_projection_keeps_id_and_only_visible_rankable_sources() -> None:
    snapshot = snapshot_from_policy(
        normalize_policy(default_policy_document(revision=9)),
        "job_search",
        "broker",
    )
    candidate = {
        "id": 7,
        "hiring_company": "工厂 A",
        "job_category": "普工",
        "salary_floor_monthly": 6000,
        "salary_ceiling_monthly": 7000,
        "pay_type": "月薪",
        "city": "苏州",
        "district": "工业园区",
        "provide_meal": True,
        "provide_housing": True,
        "shift_pattern": "两班倒",
        "work_hours": "12 小时",
        "phone": "13800000000",
        "contact_person": "张经理",
        "address": "敏感门牌地址",
        "publisher_company": "发布中介",
        "description": "任意未注册字段",
    }
    projected = project_for_reranker("job_search", "broker", snapshot, candidate)
    assert projected["id"] == 7
    assert projected["job_category"] == "普工"
    assert projected["salary_floor_monthly"] == 6000
    assert "phone" not in projected
    assert "contact_person" not in projected
    assert "address" not in projected
    assert "hiring_company" not in projected
    assert "publisher_company" not in projected
    assert "description" not in projected
    assert project_for_safe_log(candidate) == {"id": 7}


def test_forged_snapshot_cannot_bypass_hard_limit_or_ranking_registry() -> None:
    forged = EffectivePolicySnapshot(
        scene=VisibilityScene.JOB_SEARCH,
        role="worker",
        policy_source="database",
        policy_revision=99,
        fallback_policy_id=None,
        visible_fields=("hiring_company", "job_category", "salary", "city", "phone"),
        reranker_fields=("id", "job_category", "city", "phone"),
        soft_preference_fields=("phone",),
    )
    candidate = {
        "id": 8,
        "hiring_company": "工厂",
        "job_category": "普工",
        "salary_floor_monthly": 6000,
        "pay_type": "月薪",
        "city": "苏州",
        "phone": "13800000000",
    }
    projected = project_for_reranker("job_search", "worker", forged, candidate)
    filtered = permission_service.filter_job_for_role(candidate, "worker", forged)
    assert "city" not in projected
    assert "phone" not in projected
    assert "city" not in filtered
    assert "phone" not in filtered
    assert project_soft_preferences(forged, {"phone": "13800000000"}) == {}


def test_soft_preferences_are_limited_by_visible_registered_mapping() -> None:
    document = default_policy_document(revision=3)
    document["job_search"]["broker"] = ["job_category", "benefits"]
    snapshot = snapshot_from_policy(normalize_policy(document), "job_search", "broker")
    assert project_soft_preferences(
        snapshot,
        {
            "provide_meal": True,
            "provide_housing": False,
            "shift_pattern": "白班",
            "phone": "13800000000",
        },
    ) == {"provide_meal": True, "provide_housing": False}


def test_permission_service_uses_explicit_policy_whitelist_and_fails_closed() -> None:
    normalized = normalize_policy(default_policy_document(revision=5))
    worker_snapshot = snapshot_from_policy(normalized, "job_search", "worker")
    job = {
        "id": 1,
        "hiring_company": "真实工厂",
        "hiring_company_source": "job.hiring_company",
        "job_category": "普工",
        "salary_floor_monthly": 6000,
        "salary_ceiling_monthly": 7000,
        "pay_type": "月薪",
        "city": "苏州",
        "phone": "13800000000",
        "contact_person": "张经理",
        "address": "敏感门牌",
    }
    filtered = permission_service.filter_job_for_role(
        job, "worker", worker_snapshot,
    )
    assert filtered == {
        "id": 1,
        "hiring_company": "真实工厂",
        "hiring_company_source": "job.hiring_company",
        "job_category": "普工",
        "salary_floor_monthly": 6000,
        "salary_ceiling_monthly": 7000,
        "pay_type": "月薪",
    }
    assert permission_service.filter_job_for_role(
        job, "unknown", worker_snapshot,
    ) == {"id": 1}


def test_resume_phone_placeholder_only_exists_when_phone_field_is_visible() -> None:
    document = default_policy_document(revision=6)
    visible = snapshot_from_policy(normalize_policy(document), "candidate_search", "factory")
    hidden_doc = default_policy_document(revision=7)
    hidden_doc["candidate_search"]["factory"] = ["display_name", "gender_age"]
    hidden = snapshot_from_policy(
        normalize_policy(hidden_doc), "candidate_search", "factory",
    )
    resume = {"id": 2, "owner_userid": "worker-1", "gender": "男", "age": 30}
    owner = {"display_name": "求职者甲", "phone": None}

    visible_result = permission_service.filter_resume_for_role(
        resume, owner, "factory", visible,
    )
    hidden_result = permission_service.filter_resume_for_role(
        resume, owner, "factory", hidden,
    )
    assert visible_result["phone_placeholder"] == "联系方式待补充"
    assert "phone_placeholder" not in hidden_result
    assert "phone" not in hidden_result
