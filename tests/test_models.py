from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from network_monitor.models import ProbeResult, Statistics


def result(status="reply", latency=4.0, bound=False):
    return ProbeResult(
        datetime.now(timezone.utc),
        "example.test",
        status,
        latency if status == "reply" else None,
        "execution failed" if status == "error" else None,
        bound,
    )


def test_result_is_immutable():
    sample = result()
    with pytest.raises(FrozenInstanceError):
        sample.host = "different.test"


def test_empty_and_execution_only_statistics_have_no_loss_or_latency():
    stats = Statistics("example.test")
    assert stats.probes == 0
    assert stats.latest is None
    assert stats.loss_percent is None
    assert stats.minimum_ms is None
    assert stats.average_ms is None
    assert stats.maximum_ms is None
    assert not stats.has_upper_bounds
    stats.record(result("error"))
    assert stats.probes == stats.errors == 1
    assert stats.loss_percent is None
    assert stats.average_ms is None


def test_errors_are_excluded_from_loss_and_latency_aggregates():
    stats = Statistics("example.test")
    for sample in (
        result(latency=2.0),
        result("timeout"),
        result("error"),
        result(latency=8.0),
        result("error"),
    ):
        stats.record(sample)
    assert (stats.probes, stats.replies, stats.timeouts, stats.errors) == (5, 2, 1, 2)
    assert stats.loss_percent == pytest.approx(100 / 3)
    assert (stats.minimum_ms, stats.average_ms, stats.maximum_ms) == (2.0, 5.0, 8.0)
    assert stats.latest == sample


def test_history_is_bounded_but_statistics_are_cumulative():
    stats = Statistics("example.test")
    first = result(latency=1.0, bound=True)
    stats.record(first)
    for _ in range(65):
        stats.record(result(latency=7.0))
    assert stats.probes == stats.replies == 66
    assert len(stats.history) == stats.history.maxlen == 60
    assert first not in stats.history
    assert stats.minimum_ms == 1.0
    assert stats.maximum_ms == 7.0
    assert stats.average_ms == pytest.approx((1 + 65 * 7) / 66)
    assert stats.has_upper_bounds
    assert stats.loss_percent == 0.0


def test_all_timeouts_are_full_loss_not_zero_latency():
    stats = Statistics("example.test")
    stats.record(result("timeout"))
    assert stats.loss_percent == 100.0
    assert stats.average_ms is None
    assert stats.minimum_ms is None
    assert stats.maximum_ms is None


def test_histories_are_not_shared_between_instances():
    first = Statistics("example.test")
    second = Statistics("example.test")
    first.record(result())
    assert not second.history
    assert second.probes == 0
