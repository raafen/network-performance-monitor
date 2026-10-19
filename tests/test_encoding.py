"""Legacy Windows/redirected streams must retain measurements and bounds."""

from datetime import datetime, timezone
from io import BytesIO, TextIOWrapper

import pytest
from rich.console import Console

from network_monitor.display import MonitorDisplay
from network_monitor.models import ProbeResult, Statistics


@pytest.mark.parametrize("encoding", ["ascii", "cp1252"])
@pytest.mark.parametrize("width", [32, 120])
def test_legacy_stream_retains_bounds_failures_and_chart(encoding, width):
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding=encoding, errors="strict")
    console = Console(file=stream, width=width, force_terminal=False, color_system=None)
    stats = Statistics("host-\u7f51.test")
    display = MonitorDisplay(stats, console, 100, 250, animated=False)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with display:
        for result in (
            ProbeResult(stamp, stats.host, "reply", 1, latency_is_upper_bound=True),
            ProbeResult(stamp, stats.host, "timeout"),
            ProbeResult(stamp, stats.host, "error", error="denied \u7f51"),
        ):
            stats.record(result)
            display.record(result)
        display.set_probing(True)
        console.print(display)
    display.summary()
    stream.flush()
    output = raw.getvalue().decode(encoding)
    assert "<1 ms" in output
    assert "<=1 ms" in output
    assert "50.0%" in output
    assert "\\u7f51" in output
    assert "@" in output  # Actual reply remains visible in the ASCII chart.
    assert "timeout" in output and "error" in output
    assert "\x1b" not in output
    assert (stats.replies, stats.timeouts, stats.errors) == (1, 1, 1)
