from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from scripts.phase10_clock_check import collect_clock_report


class _TimedRedis:
    def __init__(self, values):
        self._values = iter(values)

    def time(self):
        return next(self._values)


@pytest.mark.parametrize(
    ("mysql_epoch", "redis_times", "expected_skew", "ready"),
    [
        (
            Decimal("100.500000"),
            [(100, 400000), (100, 600000)],
            0.1,
            True,
        ),
        (
            Decimal("102.600000"),
            [(100, 0), (100, 500000)],
            2.6,
            False,
        ),
        (
            Decimal("98.000000"),
            [(100, 0), (100, 0)],
            2.0,
            True,
        ),
        (
            Decimal("103.000000"),
            [(100, 0), (106, 0)],
            3.0,
            False,
        ),
    ],
)
def test_clock_report_uses_redis_sampling_window(
    mysql_epoch, redis_times, expected_skew, ready,
):
    db = MagicMock()
    db.execute.return_value.scalar_one.return_value = mysql_epoch

    report = collect_clock_report(db, _TimedRedis(redis_times))

    assert report["clock_skew_seconds"] == expected_skew
    assert report["max_clock_skew_seconds"] == 2.0
    assert report["ready"] is ready


def test_clock_report_exposes_redis_sampling_window():
    db = MagicMock()
    db.execute.return_value.scalar_one.return_value = Decimal("100.500000")

    report = collect_clock_report(db, _TimedRedis([(100, 0), (101, 0)]))

    assert report["sampling_window_seconds"] == 1.0
