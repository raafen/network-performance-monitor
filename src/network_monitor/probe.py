"""One real operating-system ICMP request, with a bounded subprocess lifetime."""

from __future__ import annotations

import math
import os
import platform
import re
import subprocess
from datetime import datetime, timezone
from typing import Literal

from .models import ProbeResult

# Allow process startup/teardown beyond the timeout represented by the OS flags.
# Linux rounds that timeout to whole seconds; Darwin and Windows to milliseconds.
STARTUP_GRACE_SECONDS = 1.0
# Common signed 32-bit millisecond limit of the supported ping utilities.
MAX_TIMEOUT_SECONDS = 2_147_483.647

_LATENCY = re.compile(r"\btime\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*ms\b", re.I)
_REPLY = re.compile(r"\b(?:bytes from|reply from)\b", re.I)
_EXECUTION_ERROR = re.compile(
    r"unknown host|name or service not known|temporary failure in name resolution|"
    r"cannot resolve|could not find host|bad address|no address associated|"
    r"nodename nor servname|permission denied|operation not permitted|"
    r"access denied|must be root|requires? .*(?:privilege|permission)|"
    r"general failure|transmit failed|sendto:|sendmsg:|socket:|connect:|"
    r"network is unreachable|no route to host|invalid (?:argument|option)|"
    r"illegal option|usage:|^ping(?:6)?:\s",
    re.I | re.M,
)
_NO_REPLY = re.compile(
    r"request timed out|request timeout|\b100(?:[.,]0+)?%\s*(?:packet )?loss|"
    r"\b[1-9]\d* packets? transmitted,\s*0 (?:packets? )?received",
    re.I,
)
_ICMP_FAILURE = re.compile(
    r"(?:destination\s+)?(?:host|net|network|port|protocol)\s+unreachable|"
    r"destination unreachable|time to live exceeded|ttl expired|time exceeded|"
    r"packet filtered|administratively prohibited",
    re.I,
)


def build_command(host: str, timeout: float, executable: str, system: str) -> list[str]:
    """Build exactly one request, without shell interpretation of the target."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    if timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must not exceed 2147483.647 seconds")
    if system == "Linux":
        return [executable, "-n", "-c", "1", "-W", str(math.ceil(timeout)), host]
    milliseconds = str(math.ceil(timeout * 1000))
    if system == "Darwin":
        return [executable, "-n", "-c", "1", "-W", milliseconds, host]
    if system == "Windows":
        return [executable, "-n", "1", "-w", milliseconds, host]
    raise ValueError(f"Unsupported operating system: {system}")


def parse_latency(output: str) -> tuple[float, bool] | None:
    """Read an actual reply's latency, preserving '<' rather than inventing RTT."""
    for line in output.splitlines():
        if not _REPLY.search(line):
            continue
        match = _LATENCY.search(line)
        if match:
            latency = float(match.group(2).replace(",", "."))
            if math.isfinite(latency):
                return latency, match.group(1) == "<"
    return None


def _result(
    host: str,
    status: Literal["reply", "timeout", "error"],
    *,
    latency_ms: float | None = None,
    error: str | None = None,
    latency_is_upper_bound: bool = False,
) -> ProbeResult:
    return ProbeResult(
        datetime.now(timezone.utc),
        host,
        status,
        latency_ms,
        error,
        latency_is_upper_bound,
    )


def _execution_error(host: str, returncode: int, output: str) -> ProbeResult:
    detail = " ".join(output.split())[:500]
    message = f"ping exited with status {returncode}"
    if detail:
        message += f": {detail}"
    else:
        message += ": no recognizable reply or timeout output"
    return _result(host, "error", error=message)


def classify_output(host: str, returncode: int, output: str) -> ProbeResult:
    """Use packet evidence, not overlapping platform exit codes, for loss."""
    if returncode < 0:
        return _execution_error(host, returncode, output)
    latency = parse_latency(output)
    if latency is not None:
        return _result(
            host,
            "reply",
            latency_ms=latency[0],
            latency_is_upper_bound=latency[1],
        )
    # A failed send may also print '100% packet loss'; it is not a sent probe.
    if _EXECUTION_ERROR.search(output):
        return _execution_error(host, returncode, output)
    if _NO_REPLY.search(output):
        return _result(host, "timeout")
    for line in output.splitlines():
        if re.search(r"\bfrom\b", line, re.I) and _ICMP_FAILURE.search(line):
            return _result(host, "timeout")
    return _execution_error(host, returncode, output)


def _kill_and_reap(process: subprocess.Popen[str]) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    finally:
        process.communicate()


def run_probe(
    host: str,
    timeout: float,
    executable: str,
    system: str | None = None,
) -> ProbeResult:
    """Run one ping; completion is timestamped in UTC.

    The outer deadline is the OS-rounded timeout plus startup grace and, on
    macOS, its final one-second packet-scheduling wait. Deadline exhaustion is
    an execution failure, never evidence of packet loss.
    KeyboardInterrupt always kills and reaps the running child before escaping.
    """
    system = platform.system() if system is None else system
    try:
        command = build_command(host, timeout, executable, system)
    except ValueError as exc:
        return _result(host, "error", error=str(exc))
    os_timeout = math.ceil(timeout) if system == "Linux" else math.ceil(timeout * 1000) / 1000
    deadline = os_timeout + STARTUP_GRACE_SECONDS + (1.0 if system == "Darwin" else 0.0)
    environment = os.environ.copy()
    if system != "Windows":
        environment["LC_ALL"] = "C"
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            shell=False,
        )
    except OSError as exc:
        return _result(host, "error", error=f"Could not execute ping: {exc}")
    try:
        output, _ = process.communicate(timeout=deadline)
    except KeyboardInterrupt:
        _kill_and_reap(process)
        raise
    except subprocess.TimeoutExpired:
        _kill_and_reap(process)
        return _result(
            host,
            "error",
            error=f"Ping execution exceeded its {deadline:g}s deadline",
        )
    except OSError as exc:
        _kill_and_reap(process)
        return _result(host, "error", error=f"Could not read ping output: {exc}")
    return classify_output(host, process.returncode, output)
