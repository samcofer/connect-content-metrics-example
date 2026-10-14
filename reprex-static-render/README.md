# Reprex: Connect under-reports rendered (static) content CPU/memory

A **self-contained** reproducible example proving that Posit Connect's built-in per-job
resource metrics drastically undercount **rendered / static** content (Jupyter, R Markdown,
Quarto). The reprex is a Jupyter notebook that, as a single ~5-minute render:

1. discovers its own `content-guid` + `job-key` by walking up the `/proc` parent chain to
   the `connect-session` supervisor and parsing its (world-readable) cmdline;
2. runs a sustained CPU + ~1.5 GB memory workload while sampling its **own** process subtree
   from `/proc` (ground truth);
3. reports that self-measured usage (summary + plot) in the rendered output;
4. fetches Connect's per-job metrics for **this very render** and prints a contrast + verdict.

## Result (Connect 2026.10.0, 4-core host)

One render, job `lXgoyddaytLO3IOj`, 2026-10-14 14:38:54Z → 14:44:15Z (**321 s**, `exit_code 0`):

| metric | self-measured (peak) | Connect per-job metric (peak) |
| --- | --- | --- |
| CPU (cores) | **3.99** (mean 3.33) | **0.000** |
| Memory (MB) | **1546** | **27** |

Connect recorded **31–32 samples** across the 5-minute render — every one of them `0` CPU
and `27 MB`. The rendered notebook prints its own verdict:

> VERDICT: for this render Connect UNDER-REPORTS CPU by infinite (Connect reported 0) and memory by ~56x.

Full rendered output: [`../results/reprex_rendered.html`](../results/reprex_rendered.html);
text summary: [`../results/reprex_contrast.txt`](../results/reprex_contrast.txt).

## Why it happens

A static render runs as a process tree:

```
connect-session (supervisor Connect spawns)        <- the pid Connect samples
└─ build_report.py (render harness)                   (~27 MB RSS, ~0 CPU)
   └─ ipykernel (the notebook kernel)              <- where numpy allocates 1.5 GB
      └─ (BLAS worker threads burning ~4 cores)       and burns CPU
```

Connect's per-job metrics come from `connect-session` sampling the single process Connect
spawned (the supervisor / harness), not its descendants. The kernel that does the actual
work is a grandchild and is never counted — so CPU reads `0` and memory reads the harness's
tens of MB regardless of the real load. (Verified directly: during the render the harness
pid showed 27 MB / 0 cpu in `GET /__api__/metrics/procs` while the kernel pid held 1.58 GB.)

Interactive content (FastAPI/Shiny/etc.) does not have this problem because the worker Connect
spawns *is* the process doing the work.

## Run it yourself

```bash
pip install rsconnect-python
export CONNECT_SERVER=https://connect.example.com
export CONNECT_API_KEY=<publisher key>     # to deploy
export REPREX_API_KEY=<owner or admin key> # the reprex uses this to read its own job metrics
./deploy.sh
```

`build_reprex.py` generates `reprex.ipynb`; `deploy.sh` deploys and renders it. The workload
length is `REPREX_SECONDS` (default 270).

### Reproduction notes

- **Trigger a second render after deploy.** `rsconnect` applies `-E` environment variables
  *after* the initial deploy-time render, so that first render self-measures but has no key
  for the contrast step. `deploy.sh` documents the one-line `POST .../render` to run a second
  render with the env present. (The observed numbers above are from that second render.)
- `connect-session` has a stdin timeout `-st` = `Applications.RuntimeSessionStdinTimeout`
  (default 300 s). It did **not** cap this render — the 321 s render completed with
  `exit_code 0` — but if you push the workload much longer and see truncation, raise that
  setting.
- The reprex writes a CSV to `REPREX_RECORD` (default `/tmp`), which lands in the render's
  sandbox filesystem; the authoritative artifact is the rendered HTML, which embeds the
  numbers and the plot.

## Has this been reported upstream? (`posit-dev/connect`)

Searched `posit-dev/connect` issues/PRs. **No issue filed specifically for this
render-metrics under-reporting**, but the root cause is acknowledged:

- **#40287 — "feat: process metrics for the masses"** lists *"Child/grandchild process
  tracking (forked workers …)"* under **Out of Scope**, and its design collects metrics only
  for *"PIDs [Connect] has spawned."* That is exactly the mechanism behind this bug: only the
  spawned supervisor is measured, never the subtree.
- **#31544 — "add resource usage to the `GET v1/content/{GUID}/jobs` endpoint"** (claude-triaged)
  notes resource usage today comes from the supervisor-based `metrics/procs`.
- **#35361 — "Add CPU and memory maximums to `jobs_metrics` table"** builds on the same
  (supervisor) measurement, so maxima would inherit the undercount.
- **#37377 — "Replace `rserver-monitor`"** and **#22209** (rserver-monitor host-metric
  inaccuracy) are adjacent, but about host-level metrics, not per-content subtree accounting.

Net: the under-reporting of rendered-content CPU/memory is implied by #40287's out-of-scope
note but is **not** tracked as its own bug. This reprex is suitable to attach to a new issue.
