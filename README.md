# connect-content-metrics-example

Worked examples for measuring **per-content CPU and memory** on a Posit Connect server,
plus a technique for getting *accurate* per-content resource usage that the built-in
metrics miss.

## Background: what Connect's built-in metrics do and don't give you

Connect can record per-content **job** resource metrics (CPU, memory, connections) when
`Applications.MetricsCollectionEnabled = true` (or per-content `metrics_collection_enabled`).
They are retrieved from the REST API, **not** OpenTelemetry:

- `GET /__api__/v1/content/{guid}/jobs`
- `GET /__api__/v1/content/{guid}/jobs/{key}/metrics` → `{samples: [{timestamp, cpu_fraction, memory_bytes, connections, process_id}]}`

Verified behavior (Connect 2026.10.0):

| Content type | What the built-in metric reflects |
| --- | --- |
| **Interactive** (FastAPI, Shiny, Streamlit, Plumber, …) | the real worker process → **accurate** CPU/RSS |
| **Rendered** (Jupyter, R Markdown, Quarto) | the render **supervisor** process → near-zero CPU, tiny RSS (misses the kernel/child that does the work) |
| Off-host / Launcher / Kubernetes execution | not recorded at all (writer is a no-op) |

So for scheduled reports / renders, `cpu_fraction` and `memory_bytes` undercount badly.
`content-monitor/` is the fix.

## Contents

| Path | What it is |
| --- | --- |
| `metrics-client/get_connect_metrics.py` | Pulls the built-in per-content job metrics via the REST API into a tidy pandas DataFrame + CSV/Parquet, with per-job peak/mean summaries. Good for interactive content. |
| `content-monitor/` | **The investigation result.** An interactive Shiny-for-Python app you deploy to Connect that measures the *true* real-time CPU/memory of **all** running content — including renders — by joining OS-level `/proc` subtree accounting with Connect's `GET /__api__/metrics/procs` PID→content mapping. Reports it live and records it to CSV. |
| `capacity-gate/` | A Quarto document (R + Python) that defers a resource-intensive section until the host has ≥ 4 GB free memory and ≥ 2 free CPU cores, with standalone reusable gate functions. |
| `examples/fastapi-load-app/` | A tiny FastAPI app that holds ~1.2 GB and burns CPU on demand — used to generate a measurable load for the tools above. |
| `scripts/` | Reproducible steps used to stand up the Connect test server (install, bootstrap an admin API key, register R/Python/Quarto, deploy a load app). Reference only; they encode no secrets. |
| `results/` | Sample output from `get_connect_metrics.py`. |

## How the accurate measurement works (`content-monitor`)

1. Call `GET /__api__/metrics/procs` (admin key). It returns, for every running content
   process: `pid`, `app_guid`, `app_name`, `app_run_as`, `job_key`, `type` (job tag),
   plus Connect's own `cpu_current`/`ram`. This is the authoritative **PID → content** map
   and it includes active **renders**, not just long-running apps.
2. For each reported PID, walk the **process subtree** using world-readable
   `/proc/<pid>/stat` (every process, regardless of owner): sum `utime+stime` jiffie deltas
   between polls for CPU, and sum `RSS` pages for memory. The subtree is what captures the
   render kernel/child that Connect's own number misses.
3. Attribute the subtree totals to the content item and show **measured vs. Connect-reported**
   side by side, so the undercount is visible.
4. Append every poll to a CSV so you have a time series.

See `content-monitor/README.md` for deployment and configuration.

## Status

All components were built and validated end-to-end on a live Posit Connect 2026.10.0 server
(Ubuntu 24.04). This repository is an example/reference, not a supported Posit product.
