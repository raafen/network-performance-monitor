import math
import platform
import shutil
from datetime import datetime, timezone

import pytest

from network_monitor.models import Statistics
from network_monitor.probe import run_probe


@pytest.mark.integration
def test_real_loopback_icmp_reply():
    if platform.system() not in {"Linux", "Darwin", "Windows"}:
        pytest.skip("The operating system is not supported by the ping backend")
    executable = shutil.which("ping")
    if executable is None:
        pytest.skip("The operating system ping utility is not installed")
    before = datetime.now(timezone.utc)
    result = run_probe("127.0.0.1", timeout=2.0, executable=executable)
    assert result.status == "reply", result.error
    assert result.latency_ms is not None
    assert math.isfinite(result.latency_ms)
    assert 0 <= result.latency_ms < 2_000
    assert before <= result.timestamp <= datetime.now(timezone.utc)
    assert result.timestamp.utcoffset().total_seconds() == 0
    stats = Statistics("127.0.0.1")
    stats.record(result)
    assert stats.probes == stats.replies == 1
    assert stats.timeouts == stats.errors == 0
    assert stats.loss_percent == 0.0
    assert stats.average_ms == result.latency_ms
