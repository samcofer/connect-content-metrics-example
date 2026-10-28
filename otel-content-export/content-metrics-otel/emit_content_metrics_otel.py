"""Emit per-content CPU and memory to an OTLP endpoint (e.g. the Datadog Agent).

This is the scalable answer to "get per-content resource utilization into Datadog
via OpenTelemetry" on Posit Connect 2026.10.0.

Why a sidecar instead of each content item emitting its own metrics:

  - Connect injects an OTel SDK into instrumented content and sets
    OTEL_METRICS_EXPORTER=otlp, so a content item CAN record metrics. But its
    embedded collector has NO content-metrics pipeline: content-origin metrics
    (service.name = posit-connect-content-<guid>) are filtered out before the
    external [OTLPEndpoint]. See src/connect/otel/collector/config.go:
    "Content processes are told to export metrics too (OTEL_METRICS_EXPORTER=otlp),
    but there is no content metrics pipeline". So content-self-emitted metrics
    never reach Datadog through Connect's collector.
  - Connect's own built-in per-job metric (the REST
    /__api__/v1/content/{guid}/jobs/{key}/metrics) undercounts rendered content:
    it samples only the connect-session supervisor, so Jupyter/Rmd/Quarto renders
    read near-zero CPU and tiny RSS.

This process runs once (as a Connect scheduled job, a systemd service, or a
Launcher job) and measures the TRUE per-content CPU/memory for ALL running
content by joining Connect's PID->content map (GET /__api__/metrics/procs) with
/proc subtree accounting, then exports it as OTLP metric gauges straight to the
local collector/Agent -- not through Connect's embedded collector -- so the
content-metrics drop does not apply.

Metrics emitted (one data point per running content proc-group each interval):
  connect.content.cpu.cores          gauge  {cpu}   cores in use by the subtree
  connect.content.cpu.utilization    gauge  1       cores / host CPU count
  connect.content.memory.rss_bytes   gauge  By      resident set size of subtree
Attributes on every point: content.guid, content.name, content.type, run_as.

Environment:
  CONNECT_SERVER                    Connect base URL (default http://localhost:3939)
  CONNECT_API_KEY                   admin API key (required; reads /__api__/metrics/procs)
  OTEL_EXPORTER_OTLP_ENDPOINT       OTLP/HTTP base, e.g. the Datadog Agent http://localhost:4318
  OTEL_SERVICE_NAME                 defaults to connect-content-metrics
  OTEL_RESOURCE_ATTRIBUTES          e.g. service.namespace=<deployment>
Run:
  python emit_content_metrics_otel.py --interval 10
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from typing import Dict, List, Optional, Tuple

import requests
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

CONNECT_SERVER = os.environ.get("CONNECT_SERVER", "http://localhost:3939").rstrip("/")
CONNECT_API_KEY = os.environ.get("CONNECT_API_KEY", "")
VERIFY_TLS = os.environ.get("CONNECT_VERIFY", "true").lower() not in ("0", "false", "no")

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
NCPU = os.cpu_count() or 1

_prev_cpu: Dict[int, Tuple[int, float]] = {}


def read_proc_table() -> Dict[int, dict]:
    """{pid: {ppid, jiffies, rss_bytes}} for every readable process.

    /proc/<pid>/stat is world-readable, so this works across Connect's
    per-content user-isolation boundaries.
    """
    table: Dict[int, dict] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat") as fh:
                data = fh.read()
        except (OSError, ValueError):
            continue
        try:
            rparen = data.rfind(")")  # comm (field 2) may contain spaces/parens
            after = data[rparen + 2:].split()
            ppid = int(after[1])  # field 4
            jiffies = int(after[11]) + int(after[12])  # utime + stime
            rss_pages = int(after[21])  # field 24
        except (ValueError, IndexError):
            continue
        table[pid] = {"ppid": ppid, "jiffies": jiffies, "rss_bytes": rss_pages * PAGE_SIZE}
    return table


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


def fetch_proc_map() -> List[dict]:
    """GET /__api__/metrics/procs -> list of {pid, app_guid, app_name, type, ...}."""
    if not CONNECT_API_KEY:
        raise SystemExit("CONNECT_API_KEY is required (admin key for /__api__/metrics/procs)")
    resp = requests.get(
        f"{CONNECT_SERVER}/__api__/metrics/procs",
        headers={"Authorization": f"Key {CONNECT_API_KEY}"},
        timeout=10,
        verify=VERIFY_TLS,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows if isinstance(rows, list) else rows.get("results", [])


def snapshot() -> List[dict]:
    """One measurement cycle: true per-content CPU cores + RSS for all content."""
    global _prev_cpu
    proc_rows = fetch_proc_map()
    table = read_proc_table()
    kids: Dict[int, List[int]] = {}
    for pid, info in table.items():
        kids.setdefault(info["ppid"], []).append(pid)
    now_wall = time.monotonic()
    cur_cpu = {pid: (info["jiffies"], now_wall) for pid, info in table.items()}

    def pid_cpu_cores(pid: int) -> float:
        if pid not in table or pid not in _prev_cpu:
            return 0.0
        prev_j, prev_w = _prev_cpu[pid]
        dj = table[pid]["jiffies"] - prev_j
        dw = now_wall - prev_w
        return (dj / CLK_TCK) / dw if dj >= 0 and dw > 0 else 0.0

    out: List[dict] = []
    for row in proc_rows:
        try:
            root = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            root = 0
        if root <= 0 or root not in table:
            continue  # exited, or off-host (Launcher/Kubernetes) execution
        tree = subtree(root, kids)
        cpu_cores = sum(pid_cpu_cores(p) for p in tree)
        rss_bytes = sum(table[p]["rss_bytes"] for p in tree if p in table)
        out.append({
            "content.guid": row.get("app_guid") or "unknown",
            "content.name": row.get("app_name") or "(unknown)",
            "content.type": row.get("type") or "unknown",
            "run_as": row.get("app_run_as") or "unknown",
            "cpu_cores": cpu_cores,
            "rss_bytes": float(rss_bytes),
        })
    _prev_cpu = cur_cpu
    return out


# Latest snapshot shared with the OTel observable-gauge callbacks.
_latest: List[dict] = []


def _attrs(r: dict) -> dict:
    return {
        "content.guid": r["content.guid"],
        "content.name": r["content.name"],
        "content.type": r["content.type"],
        "run_as": r["run_as"],
    }


def _obs_cpu_cores(_o: CallbackOptions):
    return [Observation(r["cpu_cores"], _attrs(r)) for r in _latest]


def _obs_cpu_util(_o: CallbackOptions):
    return [Observation(r["cpu_cores"] / NCPU, _attrs(r)) for r in _latest]


def _obs_rss(_o: CallbackOptions):
    return [Observation(r["rss_bytes"], _attrs(r)) for r in _latest]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=10.0, help="seconds between snapshots")
    ap.add_argument("--cycles", type=int, default=0, help="number of snapshots (0 = forever)")
    args = ap.parse_args()

    os.environ.setdefault("OTEL_SERVICE_NAME", "connect-content-metrics")
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(), export_interval_millis=int(args.interval * 1000)
    )
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    meter = metrics.get_meter("connect.content.metrics")
    meter.create_observable_gauge("connect.content.cpu.cores", unit="{cpu}",
                                  description="CPU cores in use by the content process subtree",
                                  callbacks=[_obs_cpu_cores])
    meter.create_observable_gauge("connect.content.cpu.utilization", unit="1",
                                  description="Content CPU cores as a fraction of host CPU count",
                                  callbacks=[_obs_cpu_util])
    meter.create_observable_gauge("connect.content.memory.rss_bytes", unit="By",
                                  description="Resident set size of the content process subtree",
                                  callbacks=[_obs_rss])

    global _latest
    snapshot()  # prime CPU deltas
    time.sleep(args.interval)
    n = 0
    while args.cycles == 0 or n < args.cycles:
        _latest = snapshot()
        ts = dt.datetime.now().strftime("%H:%M:%S")
        total = sum(r["cpu_cores"] for r in _latest)
        print(f"[{ts}] {len(_latest)} content proc-groups, {total:.2f} cores; "
              f"exporting per-content gauges to {os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT')}")
        n += 1
        if args.cycles and n >= args.cycles:
            break
        time.sleep(args.interval)
    metrics.get_meter_provider().force_flush()
    metrics.get_meter_provider().shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
