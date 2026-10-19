"""Rich renderables and plain output for real probe results."""

from __future__ import annotations

import math
import time
from datetime import timezone

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .models import ProbeResult, Statistics


def latency_text(value: float | None, upper_bound: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "<" if upper_bound else ""
    return f"{prefix}{value:g} ms"


def _message(value: str) -> str:
    """Keep external diagnostics on one printable line without interpreting markup."""
    return "".join(char if char.isprintable() else "?" for char in " ".join(value.split()))


class MonitorDisplay:
    """Render shared statistics; Live's refresh thread animates during blocking probes."""

    def __init__(
        self,
        statistics: Statistics,
        console: Console,
        warning_ms: float,
        critical_ms: float,
        *,
        animated: bool,
    ) -> None:
        self.statistics = statistics
        self.console = console
        self.warning_ms = warning_ms
        self.critical_ms = critical_ms
        self.animated = animated
        self.started = time.monotonic()
        self.probing = False
        self.spinner = Spinner("dots", style="cyan")
        self.live: Live | None = None

    def __enter__(self) -> MonitorDisplay:
        if self.animated:
            self.live = Live(
                self,
                console=self.console,
                refresh_per_second=8,
                auto_refresh=True,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self.live.start(refresh=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None

    def set_probing(self, probing: bool) -> None:
        self.probing = probing
        if self.live is not None:
            self.live.refresh()

    def record(self, result: ProbeResult) -> None:
        """Publish an already-recorded result; never invent or record measurements."""
        if self.animated:
            if self.live is not None:
                self.live.refresh()
            return
        timestamp = result.timestamp.astimezone(timezone.utc).isoformat()
        status = {
            "reply": f"reply {latency_text(result.latency_ms, result.latency_is_upper_bound)}",
            "timeout": "timeout (no reply)",
            "error": "execution error",
        }[result.status]
        detail = f" — {_message(result.error)}" if result.error else ""
        self.console.print(Text(f"{timestamp} {result.host}  {status}{detail}"))

    def latency_style(self, value: float | None) -> str:
        if value is None:
            return "dim"
        if value < self.warning_ms:
            return "green"
        if value < self.critical_ms:
            return "yellow"
        return "red"

    def _latest(self) -> Text:
        latest = self.statistics.latest
        if latest is None:
            return Text("Waiting", style="dim")
        if latest.status == "timeout":
            return Text("Timeout", style="yellow")
        if latest.status == "error":
            return Text("Execution error", style="red")
        return Text(
            latency_text(latest.latency_ms, latest.latency_is_upper_bound),
            style=self.latency_style(latest.latency_ms),
        )

    def _aggregate(self, value: float | None) -> str:
        if value is None:
            return "—"
        # An upper-bound sample makes an exact aggregate claim unjustified.
        prefix = "≤" if self.statistics.has_upper_bounds else ""
        return f"{prefix}{value:g} ms"

    def _loss(self) -> str:
        value = self.statistics.loss_percent
        return "—" if value is None else f"{value:.1f}%"

    def _metrics(self, width: int) -> Table:
        stats = self.statistics
        if width >= 92:
            table = Table(expand=True, show_edge=False, pad_edge=False)
            for name in ("Host", "Latest", "Average", "Loss", "Probes", "Replies", "Timeouts", "Errors"):
                table.add_column(name)
            table.add_row(
                Text(stats.host), self._latest(), self._aggregate(stats.average_ms), self._loss(),
                str(stats.probes), str(stats.replies), str(stats.timeouts), str(stats.errors),
            )
            return table
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        table.add_row("Host", Text(stats.host))
        table.add_row("Latest", self._latest())
        table.add_row("Average", self._aggregate(stats.average_ms))
        table.add_row("Loss", self._loss())
        table.add_row("Attempts", str(stats.probes))
        table.add_row("Reply / timeout", f"{stats.replies} / {stats.timeouts}")
        table.add_row("Errors", str(stats.errors))
        return table

    def _chart(self, width: int) -> Group:
        history = tuple(self.statistics.history)
        values = [item.latency_ms for item in history if item.status == "reply" and item.latency_ms is not None]
        peak = max(values, default=0.0)
        scale = peak if peak > 0 else 1.0
        glyphs = "▁▂▃▄▅▆▇█"
        chart = Text()
        # Wrap rather than dropping samples on narrow terminals: every stored attempt is visible.
        columns = max(1, width - 4)
        for index, result in enumerate(history):
            if index and index % columns == 0:
                chart.append("\n")
            if result.status != "reply" or result.latency_ms is None:
                chart.append(" ")
            else:
                level = min(7, max(0, math.floor(result.latency_ms / scale * 7)))
                chart.append(glyphs[level], style=self.latency_style(result.latency_ms))
        if not history:
            chart.append("Waiting for first probe", style="dim")
        scale_label = f"0–{peak:g} ms" if values else "No latency samples"
        legend = Text(f"{scale_label} · gaps = no reply/error", style="dim")
        return Group(Panel(chart, title="Last 60 attempts", padding=(0, 1)), legend)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        elapsed = max(0, int(time.monotonic() - self.started))
        minutes, seconds = divmod(elapsed, 60)
        hours, minutes = divmod(minutes, 60)
        header = Table.grid(padding=(0, 1))
        state = "Waiting" if self.statistics.latest is None else {
            "reply": "Connected", "timeout": "No reply", "error": "Probe error"
        }[self.statistics.latest.status]
        duration = f"{hours:02}:{minutes:02}:{seconds:02}"
        if self.probing:
            header.add_row(self.spinner, Text(f"Probing · {state} · {duration}", style="bold"))
        else:
            header.add_row(Text(f"{state} · {duration}", style="bold"))
        yield header
        yield self._metrics(options.max_width)
        latest = self.statistics.latest
        if latest is not None and latest.error:
            yield Text(_message(latest.error), style="red" if latest.status == "error" else "yellow")
        yield self._chart(options.max_width)
        yield Text(
            f"Green <{self.warning_ms:g} · yellow <{self.critical_ms:g} · red ≥{self.critical_ms:g} ms",
            style="dim",
        )
        yield Text(
            f"Min / avg / max: {self._aggregate(self.statistics.minimum_ms)} / "
            f"{self._aggregate(self.statistics.average_ms)} / {self._aggregate(self.statistics.maximum_ms)}"
        )
        if self.statistics.has_upper_bounds:
            yield Text("≤ aggregates include upper-bound replies", style="dim")

    def summary(self, *, interrupted: bool = False) -> None:
        stats = self.statistics
        title = "Interrupted" if interrupted else "Summary"
        elapsed = max(0.0, time.monotonic() - self.started)
        lines = [
            f"{title}: {stats.host} ({elapsed:.1f}s)",
            f"Attempts: {stats.probes}; replies: {stats.replies}; timeouts: {stats.timeouts}; errors: {stats.errors}",
            f"Packet loss: {self._loss()} (execution errors excluded)",
            f"Min / avg / max: {self._aggregate(stats.minimum_ms)} / "
            f"{self._aggregate(stats.average_ms)} / {self._aggregate(stats.maximum_ms)}",
        ]
        if stats.has_upper_bounds:
            lines.append("≤ aggregates include upper-bound replies")
        self.console.print(Text("\n".join(lines)))
