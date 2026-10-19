"""CLI validation and lifecycle tests with deterministic clocks and probe outcomes."""

import csv
import json
from datetime import datetime, timezone
from io import StringIO

import pytest
from rich.console import Console

from network_monitor import cli
from network_monitor.models import ProbeResult


STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


def invoke(arguments, *, terminal=False):
    args = cli.build_parser().parse_args(arguments)
    output = StringIO()
    errors = StringIO()
    console = Console(file=output, force_terminal=terminal, color_system="standard" if terminal else None, width=200)
    error_console = Console(file=errors, force_terminal=False, color_system=None, width=200)
    code = cli._monitor(args, console, error_console, "/test/ping")
    return code, output.getvalue(), errors.getvalue()


@pytest.mark.parametrize("arguments", [
    ["--interval", "0"], ["--interval", "nan"], ["--interval", "inf"],
    ["--timeout", "-1"], ["--timeout", "nan"], ["--timeout", "inf"],
    ["--timeout", "1e308"],
    ["--count", "0"], ["--count", "-2"], ["--count", "1.5"],
    ["--warning-ms", "nan"], ["--critical-ms", "inf"], ["--warning-ms", "-1"],
    ["--warning-ms", "250", "--critical-ms", "250"],
    ["--warning-ms", "251", "--critical-ms", "250"],
    [""], ["a b"], ["host\nname"], ["a\x00b"], ["a\u200bb"], ["x" * 254], ["--", "-host"],
])
def test_invalid_configuration_is_an_argparse_error(arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "fe80::1%en0", "example.test", "a" * 253])
def test_host_accepts_addresses_and_full_length_names(host):
    assert cli.build_parser().parse_args([host]).host == host


def test_missing_system_ping_is_a_configuration_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["--count", "1"]) == 1
    assert "ping" in capsys.readouterr().err


def test_mixed_completed_attempts_schedule_without_overlap_and_log_real_results(monkeypatch, tmp_path):
    clock = Clock()
    monkeypatch.setattr(cli, "time", clock)
    starts = []
    statuses = ["reply", "error", "timeout"]
    durations = [0.2, 1.5, 0.1]

    def probe(host, timeout, executable):
        index = len(starts)
        starts.append(clock.now)
        clock.now += durations[index]
        status = statuses[index]
        return ProbeResult(STAMP, host, status, 2.5 if status == "reply" else None, "denied" if status == "error" else None)

    monkeypatch.setattr(cli, "run_probe", probe)
    csv_path = tmp_path / "results.csv"
    jsonl_path = tmp_path / "results.jsonl"
    code, output, errors = invoke(["example.test", "--count", "3", "--interval", "1", "--csv", str(csv_path), "--jsonl", str(jsonl_path)])
    assert code == 1
    assert starts == pytest.approx([0, 1, 2.5])
    assert clock.sleeps == pytest.approx([0.8])
    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows] == statuses
    assert [record["status"] for record in records] == statuses
    assert records[0]["latency_ms"] == 2.5
    assert "Attempts: 3" in output
    assert "50.0%" in output  # The execution error is excluded from packet loss.
    assert "\x1b" not in output
    assert errors == ""


@pytest.mark.parametrize("terminal", [False, True])
def test_success_and_timeout_are_not_execution_failures_and_no_animation_is_plain(monkeypatch, terminal):
    clock = Clock()
    monkeypatch.setattr(cli, "time", clock)
    outcomes = iter([ProbeResult(STAMP, "example.test", "timeout"), ProbeResult(STAMP, "example.test", "reply", 1, latency_is_upper_bound=True)])
    monkeypatch.setattr(cli, "run_probe", lambda *args: next(outcomes))
    code, output, errors = invoke(["example.test", "--count", "2", "--no-animation"], terminal=terminal)
    assert code == 0
    assert "timeout" in output
    assert "<1 ms" in output
    assert "≤1 ms" in output
    assert "\x1b" not in output
    assert errors == ""


@pytest.mark.parametrize("during", ["probe", "wait"])
def test_interrupt_during_probe_or_wait_summarizes_only_completed_attempts(monkeypatch, tmp_path, during):
    clock = Clock()
    monkeypatch.setattr(cli, "time", clock)

    def interrupt(*args):
        raise KeyboardInterrupt

    if during == "probe":
        monkeypatch.setattr(cli, "run_probe", interrupt)
    else:
        monkeypatch.setattr(cli, "run_probe", lambda *args: ProbeResult(STAMP, "example.test", "reply", 3))
        monkeypatch.setattr(clock, "sleep", interrupt)
    path = tmp_path / "results.jsonl"
    code, output, errors = invoke(["example.test", "--jsonl", str(path)])
    assert code == 130
    assert "Interrupted" in output
    assert f"Attempts: {0 if during == 'probe' else 1}" in output
    assert len(path.read_text(encoding="utf-8").splitlines()) == (0 if during == "probe" else 1)
    assert errors == ""
    path.unlink()  # Also exercises that no Windows file handle remains open.


def test_log_setup_failure_prints_summary_without_probing(monkeypatch, tmp_path):
    def unexpected_probe(*args):
        pytest.fail("A probe must not start when log setup has failed")

    monkeypatch.setattr(cli, "run_probe", unexpected_probe)
    path = tmp_path / "missing" / "results.csv"
    code, output, errors = invoke(["example.test", "--count", "1", "--csv", str(path)])
    assert code == 1
    assert "Attempts: 0" in output
    assert errors


def test_log_write_failure_stops_and_closes_both_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "run_probe", lambda *args: ProbeResult(STAMP, "example.test", "reply", 4))
    handles = []
    original_write = cli.ResultLogger.write

    def failed_write(self, result):
        original_write(self, result)
        handles.extend([self._csv_file, self._jsonl_file])
        raise OSError("disk full [red]")

    monkeypatch.setattr(cli.ResultLogger, "write", failed_write)
    csv_path = tmp_path / "results.csv"
    jsonl_path = tmp_path / "results.jsonl"
    code, output, errors = invoke(["example.test", "--count", "3", "--csv", str(csv_path), "--jsonl", str(jsonl_path)])
    assert code == 1
    assert "Attempts: 1" in output
    assert "disk full [red]" in errors
    assert handles and all(handle.closed for handle in handles)
    assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 1


def test_same_log_destination_is_fatal_before_first_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "run_probe", lambda *args: pytest.fail("Unexpected probe"))
    path = tmp_path / "results"
    code, output, errors = invoke(["example.test", "--count", "1", "--csv", str(path), "--jsonl", str(path)])
    assert code == 1
    assert "Attempts: 0" in output
    assert errors


def test_extreme_finite_interval_remains_interruptible(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(cli, "time", clock)
    monkeypatch.setattr(cli, "run_probe", lambda *args: ProbeResult(STAMP, "example.test", "reply", 1))

    def interruptible_sleep(delay):
        assert 0 < delay <= 86400
        raise KeyboardInterrupt

    monkeypatch.setattr(clock, "sleep", interruptible_sleep)
    code, output, errors = invoke(["example.test", "--count", "2", "--interval", "1e308"])
    assert code == 130
    assert "Attempts: 1" in output
    assert errors == ""
