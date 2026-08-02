"""Executable acceptance baseline for recommendation visibility P0."""
from dataclasses import FrozenInstanceError

import pytest

from app.services.visibility_contract import (
    AddressSource,
    BUILTIN_SAFE_POLICY,
    BUILTIN_SAFE_POLICY_ID,
    BUSINESS_DEFAULT_POLICY,
    CANDIDATE_FIELDS,
    ContactSource,
    HiringCompanySource,
    JOB_FIELDS,
    ROLE_SCENE_ACCESS,
    SENSITIVE_EXPANSION_FIELDS,
    SNAPSHOT_BEHAVIOR,
    WORKER_JOB_FIELDS,
    ViewerRole,
    VisibilityScene,
    hard_visibility_limit,
    registry_for,
)


def test_role_scene_hard_gate_is_frozen() -> None:
    assert ROLE_SCENE_ACCESS[VisibilityScene.JOB_SEARCH] == {
        ViewerRole.WORKER,
        ViewerRole.BROKER,
    }
    assert ViewerRole.FACTORY not in ROLE_SCENE_ACCESS[VisibilityScene.JOB_SEARCH]
    assert hard_visibility_limit(VisibilityScene.JOB_SEARCH, ViewerRole.FACTORY) == ()
    with pytest.raises(TypeError):
        ROLE_SCENE_ACCESS[VisibilityScene.JOB_SEARCH] = frozenset()  # type: ignore[index]


@pytest.mark.parametrize("policy", [BUSINESS_DEFAULT_POLICY, BUILTIN_SAFE_POLICY])
def test_worker_job_search_is_always_exact_three_fields(policy) -> None:
    assert policy[VisibilityScene.JOB_SEARCH][ViewerRole.WORKER] == WORKER_JOB_FIELDS
    assert hard_visibility_limit(
        VisibilityScene.JOB_SEARCH, ViewerRole.WORKER,
    ) == ("hiring_company", "job_category", "salary")


def test_builtin_fallback_is_distinct_and_never_contains_high_sensitivity() -> None:
    assert BUILTIN_SAFE_POLICY_ID == "builtin-safe-v1"
    assert (
        BUSINESS_DEFAULT_POLICY[VisibilityScene.JOB_SEARCH][ViewerRole.BROKER]
        != BUILTIN_SAFE_POLICY[VisibilityScene.JOB_SEARCH][ViewerRole.BROKER]
    )
    for role_map in BUILTIN_SAFE_POLICY.values():
        for fields in role_map.values():
            assert not (set(fields) & SENSITIVE_EXPANSION_FIELDS)


def test_every_default_field_is_registered_and_ordered() -> None:
    for scene, role_map in BUSINESS_DEFAULT_POLICY.items():
        registered = tuple(registry_for(scene))
        for fields in role_map.values():
            assert fields == tuple(key for key in registered if key in fields)


def test_field_contract_covers_sources_missing_rules_and_ranking_projection() -> None:
    for field in (*JOB_FIELDS, *CANDIDATE_FIELDS):
        assert field.source_fields
        assert field.data_subject
        assert field.missing_value_rule
        assert field.display_transform
        assert field.default_visible_roles
        if not field.reranker_allowed:
            assert field.ranking_projection == ()

    job_registry = registry_for(VisibilityScene.JOB_SEARCH)
    assert job_registry["benefits"].source_fields == (
        "job.provide_meal", "job.provide_housing",
    )
    assert job_registry["phone"].sensitive is True
    assert job_registry["phone"].reranker_allowed is False
    assert job_registry["address"].reranker_allowed is False
    assert job_registry["contact_person"].reranker_allowed is False

    for scene, fields in (
        (VisibilityScene.JOB_SEARCH, JOB_FIELDS),
        (VisibilityScene.CANDIDATE_SEARCH, CANDIDATE_FIELDS),
    ):
        for field in fields:
            expected_roles = {
                role
                for role, visible_fields in BUSINESS_DEFAULT_POLICY[scene].items()
                if field.key in visible_fields
            }
            assert set(field.default_visible_roles) == expected_roles


def test_source_metadata_enums_are_closed_and_unambiguous() -> None:
    assert {item.value for item in HiringCompanySource} == {
        "job.hiring_company", "publisher_company_fallback", "none",
    }
    assert {item.value for item in AddressSource} == {
        "job.address", "publisher_address_fallback", "none",
    }
    assert {item.value for item in ContactSource} == {
        "job_override", "publisher_fallback", "none",
    }


def test_snapshot_behavior_uses_ids_and_current_policy_for_show_more() -> None:
    assert SNAPSHOT_BEHAVIOR.load_policy_once_per_request is True
    assert SNAPSHOT_BEHAVIOR.candidate_snapshot_payload == "ids_only"
    assert SNAPSHOT_BEHAVIOR.show_more_policy_revision == "current_request_revision"
    assert SNAPSHOT_BEHAVIOR.policy_change_effect == "next_generated_reply"
    with pytest.raises(FrozenInstanceError):
        SNAPSHOT_BEHAVIOR.candidate_snapshot_payload = "full_candidates"  # type: ignore[misc]
