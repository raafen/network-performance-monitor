import math
import os
import subprocess
from datetime import datetime, timezone

import pytest

from network_monitor import probe


@pytest.mark.parametrize(
    ("system", "timeout", "arguments"),
    [
        ("Linux", 0.1, ["-n", "-c", "1", "-W", "1"]),
        ("Linux", 1.01, ["-n", "-c", "1", "-W", "2"]),
        ("Darwin", 0.1251, ["-n", "-c", "1", "-W", "126"]),
        ("Windows", 0.1251, ["-n", "1", "-w", "126"]),
    ],
)
def test_exactly_one_request_and_platform_timeout_units(system, timeout, arguments):
    assert probe.build_command("host.test", timeout, "/ping", system) == [
        "/ping", *arguments, "host.test"
    ]


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan, 1e308])
def test_invalid_timeout_is_an_execution_error(timeout):
    result = probe.run_probe("host.test", timeout, "/does-not-exist", "Linux")
    assert result.status == "error"
    assert result.latency_ms is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.042 ms", (0.042, False)),
        ("64 bytes from ::1: icmp_seq=0 hlim=64 time=1.234 ms", (1.234, False)),
        ("Reply from 127.0.0.1: bytes=32 time<1ms TTL=128", (1.0, True)),
        ("Reply from ::1: time=0ms", (0.0, False)),
        ("Reply from ::1: time=2,5 ms", (2.5, False)),
        ("time=0.5 ms (unrelated diagnostic)", None),
        ("64 bytes from host: time=-2 ms", None),
        ("rtt min/avg/max/mdev = 1.0/2.0/3.0/0.0 ms", None),
        ("64 bytes from host: time=nan ms", None),
    ],
)
def test_only_actual_reply_latencies_are_parsed(output, expected):
    assert probe.parse_latency(output) == expected


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (1, "Request timed out."),
        (2, "Request timeout for icmp_seq 0"),
        (2, "1 packets transmitted, 0 packets received, 100.0% packet loss"),
        (1, "1 packets transmitted, 0 received, 100% packet loss, time 0ms"),
        (0, "Packets: Sent = 1, Received = 0, Lost = 1 (100% loss)"),
        (1, "From 192.0.2.1 icmp_seq=1 Destination Host Unreachable"),
        (0, "Reply from 192.0.2.1: Destination net unreachable."),
        (1, "From 192.0.2.1 icmp_seq=1 Time to live exceeded"),
        (1, "From 192.0.2.1 icmp_seq=1 Packet filtered"),
    ],
)
def test_evidenced_no_reply_is_timeout_despite_overlapping_exit_codes(code, output):
    result = probe.classify_output("host.test", code, output)
    assert result.status == "timeout"
    assert result.latency_ms is None
    assert result.error is None


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (68, "ping: cannot resolve missing.invalid: Unknown host"),
        (2, "ping: host.test: Name or service not known"),
        (1, "Ping request could not find host missing.invalid. Please check the name."),
        (2, "ping: socket: Operation not permitted\n100% packet loss"),
        (1, "ping: sendmsg: Network is unreachable\n100% packet loss"),
        (2, "ping: sendto: No route to host\n100.0% packet loss"),
        (1, "ping: local error: message too long\n100% packet loss"),
        (0, "PING: transmit failed. General failure."),
        (0, ""),
        (1, "unrecognized failure"),
        (-9, "100% packet loss"),
        (0, "Reply from host: bytes=32 TTL=128"),
    ],
)
def test_execution_errors_never_invent_packet_loss(code, output):
    result = probe.classify_output("host.test", code, output)
    assert result.status == "error"
    assert result.error
    assert result.latency_ms is None


def test_reply_preserves_upper_bound_and_completion_timestamp():
    before = datetime.now(timezone.utc)
    result = probe.classify_output("host.test", 0, "Reply from host.test: time<1ms")
    after = datetime.now(timezone.utc)
    assert result.status == "reply"
    assert result.latency_ms == 1.0
    assert result.latency_is_upper_bound
    assert before <= result.timestamp <= after
    assert result.timestamp.utcoffset().total_seconds() == 0


def test_missing_utility_and_unsupported_system_are_execution_errors(tmp_path):
    missing = probe.run_probe("host.test", 1, str(tmp_path / "missing"), "Linux")
    assert missing.status == "error"
    unsupported = probe.run_probe("host.test", 1, "ping", "UnknownOS")
    assert unsupported.status == "error"


class Child:
    """Controlled subprocess boundary for deadline and interruption behavior."""

    def __init__(self, exception=None, output="Request timed out."):
        self.exception = exception
        self.output = output
        self.returncode = 1
        self.killed = False
        self.reaped = False
        self.deadline = None

    def communicate(self, timeout=None):
        if timeout is not None:
            self.deadline = timeout
            if self.exception is not None:
                raise self.exception
        else:
            self.reaped = True
        return self.output, None

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_deadline_kills_and_reaps_without_counting_packet_loss(monkeypatch, system):
    child = Child(subprocess.TimeoutExpired("ping", 0.1))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: child)
    result = probe.run_probe("host.test", 0.1, "ping", system)
    assert child.killed and child.reaped
    assert result.status == "error"
    assert "deadline" in result.error.lower()


def test_keyboard_interrupt_kills_and_reaps_before_propagating(monkeypatch):
    child = Child(KeyboardInterrupt())
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: child)
    with pytest.raises(KeyboardInterrupt):
        probe.run_probe("host.test", 1, "ping", "Linux")
    assert child.killed and child.reaped


def test_output_read_error_kills_and_reaps(monkeypatch):
    child = Child(OSError("broken pipe"))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: child)
    result = probe.run_probe("host.test", 1, "ping", "Linux")
    assert result.status == "error"
    assert child.killed and child.reaped


@pytest.mark.skipif(os.name == "nt", reason="Executable script fixture uses a Unix shebang")
def test_real_subprocess_uses_c_locale_and_literal_host(tmp_path):
    import sys

    executable = tmp_path / "fixture-ping"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert os.environ['LC_ALL'] == 'C'\n"
        "assert sys.argv[-1] == 'host;echo-injected'\n"
        "print('64 bytes from host: icmp_seq=1 ttl=64 time=2.5 ms')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    result = probe.run_probe("host;echo-injected", 1, str(executable), "Linux")
    assert result.status == "reply"
    assert result.latency_ms == 2.5


@pytest.mark.skipif(os.name == "nt", reason="Executable script fixture uses a Unix shebang")
def test_real_hung_subprocess_is_killed_and_reaped(tmp_path):
    import sys

    executable = tmp_path / "hung-ping"
    pid_file = tmp_path / "pid"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    result = probe.run_probe("host.test", 0.1, str(executable), "Darwin")
    assert result.status == "error"
    assert "deadline" in result.error.lower()
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
