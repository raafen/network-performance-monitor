import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from network_monitor.logging import FIELDS, ResultLogger
from network_monitor.models import ProbeResult


def sample(status="reply"):
    return ProbeResult(
        datetime(2026, 1, 2, 4, 30, tzinfo=timezone(timedelta(hours=2))),
        'host,"quoted".test',
        status,
        1.0 if status == "reply" else None,
        'failure, with "quotes"\nand newline' if status == "error" else None,
        status == "reply",
    )


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_each_result_is_visible_before_context_closes(tmp_path):
    csv_path, jsonl_path = tmp_path / "results.csv", tmp_path / "results.jsonl"
    with ResultLogger(csv_path, jsonl_path) as logger:
        logger.write(sample())
        csv_rows = read_csv(csv_path)
        json_rows = read_jsonl(jsonl_path)
        assert len(csv_rows) == len(json_rows) == 1
        assert tuple(csv_rows[0]) == tuple(json_rows[0]) == FIELDS
        assert csv_rows[0]["host"] == json_rows[0]["host"] == sample().host
        assert json_rows[0]["timestamp"] == "2026-01-02T02:30:00+00:00"
        assert json_rows[0]["latency_ms"] == 1.0
        assert json_rows[0]["latency_is_upper_bound"] is True
        assert json_rows[0]["error"] is None
        logger.write(sample("timeout"))
        assert len(read_csv(csv_path)) == len(read_jsonl(jsonl_path)) == 2
        assert read_csv(csv_path)[1]["latency_ms"] == ""
        assert read_jsonl(jsonl_path)[1]["latency_ms"] is None


def test_reopening_appends_without_duplicate_header_and_quotes_errors(tmp_path):
    csv_path, jsonl_path = tmp_path / "results.csv", tmp_path / "results.jsonl"
    for status in ("reply", "error"):
        with ResultLogger(csv_path, jsonl_path) as logger:
            logger.write(sample(status))
    csv_rows = read_csv(csv_path)
    json_rows = read_jsonl(jsonl_path)
    assert len(csv_rows) == len(json_rows) == 2
    assert csv_rows[1]["error"] == json_rows[1]["error"] == sample("error").error
    assert [row["status"] for row in csv_rows] == ["reply", "error"]


def test_existing_unterminated_records_are_not_glued_to_new_record(tmp_path):
    csv_path, jsonl_path = tmp_path / "results.csv", tmp_path / "results.jsonl"
    csv_path.write_text(",".join(FIELDS), encoding="utf-8")
    jsonl_path.write_text('{"existing":true}', encoding="utf-8")
    with ResultLogger(csv_path, jsonl_path) as logger:
        logger.write(sample())
    assert len(read_csv(csv_path)) == 1
    assert read_jsonl(jsonl_path)[0] == {"existing": True}
    assert read_jsonl(jsonl_path)[1]["status"] == "reply"


@pytest.mark.parametrize("header", ["wrong,header\n", "\n", ",".join(reversed(FIELDS)) + "\n"])
def test_mismatched_csv_header_is_rejected_without_appending(tmp_path, header):
    path = tmp_path / "results.csv"
    path.write_text(header, encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        with ResultLogger(csv_path=path):
            pass
    assert path.read_text(encoding="utf-8") == header


def test_same_target_paths_are_rejected_before_any_header_is_written(tmp_path):
    path = tmp_path / "results"
    with pytest.raises(ValueError, match="different files"):
        with ResultLogger(path, path.parent / "." / path.name):
            pass
    assert not path.exists()


@pytest.mark.parametrize("link_type", ["hard", "symbolic"])
def test_same_physical_file_via_links_is_rejected(tmp_path, link_type):
    path, alias = tmp_path / "results", tmp_path / "alias"
    path.touch()
    try:
        if link_type == "hard":
            os.link(path, alias)
        else:
            alias.symlink_to(path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Filesystem does not support requested link: {exc}")
    with pytest.raises(ValueError, match="different files"):
        with ResultLogger(path, alias):
            pass
    assert path.read_bytes() == b""


def test_partial_initialization_closes_already_opened_file(tmp_path, monkeypatch):
    csv_path = tmp_path / "results.csv"
    jsonl_path = tmp_path / "missing-parent" / "results.jsonl"
    opened = []
    original_open = Path.open

    def track_open(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(Path, "open", track_open)
    with pytest.raises(OSError):
        with ResultLogger(csv_path, jsonl_path):
            pass
    assert opened
    assert all(file.closed for file in opened)


def test_header_validation_failure_closes_both_files(tmp_path, monkeypatch):
    csv_path, jsonl_path = tmp_path / "bad.csv", tmp_path / "results.jsonl"
    csv_path.write_text("bad,header\n", encoding="utf-8")
    opened = []
    original_open = Path.open

    def track_open(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        opened.append(file)
        return file

    monkeypatch.setattr(Path, "open", track_open)
    with pytest.raises(ValueError):
        with ResultLogger(csv_path, jsonl_path):
            pass
    assert len(opened) == 2
    assert all(file.closed for file in opened)


def test_write_failure_propagates_and_closes_other_sink(tmp_path, monkeypatch):
    jsonl_path = tmp_path / "results.jsonl"
    csv_path = tmp_path / "results.csv"
    opened = []
    original_open = Path.open

    class FailingFile:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def __enter__(self):
            self.file.__enter__()
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def write(self, text):
            raise OSError("disk full")

    def failing_open(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        opened.append(file)
        return FailingFile(file) if path == jsonl_path else file

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="disk full"):
        with ResultLogger(csv_path, jsonl_path) as logger:
            logger.write(sample())
    assert all(file.closed for file in opened)


def test_no_sink_context_and_closed_context_behavior():
    logger = ResultLogger()
    with pytest.raises(RuntimeError):
        logger.write(sample())
    with logger:
        logger.write(sample())
        with pytest.raises(RuntimeError):
            logger.__enter__()
    with pytest.raises(RuntimeError):
        logger.write(sample())


def test_naive_timestamps_are_not_assumed_to_be_local_time(tmp_path):
    result = ProbeResult(datetime(2026, 1, 1), "host.test", "timeout")
    with ResultLogger(jsonl_path=tmp_path / "results.jsonl") as logger:
        with pytest.raises(ValueError, match="timezone-aware"):
            logger.write(result)
