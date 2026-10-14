# Live validation

Validated end-to-end on a Posit Connect 2026.10.0 server (Ubuntu 24.04, 4 cores / 16 GB)
on 2026-10-14, with two workloads running at once:

- `cpu-mem-api` — a FastAPI app holding ~1.2 GB and burning CPU (**interactive**; Connect
  measures it accurately).
- `cpu-mem-burn` — a Jupyter notebook render allocating ~1.5 GB in its kernel child
  (**rendered**; Connect's built-in metric undercounts it).

## Result: the collector captures the render that Connect undercounts

`collector.py` run against the live server while both workloads were active. The
`cpu-mem-burn` render over successive polls (abridged):

| time | type | n_procs | **measured cpu_cores** | **measured rss_mb** | connect cpu | connect ram_mb |
| --- | --- | --- | --- | --- | --- | --- |
| 13:12:22 | build_jupyter | 2 | 0.00 | 84.8 | 0 | 26.6 |
| 13:12:42 | build_jupyter | 3 | 0.52 | 157.5 | 0 | 26.6 |
| 13:12:47 | build_jupyter | 3 | 1.30 | **1669.4** | 0 | 26.6 |
| 13:12:57 | build_jupyter | 3 | 1.99 | **1669.4** | 0 | 26.6 |

Connect reports the render as **0 cpu / 26.6 MB** (the supervisor process) the whole time.
The collector, by summing the process subtree, measures the real **~2 cores / 1669 MB** once
the kernel child spins up (`n_procs` 2 → 3).

For the interactive `cpu-mem-api`, measured and Connect-reported agree (≈3.1 cores,
≈1249 MB) — the collector corrects the rendered case without distorting the interactive one.
Total CPU stayed capped at ~3.99 / 4 host cores.

A captured CSV is at [`../results/content_monitor_sample.csv`](../results/content_monitor_sample.csv).

## Interactive app

`app.py` was deployed to Connect as `python-shiny` content, its `CONNECT_SERVER` /
`CONNECT_API_KEY` (admin) environment variables set via
`PATCH /__api__/v1/content/{guid}/environment`, and the page served HTTP 200. Its reactive
loop calls the same `collector.compute_snapshot()` on each poll.
