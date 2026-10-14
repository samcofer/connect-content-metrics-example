"""Core collector: true per-content CPU/memory by joining Connect's PID->content map
with OS-level /proc subtree accounting.

Importable (used by the Shiny app in app.py) and runnable as a standalone recorder:

    CONNECT_SERVER=http://localhost:3939 CONNECT_API_KEY=*** \
        python collector.py --polls 20 --interval 5

As a standalone process it works as a headless recorder suitable for a Connect scheduled
job (24/7 recording independent of anyone viewing the dashboard).

Why this exists: Connect's built-in per-job metrics reflect only the reported process,
which for renders (Jupyter/Rmd/Quarto) is the supervisor -- near-zero CPU/RSS. Summing the
process *subtree* from /proc captures the kernel/child that does the real work.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import pandas as pd
import requests

CONNECT_SERVER = os.environ.get("CONNECT_SERVER", "http://localhost:3939").rstrip("/")
CONNECT_API_KEY = os.environ.get("CONNECT_API_KEY", "")
RECORD_PATH = os.environ.get(
    "MONITOR_RECORD_PATH",
    str(Path(os.environ.get("TMPDIR", "/tmp")) / "connect_content_metrics.csv"),
)
VERIFY_TLS = os.environ.get("CONNECT_VERIFY", "true").lower() not in ("0", "false", "no")

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
NCPU = os.cpu_count() or 1

RECORD_FIELDS = [
    "timestamp", "content_guid", "content_name", "run_as", "job_key", "type",
    "n_procs", "pids", "cpu_cores", "cpu_pct_host", "rss_mb",
    "connect_cpu_current", "connect_ram_mb",
]

# Module state that must persist across polls.
_prev_cpu: Dict[int, Tuple[int, float]] = {}
history: "Deque[Tuple[dt.datetime, float, float]]" = deque(maxlen=360)  # (t, cpu_cores, rss_gb)
_record_started = False


def read_proc_table() -> Dict[int, dict]:
    """{pid: {ppid, jiffies, rss_bytes, comm}} for every readable process.

    /proc/<pid>/stat is world-readable, so this works across user-isolation boundaries.
    """
    table: Dict[int, dict] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "r") as fh:
                data = fh.read()
        except (OSError, ValueError):
            continue
        try:
            rparen = data.rfind(")")           # comm (field 2) may contain spaces/parens
            after = data[rparen + 2:].split()
            comm = data[data.find("(") + 1:rparen]
            ppid = int(after[1])               # field 4
            utime = int(after[11])             # field 14
            stime = int(after[12])             # field 15
            rss_pages = int(after[21])         # field 24
        except (ValueError, IndexError):
            continue
        table[pid] = {"ppid": ppid, "jiffies": utime + stime,
                      "rss_bytes": rss_pages * PAGE_SIZE, "comm": comm}
    return table


def children_index(table: Dict[int, dict]) -> Dict[int, List[int]]:
    kids: Dict[int, List[int]] = {}
    for pid, info in table.items():
        kids.setdefault(info["ppid"], []).append(pid)
    return kids


def subtree(root: int, kids: Dict[int, List[int]]) -> List[int]:
    out, stack, seen = [], [root], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(kids.get(pid, []))
    return out


def fetch_proc_map() -> Tuple[List[dict], Optional[str]]:
    """GET /__api__/metrics/procs -> (rows, error). Requires an admin API key."""
    if not CONNECT_API_KEY:
        return [], "CONNECT_API_KEY is not set (an admin key is required)."
    try:
        resp = requests.get(
            f"{CONNECT_SERVER}/__api__/metrics/procs",
            headers={"Authorization": f"Key {CONNECT_API_KEY}"},
            timeout=10, verify=VERIFY_TLS,
        )
        resp.raise_for_status()
        rows = resp.json()
        return (rows if isinstance(rows, list) else rows.get("results", [])), None
    except requests.RequestException as e:
        return [], f"Connect API error: {e}"


def compute_snapshot(record: bool = True) -> Tuple[pd.DataFrame, Optional[str]]:
    """One measurement cycle. Returns (per-content DataFrame, error-or-None)."""
    global _prev_cpu, _record_started

    proc_rows, err = fetch_proc_map()
    table = read_proc_table()
    kids = children_index(table)
    now_wall = time.monotonic()
    now_ts = dt.datetime.now(dt.timezone.utc)
    cur_cpu = {pid: (info["jiffies"], now_wall) for pid, info in table.items()}

    def pid_cpu_cores(pid: int) -> float:
        if pid not in table or pid not in _prev_cpu:
            return 0.0
        prev_j, prev_w = _prev_cpu[pid]
        dj = table[pid]["jiffies"] - prev_j
        dw = now_wall - prev_w
        return (dj / CLK_TCK) / dw if dj >= 0 and dw > 0 else 0.0

    records: List[dict] = []
    for row in proc_rows:
        try:
            root = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            root = 0
        if root <= 0 or root not in table:
            continue  # exited, or off-host execution with no local pid
        tree = subtree(root, kids)
        cpu_cores = sum(pid_cpu_cores(p) for p in tree)
        rss_bytes = sum(table[p]["rss_bytes"] for p in tree if p in table)
        connect_ram = row.get("ram")
        records.append({
            "timestamp": now_ts.isoformat(timespec="seconds"),
            "content_guid": row.get("app_guid"),
            "content_name": row.get("app_name") or "(unknown)",
            "run_as": row.get("app_run_as"),
            "job_key": row.get("job_key"),
            "type": row.get("type"),
            "n_procs": len(tree),
            "pids": ",".join(str(p) for p in sorted(tree)),
            "cpu_cores": round(cpu_cores, 3),
            "cpu_pct_host": round(100.0 * cpu_cores / NCPU, 1),
            "rss_mb": round(rss_bytes / 1048576.0, 1),
            "connect_cpu_current": row.get("cpu_current"),
            "connect_ram_mb": round(connect_ram / 1048576.0, 1) if isinstance(connect_ram, (int, float)) else None,
        })

    _prev_cpu = cur_cpu
    df = pd.DataFrame(records, columns=list(RECORD_FIELDS))
    if not df.empty:
        df = df.sort_values("cpu_cores", ascending=False).reset_index(drop=True)
        history.append((now_ts, float(df["cpu_cores"].sum()), float(df["rss_mb"].sum()) / 1024.0))
        if record:
            _write_records(records)
    return df, err


def _write_records(records: List[dict]) -> None:
    global _record_started
    try:
        Path(RECORD_PATH).parent.mkdir(parents=True, exist_ok=True)
        new_file = not Path(RECORD_PATH).exists() or not _record_started
        with open(RECORD_PATH, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=RECORD_FIELDS)
            if new_file and Path(RECORD_PATH).stat().st_size == 0:
                w.writeheader()
            w.writerows(records)
        _record_started = True
    except OSError:
        pass


def _main() -> int:
    ap = argparse.ArgumentParser(description="Headless Connect content resource recorder.")
    ap.add_argument("--polls", type=int, default=0, help="number of polls (0 = run forever)")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    args = ap.parse_args()
    print(f"recording to {RECORD_PATH}; server={CONNECT_SERVER}; interval={args.interval}s")
    compute_snapshot(record=False)  # prime CPU deltas
    time.sleep(args.interval)
    n = 0
    while args.polls == 0 or n < args.polls:
        df, err = compute_snapshot()
        if err:
            print("WARN:", err)
        else:
            total = df["cpu_cores"].sum() if not df.empty else 0.0
            print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {len(df)} content proc-groups, "
                  f"{total:.2f} cores in use")
            if not df.empty:
                print(df[["content_name", "type", "n_procs", "cpu_cores", "rss_mb",
                          "connect_cpu_current", "connect_ram_mb"]].to_string(index=False))
        n += 1
        if args.polls == 0 or n < args.polls:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
