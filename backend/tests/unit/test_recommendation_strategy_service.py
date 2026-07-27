"""§5 / §7 strategy-service contract tests."""
from __future__ import annotations

import pytest

from app.schemas.recommendation import TEMPLATE_DEFAULTS
from app.services.recommendation_strategy_service import (
    ReleaseStateError,
    canonical_parameters,
    parameters_digest,
    template_parameters,
    validate_direction,
)


@pytest.mark.parametrize("template_key", sorted(TEMPLATE_DEFAULTS))
def test_official_templates_round_trip_through_strict_schema(template_key):
    parameters = template_parameters(template_key)

    assert canonical_parameters(parameters) == TEMPLATE_DEFAULTS[template_key]
    assert sum(
        canonical_parameters(parameters)[name]
        for name in (
            "match_weight",
            "quality_weight",
            "freshness_weight",
            "exposure_weight",
        )
    ) == 100


def test_parameter_digest_is_canonical_and_sensitive_to_business_values():
    balanced = template_parameters("balanced")
    reordered = dict(reversed(list(canonical_parameters(balanced).items())))
    changed = canonical_parameters(balanced)
    changed.update({
        "match_weight": 71,
        "quality_weight": 9,
    })

    assert parameters_digest(balanced) == parameters_digest(reordered)
    assert parameters_digest(balanced) != parameters_digest(changed)
    assert len(parameters_digest(balanced)) == 64


def test_unknown_template_and_direction_fail_closed():
    with pytest.raises(ValueError, match="unknown recommendation template"):
        template_parameters("custom")
    with pytest.raises(ReleaseStateError, match="unknown recommendation direction"):
        validate_direction("search_company")
