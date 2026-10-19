# Network Performance Monitor

A Python CLI for real ICMP latency and packet-loss monitoring, with a live Rich dashboard and append-only CSV / JSON Lines logs. Each measurement comes from the operating system's `ping` utility; there are no simulated samples or overlapping probes.

## Features

- Animated in-flight spinner, elapsed-time header, and connection status.
- Host, latest / average latency, cumulative packet loss, and probe count in a live table.
- Scrolling sparkline of the last 60 attempts, with failures distinguished from measured latency.
- Configurable green / yellow / red latency thresholds; clearly separate timeouts and execution errors.
- Minimum, average, and maximum latency, including a clean summary after Ctrl+C.
- Narrow-terminal layout and automatic non-animated output when redirected.
- Timestamped CSV and JSON Lines logging, flushed after every completed probe.
- Sequential subprocess execution without a shell; bounded chart history and constant-space cumulative statistics.

## Installation

Requires **Python 3.10+** and an OS-provided `ping` executable on `PATH`. Rich is the only runtime Python dependency.

```sh
git clone https://github.com/raafen/network-performance-monitor.git
cd network-performance-monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows, use `py -m venv .venv`, then `.venv\Scripts\Activate.ps1` in PowerShell. If activation is restricted, run `.venv\Scripts\python.exe` directly.

## Usage

```sh
# Continuous monitoring; Ctrl+C prints the summary and exits
network-monitor example.com

# Finite loopback run
network-monitor 127.0.0.1 --count 10 --interval 0.5 --timeout 1

# Custom latency bands and simultaneous logging
network-monitor 1.1.1.1 --warning-ms 80 --critical-ms 200 \
  --csv results.csv --jsonl results.jsonl

# Stable plain output, including when piping or redirecting
network-monitor example.com --count 5 --no-animation
network-monitor example.com --count 5 > monitor.txt

