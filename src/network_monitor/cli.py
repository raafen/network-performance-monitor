"""Command-line parsing and the synchronous, non-overlapping probe loop."""

from __future__ import annotations

import argparse
import math
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.text import Text

from .display import MonitorDisplay
from .logging import ResultLogger
from .models import Statistics
from .probe import MAX_TIMEOUT_SECONDS, run_probe


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a finite positive number") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def _timeout(value: str) -> float:
    number = _positive_float(value)
    if number > MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(f"must not exceed {MAX_TIMEOUT_SECONDS:g} seconds")
    return number


def _nonnegative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number") from error
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _host(value: str) -> str:
    if (
        not value
        or len(value) > 253
        or value.startswith("-")
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
    ):
        raise argparse.ArgumentTypeError(
            "host must be 1–253 characters, must not start with '-', and must not contain whitespace or control characters"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="network-monitor",
        description="Monitor real ICMP latency and packet loss with the system ping utility.",
    )
    parser.add_argument("host", nargs="?", default="1.1.1.1", type=_host, help="hostname or IP address (default: %(default)s)")
    parser.add_argument("--interval", type=_positive_float, default=1.0, metavar="SECONDS", help="minimum probe start-to-start interval (default: %(default)s)")
    parser.add_argument("--timeout", type=_timeout, default=1.0, metavar="SECONDS", help="per-probe reply timeout (default: %(default)s)")
    parser.add_argument("--count", type=_positive_int, metavar="N", help="stop after N completed attempts (default: continuous)")
    parser.add_argument("--warning-ms", type=_nonnegative_float, default=100.0, metavar="MS", help="yellow latency threshold (default: %(default)s)")
    parser.add_argument("--critical-ms", type=_nonnegative_float, default=250.0, metavar="MS", help="red latency threshold (default: %(default)s)")
    parser.add_argument("--csv", type=Path, metavar="PATH", help="append results to a CSV file")
    parser.add_argument("--jsonl", type=Path, metavar="PATH", help="append results to a JSON Lines file")
    parser.add_argument("--no-animation", action="store_true", help="print one plain line per result instead of a live display")
    return parser


def _monitor(args: argparse.Namespace, console: Console, error_console: Console, executable: str) -> int:
    statistics = Statistics(args.host)
    display = MonitorDisplay(
        statistics,
        console,
        args.warning_ms,
        args.critical_ms,
        animated=console.is_terminal and not args.no_animation,
    )
    interrupted = False
    failed = False
    try:
        # Open logs before the live screen; nested contexts close everything on interruption or I/O failure.
        with ResultLogger(csv_path=args.csv, jsonl_path=args.jsonl) as logger:
            with display:
                while args.count is None or statistics.probes < args.count:
                    display.set_probing(True)
                    started = time.monotonic()
                    try:
                        result = run_probe(args.host, args.timeout, executable)
                    finally:
                        display.set_probing(False)
                    statistics.record(result)
                    display.record(result)
                    logger.write(result)
                    if args.count is not None and statistics.probes >= args.count:
                        break
                    remaining = started + args.interval - time.monotonic()
                    while remaining > 0:
                        # Bound individual sleeps so every finite interval remains interruptible
                        # without overflowing platform-specific sleep implementations.
                        time.sleep(min(remaining, 86400.0))
                        remaining = started + args.interval - time.monotonic()
    except KeyboardInterrupt:
        interrupted = True
    except (OSError, ValueError) as error:
        # ResultLogger rejects incompatible destinations and propagates all write/flush failures.
        failed = True
        error_console.print(Text(f"Error: {error}"))
    display.summary(interrupted=interrupted)
    if interrupted:
        return 130
    return 1 if failed or statistics.errors else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.warning_ms >= args.critical_ms:
        parser.error("--warning-ms must be less than --critical-ms")
    error_console = Console(stderr=True, highlight=False)
    executable = shutil.which("ping")
    if executable is None:
        error_console.print(Text("Error: system ping executable was not found on PATH."))
        return 1
    return _monitor(args, Console(highlight=False), error_console, executable)
