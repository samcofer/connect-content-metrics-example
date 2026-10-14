#!/usr/bin/env python3
"""Retrieve, format, and import per-content resource-utilization (CPU/memory) metrics
from Posit Connect.

For every targeted content item this tool enumerates the item's jobs and pulls the
per-job resource-metric samples that Connect records when per-content metrics collection
is enabled (server `Applications.MetricsCollectionEnabled = true`, or per-content
`metrics_collection_enabled = true`). Each sample carries a timestamp, CPU usage as a
fraction of a single core (can exceed 1.0 on multicore), resident memory in bytes, an
optional connection count, and the OS process id.

Output:
  * a tidy per-sample table  -> <out-dir>/connect_metrics_samples.csv (and .parquet if asked)
  * a per-job summary table   -> <out-dir>/connect_metrics_summary.csv  + printed to stdout

The samples table is the "import" artifact: one row per sample, ready to load into a
dataframe, a time-series store, or a Prometheus textfile exporter.

Requires Python 3.8+. Uses only the standard library; pandas is used when available
(nicer formatting + parquet), otherwise it falls back to the csv module.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

try:
    import pandas as pd  # optional
except Exception:  # pragma: no cover - pandas is optional
    pd = None


class ConnectClient:
    def __init__(self, server: str, api_key: str, insecure: bool = False, timeout: int = 30):
        self.base = server.rstrip("/") + "/__api__"
        self.api_key = api_key
        self.timeout = timeout
        self.ctx: Optional[ssl.SSLContext] = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str, params: Optional[Dict[str, str]] = None, retries: int = 3) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last_err: Optional[Exception] = None
        for attempt in range(retries):
            req = urllib.request.Request(url, headers={"Authorization": f"Key {self.api_key}"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as e:
                # Retry transient 5xx; surface 4xx immediately.
                if 500 <= e.code < 600 and attempt < retries - 1:
                    last_err = e
                    time.sleep(1.5 * (attempt + 1))
                    continue
                detail = e.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"GET {path} -> HTTP {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"GET {path} failed after {retries} attempts: {last_err}")


def resolve_content(client: ConnectClient, guids: List[str], titles: List[str]) -> List[Dict[str, Any]]:
    """Return content records matching the requested guids/titles (all, if neither given)."""
    listing = client.get("/v1/content")
    items = listing if isinstance(listing, list) else listing.get("results", [])
    if not guids and not titles:
        return items
    wanted = []
    for c in items:
        if c.get("guid") in guids or (c.get("title") in titles):
            wanted.append(c)
    # Also accept guids that were not in the listing (permission-scoped), fetch directly.
    found_guids = {c.get("guid") for c in wanted}
    for g in guids:
        if g not in found_guids:
            try:
                wanted.append(client.get(f"/v1/content/{g}"))
            except RuntimeError as e:
                print(f"  ! could not fetch content {g}: {e}", file=sys.stderr)
    return wanted


def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(v)
    except ValueError:
        return None


def collect_samples(client: ConnectClient, content: Dict[str, Any], cores: int) -> List[Dict[str, Any]]:
    guid = content.get("guid")
    title = content.get("title")
    rows: List[Dict[str, Any]] = []
    try:
        jobs = client.get(f"/v1/content/{guid}/jobs")
    except RuntimeError as e:
        print(f"  ! {title or guid}: cannot list jobs: {e}", file=sys.stderr)
        return rows
    jobs = jobs if isinstance(jobs, list) else jobs.get("results", [])
    for job in jobs:
        key = job.get("key")
        try:
            payload = client.get(f"/v1/content/{guid}/jobs/{key}/metrics")
        except RuntimeError as e:
            # 404/403 => metrics not available for this job; skip quietly.
            print(f"  . {title or guid} job {key}: no metrics ({e})", file=sys.stderr)
            continue
        samples = (payload or {}).get("samples") or []
        for s in samples:
            cpu = s.get("cpu_fraction")
            mem = s.get("memory_bytes")
            rows.append({
                "content_guid": guid,
                "content_title": title,
                "job_key": key,
                "job_tag": job.get("tag"),
                "timestamp": s.get("timestamp"),
                "cpu_fraction": cpu,
                "cpu_pct_of_host": (round(100.0 * cpu / cores, 2) if cpu is not None and cores else None),
                "memory_bytes": mem,
                "memory_mb": (round(mem / 1048576.0, 1) if mem is not None else None),
                "connections": s.get("connections"),
                "process_id": s.get("process_id"),
            })
    return rows


def summarize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_job: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        by_job.setdefault((r["content_guid"], r["content_title"], r["job_key"], r["job_tag"]), []).append(r)
    out = []
    for (guid, title, key, tag), rs in by_job.items():
        cpus = [r["cpu_fraction"] for r in rs if r["cpu_fraction"] is not None]
        mems = [r["memory_mb"] for r in rs if r["memory_mb"] is not None]
        times = sorted([t for t in (parse_ts(r["timestamp"]) for r in rs) if t])
        dur = (times[-1] - times[0]).total_seconds() if len(times) >= 2 else 0.0
        out.append({
            "content_title": title,
            "content_guid": guid,
            "job_key": key,
            "job_tag": tag,
            "samples": len(rs),
            "duration_s": round(dur, 1),
            "cpu_peak": round(max(cpus), 3) if cpus else None,
            "cpu_mean": round(sum(cpus) / len(cpus), 3) if cpus else None,
            "mem_peak_mb": round(max(mems), 1) if mems else None,
            "mem_mean_mb": round(sum(mems) / len(mems), 1) if mems else None,
        })
    out.sort(key=lambda d: (d["content_title"] or "", d["job_key"] or ""))
    return out


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def print_summary(summary: List[Dict[str, Any]]) -> None:
    cols = ["content_title", "job_key", "job_tag", "samples", "duration_s",
            "cpu_peak", "cpu_mean", "mem_peak_mb", "mem_mean_mb"]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in summary)) if summary else len(c) for c in cols}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("  ".join("-" * widths[c] for c in cols))
    for r in summary:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


def main() -> int:
    ap = argparse.ArgumentParser(description="Pull per-content CPU/memory metrics from Posit Connect.")
    ap.add_argument("--server", required=True, help="Connect base URL, e.g. http://host:3939")
    ap.add_argument("--api-key", default=os.environ.get("CONNECT_API_KEY"),
                    help="API key (or set CONNECT_API_KEY)")
    ap.add_argument("--content", action="append", default=[], metavar="GUID",
                    help="content GUID (repeatable); omit to target all visible content")
    ap.add_argument("--title", action="append", default=[], metavar="TITLE",
                    help="content title (repeatable)")
    ap.add_argument("--cores", type=int, default=os.cpu_count() or 1,
                    help="host core count, for cpu_pct_of_host (default: this host)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification")
    ap.add_argument("--out-dir", default=".", help="output directory")
    ap.add_argument("--format", choices=["csv", "parquet", "both"], default="csv")
    args = ap.parse_args()

    if not args.api_key:
        print("error: --api-key or CONNECT_API_KEY is required", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)
    client = ConnectClient(args.server, args.api_key, insecure=args.insecure)

    targets = resolve_content(client, args.content, args.title)
    if not targets:
        print("No matching content found.", file=sys.stderr)
        return 1

    all_rows: List[Dict[str, Any]] = []
    for c in targets:
        rows = collect_samples(client, c, args.cores)
        all_rows.extend(rows)

    if not all_rows:
        print("\nNo resource-metric samples were returned for the targeted content.\n"
              "Enable collection with `Applications.MetricsCollectionEnabled = true` (server-wide)\n"
              "or set per-content `metrics_collection_enabled = true`, then run the content so\n"
              "jobs execute. Note: rendered content (Rmd/Quarto/Jupyter) reports the render\n"
              "supervisor process (near-zero); interactive apps/APIs report true worker usage.",
              file=sys.stderr)
        return 1

    samples_csv = os.path.join(args.out_dir, "connect_metrics_samples.csv")
    summary_csv = os.path.join(args.out_dir, "connect_metrics_summary.csv")
    summary = summarize(all_rows)

    if pd is not None:
        df = pd.DataFrame(all_rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.sort_values(["content_title", "job_key", "timestamp"])
        if args.format in ("csv", "both"):
            df.to_csv(samples_csv, index=False)
        if args.format in ("parquet", "both"):
            df.to_parquet(os.path.join(args.out_dir, "connect_metrics_samples.parquet"), index=False)
        pd.DataFrame(summary).to_csv(summary_csv, index=False)
    else:
        if args.format in ("parquet", "both"):
            print("warning: pandas not installed; writing CSV only", file=sys.stderr)
        write_csv(samples_csv, all_rows)
        write_csv(summary_csv, summary)

    print(f"\nWrote {len(all_rows)} samples across {len(summary)} job(s) to {samples_csv}\n")
    print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
