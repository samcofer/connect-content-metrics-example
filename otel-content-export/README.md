# Per-content resource utilization to Datadog via OpenTelemetry

Getting **per-content CPU and memory** out of Posit Connect and into Datadog (or
any OTLP backend) using OpenTelemetry — what works, what doesn't, and a working,
proven sidecar that does it.

Everything here was verified end-to-end on a disposable **Posit Connect
2026.10.0** server (Ubuntu 24.04), with R 4.5.1, Python 3.12.11, and a local
OpenTelemetry Collector standing in for the Datadog Agent. The collector's
file-exporter captures are checked in under [`evidence/`](evidence/).

## Bottom line

| Goal: per-content signal in Datadog via OTel | Works today (2026.10.0)? | How |
| --- | --- | --- |
| Per-content **CPU / memory** | ✅ **with the sidecar here** | [`content-metrics-otel/`](content-metrics-otel/) exports `connect.content.cpu.cores` / `.cpu.utilization` / `.memory.rss_bytes`, tagged `content.guid`, directly to the Agent |
| Per-content **execution** (duration, success/failure) | ✅ **natively, no code** | Connect emits `job.duration` + `job.completion` with `content.guid` / `content.name` data-point attributes; enable `[OpenTelemetry]` + an `[OTLPEndpoint]` with `Metrics = true` |
| Per-content **traces** (request latency, spans) | ⚠️ **retained in Connect, not forwarded** | content traces land in local files + the `GET /__api__/v1/content/{guid}/traces` API, not at the external endpoint; a content-env `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` override can push them to the Agent directly |
| Content item **emitting its own custom metrics** through Connect's OTel | ❌ **dropped** | Connect's embedded collector has no content-metrics pipeline — see [`evidence/connect-collector-pipeline.md`](evidence/connect-collector-pipeline.md) |

The important negative result: **you cannot get per-content CPU/RAM by having
each content item emit OpenTelemetry metrics about itself.** Connect injects an
OTel SDK and sets `OTEL_METRICS_EXPORTER=otlp` in the content process, the
content's `force_flush()` succeeds against Connect's embedded collector — and
then the collector drops it, because content-origin metrics are filtered out
before the external endpoint and there is no content-metrics pipeline. This is
in the Connect source, not a misconfiguration (citation in the evidence doc).
Content traces and logs are not dropped but they are **retained inside Connect**
(local files + the content traces API), not fanned out to your external endpoint
by default.

So per-content CPU/RAM has to be measured **out of band** and shipped straight
to the Agent. That's the sidecar.

## 1. The sidecar — per-content CPU/RAM → Datadog Agent (the answer)

[`content-metrics-otel/emit_content_metrics_otel.py`](content-metrics-otel/emit_content_metrics_otel.py)

One process (a Connect scheduled job, a Launcher job, or a host service) measures
the **true** per-content CPU/memory for *all* running content and exports it as
OTLP gauges straight to the local OTLP endpoint — bypassing Connect's embedded
collector, so the content-metrics drop does not apply. It gets accurate numbers
by joining Connect's PID→content map (`GET /__api__/metrics/procs`) with `/proc`
subtree accounting, which also fixes the rendered-content undercount that the
built-in REST metric suffers from (see this repo's `content-monitor/`).

Metrics emitted each interval, one data point per running content proc-group:

| Metric | Unit | Meaning |
| --- | --- | --- |
| `connect.content.cpu.cores` | `{cpu}` | CPU cores in use by the content process subtree |
| `connect.content.cpu.utilization` | `1` | the above as a fraction of host CPU count |
| `connect.content.memory.rss_bytes` | `By` | RSS of the content process subtree |

Every point carries `content.guid`, `content.name`, `content.type`, `run_as`.

```bash
pip install -r content-metrics-otel/requirements.txt
CONNECT_SERVER=http://localhost:3939 \
CONNECT_API_KEY=<admin-key> \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \   # the Datadog Agent's OTLP intake
OTEL_RESOURCE_ATTRIBUTES=service.namespace=my-connect \
  python content-metrics-otel/emit_content_metrics_otel.py --interval 10
```

Proven output (from [`evidence/sidecar-signal-inventory.txt`](evidence/sidecar-signal-inventory.txt),
captured while a FastAPI item burned CPU on a 2-core host):

```
connect.content.cpu.cores        content.guid=bd9b0747-...  value=0.912
connect.content.cpu.utilization  content.guid=bd9b0747-...  value=0.456
connect.content.memory.rss_bytes content.guid=bd9b0747-...  value=72282112
```

For production, point `OTEL_EXPORTER_OTLP_ENDPOINT` at the node-local Datadog
Agent (the Agent authenticates to the Datadog backend). The three gauges map
cleanly onto Datadog metrics you can group by `content.guid` / `content.name`.

## 2. Content self-emitting metrics — the pattern, and why it's dropped

[`content-self-emit/`](content-self-emit/) has a Python (FastAPI) and an R
(Plumber) content item that record custom OpenTelemetry metrics the documented
way — through Connect's injected SDK, no exporter wiring. The code is correct and
forward-compatible: if Connect adds a content-metrics pipeline, these just start
flowing. Today they are accepted by the embedded collector and then filtered out
before the external endpoint (see the evidence doc). Use these to understand the
publisher-side API; use the sidecar to actually get metrics to Datadog.

Two things verified while deploying these:

- **Python** uses Connect's injected SDK (the bundle does not ship
  `opentelemetry-sdk`). With Traces enabled, the worker gets the full `OTEL_*`
  environment and the metrics API works; from inside the worker `force_flush()`
  succeeds, confirming the drop happens at Connect's collector, not in the
  content.
