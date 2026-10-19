"""Probe results and bounded, cumulative monitoring statistics."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ProbeResult:
    timestamp: datetime
    host: str
    status: Literal["reply", "timeout", "error"]
    latency_ms: float | None = None
    error: str | None = None
    latency_is_upper_bound: bool = False


@dataclass
class Statistics:
    host: str
    probes: int = field(default=0, init=False)
    replies: int = field(default=0, init=False)
    timeouts: int = field(default=0, init=False)
    errors: int = field(default=0, init=False)
    latest: ProbeResult | None = field(default=None, init=False)
    history: deque[ProbeResult] = field(
        default_factory=lambda: deque(maxlen=60), init=False
    )
    _latency_total: float = field(default=0.0, init=False, repr=False)
    _latency_count: int = field(default=0, init=False, repr=False)
    _minimum_ms: float | None = field(default=None, init=False, repr=False)
    _maximum_ms: float | None = field(default=None, init=False, repr=False)
    _has_upper_bounds: bool = field(default=False, init=False, repr=False)

    def record(self, result: ProbeResult) -> None:
        self.probes += 1
        self.latest = result
        self.history.append(result)
        if result.status == "timeout":
            self.timeouts += 1
        elif result.status == "error":
            self.errors += 1
        else:
            self.replies += 1
            if result.latency_ms is not None:
                latency = result.latency_ms
                self._latency_total += latency
                self._latency_count += 1
                self._minimum_ms = (
                    latency if self._minimum_ms is None else min(self._minimum_ms, latency)
                )
                self._maximum_ms = (
                    latency if self._maximum_ms is None else max(self._maximum_ms, latency)
                )
                self._has_upper_bounds |= result.latency_is_upper_bound

    @property
    def loss_percent(self) -> float | None:
        delivered = self.replies + self.timeouts
        return 100.0 * self.timeouts / delivered if delivered else None

    @property
    def average_ms(self) -> float | None:
        return self._latency_total / self._latency_count if self._latency_count else None

    @property
    def minimum_ms(self) -> float | None:
        return self._minimum_ms

    @property
    def maximum_ms(self) -> float | None:
        return self._maximum_ms

    @property
    def has_upper_bounds(self) -> bool:
        return self._has_upper_bounds
