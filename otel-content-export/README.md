# Per-content resource metrics via OpenTelemetry traces

Getting **per-content CPU and memory** out of Posit Connect and into Datadog (or
any OTLP backend) when true OpenTelemetry *metrics* from content can't be used.
The trick: the content defines its own **custom span**, records CPU/memory as
span attributes, and marks the span `is_public` so Connect forwards it to the
Connect-configured OTLP destination. No sidecar, no separate agent process, no
per-content endpoint override.

Verified end-to-end on a disposable **Posit Connect 2026.10.0** server
(Ubuntu 24.04). Captures are in [`evidence/`](evidence/).

## Why traces instead of metrics

Connect injects an OpenTelemetry SDK into instrumented content and sets
`OTEL_METRICS_EXPORTER=otlp`, so content *can* record metrics. But Connect's
embedded collector has **no content-metrics pipeline**: content-origin metrics
(`service.name = posit-connect-content-<guid>`) are filtered out before the
external `[OTLPEndpoint]`. Confirmed on the live server (the content's own
`force_flush()` succeeds but nothing arrives) and in the Connect source
(`src/connect/otel/collector/config.go`: *"there is no content metrics
pipeline"*). So true content metrics are a dead end for Datadog.

Traces are different. Content spans are emitted and captured, and they carry the
content resource attributes (`content.guid`, `job.key`, ...). By default they
are **retained inside Connect** (local files + the
`GET /__api__/v1/content/{guid}/traces` API) and not forwarded, because Connect
runs its external ("customer") trace pipeline behind a filter that keeps only
spans whose `is_public` attribute is `true` (the `OpenTelemetry.PublicTracesOnly`
setting, on by default). A content item can set that attribute on its own span.

## The technique (verified)

In the content, open a custom span, attach the resource numbers as attributes,
and set `is_public = true`:

```python
from opentelemetry import trace
from opentelemetry.trace import SpanKind

tracer = trace.get_tracer("ird.content.resource")

with tracer.start_as_current_span("content.resource.sample", kind=SpanKind.INTERNAL) as span:
    span.set_attribute("is_public", True)                 # opt into the external pipeline
    span.set_attribute("content.memory.rss_bytes", rss)   # from /proc/self
    span.set_attribute("content.cpu.user_seconds", utime)
    span.set_attribute("content.cpu.system_seconds", stime)
```

Connect auto-ingests the span through its embedded collector and forwards it to
the configured `[OTLPEndpoint]` (the Datadog Agent), tagged with the content
resource attributes. Full examples:

- [`content-trace-metrics/python/app.py`](content-trace-metrics/python/app.py) — FastAPI, verified.
- [`content-trace-metrics/r/plumber.R`](content-trace-metrics/r/plumber.R) — the R parallel via the `otel` package (needs `otelsdk`; see caveats).

### Proof

With `is_public = true` (from [`evidence/spans-external-with-ispublic.txt`](evidence/spans-external-with-ispublic.txt)), the custom spans reach the external endpoint:

```
service.name = posit-connect-content-28f28464-...   <== CONTENT PROCESS
  content.guid = 28f28464-...   job.key = qgoGsntXG1rwk5wc
  content.resource.sample  x8
     content.cpu.user_seconds=2.44  content.memory.rss_bytes=78827520  content.process.num_threads=5
```

Without it ([`evidence/spans-dropped-without-ispublic.txt`](evidence/spans-dropped-without-ispublic.txt)), zero reach the endpoint; they are still captured in Connect's traces API ([`evidence/content-traces-api-sample.ndjson`](evidence/content-traces-api-sample.ndjson)). The only difference is the one attribute.

## Server setup

[`setup/rstudio-connect.otel.gcfg`](setup/rstudio-connect.otel.gcfg) — `[OpenTelemetry]`
plus an `[OTLPEndpoint]` pointed at the node-local Datadog Agent. Nothing extra is
needed for the per-span `is_public` approach; the default `PublicTracesOnly`
keeps everything else on-host.

[`setup/otelcol-capture.yaml`](setup/otelcol-capture.yaml) — the lab stand-in for
the Datadog Agent (an OpenTelemetry Collector writing received signals to a
file). One gotcha: set `service.telemetry.metrics.level: none`, or its default
`:8888` Prometheus port collides with Connect's embedded collector and Connect
won't start.

## Caveats (read before shipping)

- **`is_public` is an internal mechanism.** `OpenTelemetry.PublicTracesOnly` and
  the `is_public` attribute are marked `internal` in Connect's settings; they are
  not (yet) a documented, supported contract. The technique works on 2026.10.0,
  but confirm with Posit before depending on it long term.
- **Server-wide alternative:** setting `OpenTelemetry.PublicTracesOnly = false`
  forwards *all* content spans to the external endpoint (no per-span attribute),
  at the cost of exporting every content span. Also an internal setting.
- **Documented alternative:** a content item can bypass Connect's collector
  entirely by setting `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` in its own environment
  to the Agent. That path is documented, but Connect then stops retaining those
  traces.
- **Coverage:** this only covers content that includes the instrumentation and is
  running. Rendered reports and uninstrumented content won't self-report.
- **Semantics:** CPU/memory are gauges; carrying them as span attributes works
  and Datadog can derive span-based metrics from them, but it is a trace-shaped
  workaround for the fact that content metrics can't be exported.
- **R:** needs `otelsdk` (Rust-backed); on a host without a Rust toolchain or a
  prebuilt binary it fails to install and R tracing is unavailable. The R example
  here was not live-verified for that reason.

## Reproducing

1. Install Connect 2026.10.0; register Python (and R); add the `[OpenTelemetry]` +
   `[OTLPEndpoint]` config; restart.
2. Run a collector (or the Datadog Agent) on the OTLP endpoint.
3. Deploy `content-trace-metrics/python`; enable Traces
   (`PATCH /v1/content/{guid} {"otel_enabled": true}`).
4. Exercise it and watch the `content.resource.sample` spans arrive at the
   endpoint with the CPU/memory attributes. The captures in `evidence/` are the
   reference.