- **R** content telemetry needs the `otelsdk` package, which Connect installs on
  the telemetry rebuild. `otelsdk` is Rust-backed: on a host without a Rust
  toolchain (or a matching prebuilt binary in the repo) its source install fails
  and Connect logs *"failed to install otelsdk ... Trace collection will be
  unavailable"*. The `otel` API package still loads, but `get_meter()` returns a
  no-op, so nothing is emitted. Plan for a Rust toolchain (or a binary `otelsdk`)
  on R nodes if you want R content telemetry. Either way, R content CPU/RAM is
  covered by the sidecar.

## 3. Server setup

[`setup/rstudio-connect.otel.gcfg`](setup/rstudio-connect.otel.gcfg) — the
`[OpenTelemetry]` + `[OTLPEndpoint]` block to merge into
`/etc/rstudio-connect/rstudio-connect.gcfg`. Point the endpoint at the local
Datadog Agent. This is what turns on the native per-job metrics (`job.duration`,
`job.completion`) and content-trace retention.

[`setup/otelcol-capture.yaml`](setup/otelcol-capture.yaml) — the lab stand-in for
the Datadog Agent: an OpenTelemetry Collector that writes every received signal
to a file so you can see exactly what Connect exports. Note the one gotcha: set
`service.telemetry.metrics.level: none`, or its default `:8888` Prometheus port
collides with Connect's embedded collector and Connect won't start.

## Reproducing

1. Install Connect 2026.10.0; register R / Python / Quarto; add the `[OpenTelemetry]`
   + `[OTLPEndpoint]` config; restart.
2. Run a collector (or the Datadog Agent) on the OTLP endpoint.
3. Deploy the content in `content-self-emit/`; enable **Traces** on each
   (`PATCH /v1/content/{guid} {"otel_enabled": true}`).
4. Run the sidecar from `content-metrics-otel/`.
5. Inspect what arrives. The captures in `evidence/` are the reference.

## Evidence

- [`evidence/sidecar-capture.sample.jsonl`](evidence/sidecar-capture.sample.jsonl) + [`sidecar-signal-inventory.txt`](evidence/sidecar-signal-inventory.txt) — the sidecar's per-content CPU/RAM arriving at the endpoint.
- [`evidence/connect-collector-signal-inventory.txt`](evidence/connect-collector-signal-inventory.txt) — what reaches the external endpoint from the content-self-emit test: content custom metrics absent; native `job.duration`/`job.completion` present with `content.guid` data-point attributes.
- [`evidence/content-traces-api-sample.ndjson`](evidence/content-traces-api-sample.ndjson) — content request traces retained in Connect (read via the traces API), tagged `content.guid`/`job.key`.
- [`evidence/connect-collector-pipeline.md`](evidence/connect-collector-pipeline.md) — the Connect-source reason content metrics are dropped and traces/logs stay local.
