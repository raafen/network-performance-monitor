"""Behavioral coverage for narrow rendering, real history, and literal output."""

from datetime import datetime, timezone
from io import StringIO

import pytest
from rich.cells import cell_len
from rich.console import Console

from network_monitor.display import MonitorDisplay, latency_text
from network_monitor.models import ProbeResult, Statistics


STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def result(status="reply", latency=1.0, **kwargs):
    return ProbeResult(STAMP, "example.test", status, latency if status == "reply" else None, **kwargs)


def screen(statistics, width=80):
    stream = StringIO()
    console = Console(file=stream, width=width, color_system=None, force_terminal=False)
    display = MonitorDisplay(statistics, console, 100, 250, animated=False)
    console.print(display)
    return stream.getvalue(), display


@pytest.mark.parametrize("width", [32, 48, 80, 120])
def test_render_respects_width_with_literal_host_and_error(width):
    stats = Statistics("[red]" + "long-host-" * 14 + "[/red]")
    stats.record(result("error", error="[bold]permission denied[/bold] " * 5))
    output, _ = screen(stats, width)
    assert all(cell_len(line) <= width for line in output.splitlines())
    assert "\x1b" not in output
    assert "[red]" in output
    assert "permission" in output
    assert "Execution error" in output


def test_chart_contains_measured_levels_and_failure_gap():
    stats = Statistics("example.test")
    stats.record(result(latency=0))
    stats.record(result("timeout"))
    stats.record(result(latency=10))
    output, _ = screen(stats)
    assert "▁ █" in output
    assert "50.0%" not in output  # One timeout among three completed network attempts.
    assert "33.3%" in output


def test_chart_wraps_all_sixty_samples_on_narrow_terminal():
    stats = Statistics("example.test")
    for latency in range(1, 64):
        stats.record(result(latency=latency))
    output, _ = screen(stats, 32)
    assert sum(output.count(glyph) for glyph in "▁▂▃▄▅▆▇█") == 60
    assert len(stats.history) == 60
    assert stats.probes == 63
    assert all(cell_len(line) <= 32 for line in output.splitlines())


def test_upper_bounds_are_preserved_in_latest_aggregates_and_summary():
    stats = Statistics("example.test")
    stats.record(result(latency=1.0, latency_is_upper_bound=True))
    output, display = screen(stats)
    assert "<1 ms" in output
    assert "≤1 ms" in output
    display.summary()
    assert "≤1 ms" in display.console.file.getvalue()[len(output):]
    assert display.statistics.average_ms == 1.0
    assert latency_text(1, True) == "<1 ms"
    assert latency_text(None) == "—"


def test_threshold_boundaries_are_distinct():
    _, display = screen(Statistics("example.test"))
    assert display.latency_style(99.99) == "green"
    assert display.latency_style(100) == "yellow"
    assert display.latency_style(249.99) == "yellow"
    assert display.latency_style(250) == "red"


def test_plain_records_have_no_ansi_or_interpreted_markup():
    stats = Statistics("example.test")
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=160)
    display = MonitorDisplay(stats, console, 100, 250, animated=False)
    samples = [result(latency=1, latency_is_upper_bound=True), result("timeout"), result("error", error="[red]denied[/red]\nnext")]
    with display:
        for sample in samples:
            stats.record(sample)
            display.record(sample)
    output = stream.getvalue()
    assert len(output.splitlines()) == 3
    assert "<1 ms" in output
    assert "timeout" in output
    assert "execution error" in output
    assert "[red]denied[/red] next" in output
    assert "\x1b" not in output


def test_empty_and_execution_error_statistics_do_not_claim_packet_loss():
    stats = Statistics("example.test")
    output, _ = screen(stats)
    assert not any(glyph in output for glyph in "▁▂▃▄▅▆▇█")
    assert "0.0%" not in output
    stats.record(result("error", error="denied"))
    output, _ = screen(stats)
    assert "0.0%" not in output
    assert "Execution error" in output


def test_probing_spinner_changes_with_time_without_creating_measurements(monkeypatch):
    clock = [10.0]
    stats = Statistics("example.test")
    stream = StringIO()
    console = Console(file=stream, color_system=None, width=80)
    monkeypatch.setattr(console, "get_time", lambda: clock[0])
    display = MonitorDisplay(stats, console, 100, 250, animated=True)
    display.set_probing(True)
    console.print(display)
    first = stream.getvalue()
    stream.seek(0)
    stream.truncate(0)
    clock[0] += 0.2
    console.print(display)
    second = stream.getvalue()
    assert "Probing" in first and "Probing" in second
    assert first != second
    assert stats.probes == 0
    assert not stats.history
