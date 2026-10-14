# content-monitor

An interactive Shiny-for-Python app, deployed as Connect content, that measures the
**true** real-time CPU and memory of every running content item on the same Connect
server — including **renders**, whose usage Connect's built-in per-job metrics undercount
(they reflect only the render supervisor process, not the kernel/child doing the work).

## How it works

1. `GET {CONNECT_SERVER}/__api__/metrics/procs` (admin API key) returns every running
   content process with `pid`, `app_guid`, `app_name`, `app_run_as`, `job_key`, and `type`
   — the authoritative **PID → content** map, covering active renders as well as apps.
2. For each reported `pid`, the collector walks the **process subtree** from world-readable
   `/proc/<pid>/stat` (readable for all processes regardless of owner), summing `utime+stime`
   jiffie deltas between polls (CPU, as a fraction of one core) and `RSS` pages (memory).
3. Totals are attributed to the content item and shown next to Connect's own numbers, so the
   undercount is visible. Each poll is appended to a CSV time series.

## Files

- `collector.py` — the measurement engine. Importable, and also runnable headless:
  ```bash
  CONNECT_SERVER=http://localhost:3939 CONNECT_API_KEY=<admin-key> \
      python collector.py --polls 0 --interval 5
  ```
  Run it as a **Connect scheduled job** for 24/7 recording independent of the dashboard.
- `app.py` — the Shiny dashboard (value boxes, a measured-vs-Connect table, a CPU-over-time
  plot, and a CSV download) layered over `collector.py`.
- `requirements.txt` — `shiny`, `pandas`, `requests`, `matplotlib`.

## Configuration (Connect content environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONNECT_SERVER` | `http://localhost:3939` | Connect base URL |
| `CONNECT_API_KEY` | *(required)* | **Admin** key — `/__api__/metrics/procs` lists all content |
| `MONITOR_POLL_SECONDS` | `5` | poll interval |
| `MONITOR_RECORD_PATH` | `$TMPDIR/connect_content_metrics.csv` | CSV record path |
| `CONNECT_VERIFY` | `true` | set `false` to skip TLS verification |

## Deploy

```bash
rsconnect deploy shiny \
  --server  https://connect.example.com \
  --api-key <publisher-key> \
  --title   "Content resource monitor" \
  content-monitor/

# then set the runtime env vars on the deployed content:
rsconnect content set-environment-vars --guid <monitor-guid> \
  CONNECT_SERVER=https://connect.example.com \
  CONNECT_API_KEY=<admin-key>
```

## Caveats

- The admin key lets this app see **all** content on the server — treat it as an admin tool
  and restrict who can view it.
- Reactive polling in `app.py` runs only while a browser session is open. For continuous,
  unattended recording use `collector.py` as a scheduled job.
- `MONITOR_RECORD_PATH` defaults to a temp dir, which is **not** durable across content
  restarts. Point it at a mounted volume, or push to a database / object store, for
  retention.
- Subtree CPU for a process group that spans a poll boundary is attributed to the poll in
  which it is observed; very short-lived render children between polls can be missed. Lower
  `MONITOR_POLL_SECONDS` to tighten resolution.
- `cpu_cores` is CPU as a fraction of one core summed over the subtree and can exceed 1.0 on
  multicore work.
