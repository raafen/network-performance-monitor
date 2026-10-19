"""Append-only CSV and JSON Lines sinks, flushed after every result."""

from __future__ import annotations

import csv
import json
import os
from contextlib import ExitStack
from datetime import timezone
from pathlib import Path
from types import TracebackType
from typing import TextIO

from .models import ProbeResult

FIELDS = (
    "timestamp",
    "host",
    "status",
    "latency_ms",
    "latency_is_upper_bound",
    "error",
)


def _separate_existing_records(file: TextIO) -> None:
    """Keep an unterminated final record separate without loading the log."""
    file.seek(0, os.SEEK_END)
    if file.tell() == 0:
        return
    file.buffer.seek(-1, os.SEEK_END)
    last_byte = file.buffer.read(1)
    file.seek(0, os.SEEK_END)
    if last_byte not in (b"\n", b"\r"):
        file.write("\n")


class ResultLogger:
    def __init__(
        self, csv_path: Path | None = None, jsonl_path: Path | None = None
    ) -> None:
        self.csv_path = Path(csv_path) if csv_path is not None else None
        self.jsonl_path = Path(jsonl_path) if jsonl_path is not None else None
        self._stack: ExitStack | None = None
        self._csv_file: TextIO | None = None
        self._jsonl_file: TextIO | None = None
        self._writer: csv.DictWriter | None = None

    def __enter__(self) -> ResultLogger:
        if self._stack is not None:
            raise RuntimeError("Result logger is already open")
        if (
            self.csv_path is not None
            and self.jsonl_path is not None
            and self.csv_path.resolve() == self.jsonl_path.resolve()
        ):
            raise ValueError("CSV and JSONL must use different files")
        with ExitStack() as stack:
            csv_file = (
                stack.enter_context(self.csv_path.open("a+", encoding="utf-8", newline=""))
                if self.csv_path is not None
                else None
            )
            jsonl_file = (
                stack.enter_context(self.jsonl_path.open("a+", encoding="utf-8", newline=""))
                if self.jsonl_path is not None
                else None
            )
            if csv_file is not None and jsonl_file is not None:
                csv_stat = os.fstat(csv_file.fileno())
                jsonl_stat = os.fstat(jsonl_file.fileno())
                if (csv_stat.st_dev, csv_stat.st_ino) == (
                    jsonl_stat.st_dev,
                    jsonl_stat.st_ino,
                ):
                    raise ValueError("CSV and JSONL must use different files")
            writer = None
            if csv_file is not None:
                csv_file.seek(0, os.SEEK_END)
                empty = csv_file.tell() == 0
                if not empty:
                    csv_file.seek(0)
                    try:
                        header = next(csv.reader(csv_file))
                    except (StopIteration, csv.Error) as exc:
                        raise ValueError("CSV log has an invalid header") from exc
                    if header != list(FIELDS):
                        raise ValueError("CSV log header does not match the result schema")
                csv_file.seek(0, os.SEEK_END)
                writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
                if empty:
                    writer.writeheader()
                    csv_file.flush()
                else:
                    _separate_existing_records(csv_file)
            if jsonl_file is not None:
                _separate_existing_records(jsonl_file)
            self._csv_file = csv_file
            self._jsonl_file = jsonl_file
            self._writer = writer
            self._stack = stack.pop_all()
        return self

    def write(self, result: ProbeResult) -> None:
        if self._stack is None:
            raise RuntimeError("Result logger must be used as a context manager")
        if result.timestamp.utcoffset() is None:
            raise ValueError("Result timestamp must be timezone-aware")
        row = {
            "timestamp": result.timestamp.astimezone(timezone.utc).isoformat(),
            "host": result.host,
            "status": result.status,
            "latency_ms": result.latency_ms,
            "latency_is_upper_bound": result.latency_is_upper_bound,
            "error": result.error,
        }
        if self._writer is not None and self._csv_file is not None:
            self._writer.writerow(row)
            self._csv_file.flush()
        if self._jsonl_file is not None:
            self._jsonl_file.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            self._jsonl_file.flush()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stack = self._stack
        self._stack = None
        self._writer = None
        self._csv_file = None
        self._jsonl_file = None
        if stack is not None:
            stack.__exit__(exc_type, exc_value, traceback)