# The module entry point works too
python -m network_monitor --help
```

| Argument | Default | Meaning |
| --- | --- | --- |
| `host` | `1.1.1.1` | Hostname or IP address; one target per process |
| `--interval SECONDS` | `1` | Positive, finite minimum time between probe starts |
| `--timeout SECONDS` | `1` | Positive, finite OS ping reply timeout; platform resolution applies |
| `--count N` | Continuous | Positive number of completed probe attempts |
| `--warning-ms MS` | `100` | Yellow begins here; nonnegative and below critical |
| `--critical-ms MS` | `250` | Red begins here; finite and above warning |
| `--csv PATH` | Disabled | Append CSV results |
| `--jsonl PATH` | Disabled | Append one JSON object per completed attempt |
| `--no-animation` | Disabled | Print plain results instead of a refreshing dashboard |

The interval is **start-to-start**, not an extra delay after every probe. If a probe takes longer than the interval, the next starts after it finishes. There is at most one ping subprocess at a time. Rich refreshes independently while that subprocess waits; the display never generates measurements.

Latency below the warning threshold is green, between warning and critical is yellow, and at or above critical is red. Color is not the only signal: timeout and error outcomes are labeled. The chart has at most 60 recent attempts; cumulative statistics cover the entire session, not only the visible window. The chart is relative to the visible latency range, not a fixed millisecond axis.

### Measurement semantics

- **Reply**: a latency parsed from a received ICMP reply.
- **Timeout**: ping establishes that the request had no reply (including an ICMP destination-unreachable outcome). It contributes to packet loss, but not latency statistics.
- **Execution error**: DNS failure, missing executable, permissions, unsupported output, or an exhausted process deadline. It increments errors, not packet loss.
- **Packet loss**: `timeouts / (replies + timeouts) × 100`. No valid ICMP outcomes means loss is unavailable, not 0% or 100%. Probe count includes execution errors.
- **Latency statistics**: only replies contribute to minimum / average / maximum. Windows may report `time<1ms`; this is retained as an upper bound, not an invented exact sub-millisecond measurement. Aggregate statistics are marked as bounds when any contributing sample is a bound.
- **Interruption**: an interrupted in-flight probe is killed and reaped, not logged as a completed or failed measurement. Previously completed results remain available.

Exit codes: `0` for a completed run without execution errors (packet loss alone does not change the exit code), `1` for execution or logging failures, `2` for invalid CLI arguments, and `130` for Ctrl+C.

## Log formats

Both formats append and flush after each completed attempt, including timeouts and execution errors. Timestamps are timezone-aware UTC ISO 8601 at probe completion. Flushing is not an `fsync` durability guarantee against power loss. Parent directories must already exist. Logs may reveal your target names/IPs and are ignored by Git by default.

CSV columns, in order:

```text
timestamp,host,status,latency_ms,latency_is_upper_bound,error
```

`status` is `reply`, `timeout`, or `error`. Missing latency and error fields are empty in CSV and `null` in JSON. The bound flag is a boolean. Existing CSV files must have the expected header; CSV and JSONL paths must not point to the same file.

Illustrative JSON Lines schema (not a captured measurement):

```json
{"timestamp":"2026-10-19T12:00:00+00:00","host":"127.0.0.1","status":"reply","latency_ms":0.125,"latency_is_upper_bound":false,"error":null}
{"timestamp":"2026-10-19T12:00:01+00:00","host":"example.com","status":"timeout","latency_ms":null,"latency_is_upper_bound":false,"error":null}
{"timestamp":"2026-10-19T12:00:02+00:00","host":"missing.invalid","status":"error","latency_ms":null,"latency_is_upper_bound":false,"error":"DNS resolution failed"}
```

Logging failures stop monitoring with a diagnostic rather than silently dropping data. Writes to two different files are not transactional: if the second write fails, the first may already contain the result. Do not have multiple monitor processes append to the same log files.

## Platforms and troubleshooting

| Platform | Ping behavior and limitations |
| --- | --- |
| Linux | Uses `ping -c 1` and an integer-second reply timeout, rounded up. Minimal containers may need `iputils-ping` installed and permission to send ICMP. |
| macOS | Uses `ping -c 1` with a millisecond reply timeout. The default system `ping` primarily targets IPv4; IPv6-only targets may require a separate `ping6` utility, which this tool does not select. |
| Windows | Uses `ping -n 1 -w` with a millisecond timeout. English reply/error output is supported; localized output that cannot be recognized is reported as an execution error rather than fabricated packet loss. Very fast replies may be upper bounds (`<1 ms`). |

The process deadline allows the effective OS reply timeout **plus one second** for process startup and cleanup. macOS gets **one additional second** for its final packet-scheduling wait before printing loss statistics. Linux rounds the configured timeout up to whole seconds; macOS and Windows round up to milliseconds. The maximum accepted timeout is 2,147,483.647 seconds (the common signed 32-bit millisecond limit). If DNS or the utility hangs beyond the deadline, the process is killed and the attempt is an **execution error**: absence of a completed ping does not establish packet loss. Scheduling and OS timer precision may add small overhead.

- **`ping` not found:** install the OS utility. Debian/Ubuntu: `sudo apt-get install iputils-ping`. Confirm `ping 127.0.0.1` works in the same environment.
- **Permission denied / operation not permitted:** check container capabilities or your administrator's ICMP policy. This program does not elevate privileges.
- **100% loss:** ICMP may be blocked or rate-limited. Ping measures ICMP reachability, not HTTP availability, throughput, bandwidth, or application health.
- **DNS failure:** check the target and local resolver. An invalid hostname is an error, never counted as packet loss.
- **No animation:** redirected/non-terminal output and `--no-animation` intentionally use plain records. Interactive terminals must support Rich's cursor control. Narrow terminals use a compact layout; extremely small widths necessarily truncate labels. UTF-8 enables the full sparkline and spinner glyphs. Legacy output encodings use ASCII chart/spinner glyphs and `<=` / `>=` bounds; unsupported characters in target names or diagnostics are escaped without changing logged measurements.
- **Log path failure:** create its parent directory and verify write permissions. Do not reuse a CSV file with a different schema.

## Development and verification

```sh
python -m pip install -e '.[test]'
python -m pytest                       # unit tests + real OS loopback probe
python -m pytest -m 'not integration'  # unit tests only
python -m pytest -m integration        # real ICMP loopback smoke test
network-monitor 127.0.0.1 --count 3 --interval 0.1 --no-animation
```

The integration test requires a working system ping and ICMP permission. GitHub Actions runs Python 3.10 and 3.14 on Linux, macOS, and Windows, plus an installed-CLI loopback run. Workflow configuration is not evidence that hosted runs have passed; consult the repository's Actions tab for actual results.

### Structure

- `models.py`: typed results, bounded history, cumulative statistics.
- `probe.py`: platform commands, subprocess lifecycle, real ping output classification.
- `logging.py`: incremental CSV / JSON Lines output.
- `display.py`: responsive Rich presentation and plain output.
- `cli.py`: validated arguments, sequential scheduling, shutdown.

This project was developed with AI coding assistance and verified using the commands reported with the delivery. No Cursor usage is claimed. Example log values above are illustrative; actual measurements always come from OS ping.

## License

[MIT](LICENSE).
