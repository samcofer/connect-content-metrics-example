# Why content-emitted metrics never leave Connect (source)

Connect runs an embedded OpenTelemetry Collector and fans signals out to the
`[OTLPEndpoint]` targets you configure. The pipeline topology is built in
`src/connect/otel/collector/config.go` (Connect 2026.10.0). The relevant parts:

The `metrics` and `logs` pipelines that export to external endpoints apply a
`filter/service_only` processor, and there is **no content-metrics pipeline**.
The code says so directly:

> Drop signals where `service.name` matches content pattern (keep service
> signals only). Content processes are told to export metrics too
> (`OTEL_METRICS_EXPORTER=otlp`), but **there is no content metrics pipeline**;
> without the metrics filter they would pollute the node-level metrics file.

What actually reaches an external `[OTLPEndpoint]` vs what stays in Connect, as
observed on a live 2026.10.0 server (default settings):

| Content signal (`service.name = posit-connect-content-<guid>`) | Goes to external `[OTLPEndpoint]` (Datadog Agent)? | Where it does go |
| --- | --- | --- |
| traces  | **No** (not observed at the external endpoint by default) | retained in Connect: local files `/var/lib/rstudio-connect/otel/content_guid=<guid>/job_key=<key>/trace.jsonl` and the `GET /__api__/v1/content/{guid}/traces` API |
| logs    | **No** | local files (`logs/content` → `file/content_logs`) |
| metrics | **No** — dropped; no content-metrics pipeline | nowhere (filtered out) |

Empirical confirmation (captures in this folder):

- **Metrics dropped.** A FastAPI content item recorded a custom
  counter/histogram/gauge through the injected OpenTelemetry SDK. From inside the
  worker `force_flush()` returned `True` (the embedded collector accepted the
  POST), yet no content metric (`ird.*`, `service.name = posit-connect-content-*`)
  ever arrived at the external endpoint. Only `service.name = posit-connect`
  metrics did. See `connect-collector-signal-inventory.txt`.
- **Traces retained, not forwarded.** The same item's FastAPI request spans were
  emitted and show up in the content traces API and in the local
  `.../trace.jsonl` files, tagged `content.guid`/`job.key`, but did not appear at
  the external OTLP endpoint.

To push content traces (or metrics) straight to Datadog instead of Connect's
embedded collector, a publisher can override the per-signal endpoint in the
content's environment, e.g. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` pointed at the
local Agent. Connect applies content env vars after its own, so the override
wins (documented behavior; note Connect then no longer retains those traces).
The CPU/RAM sidecar avoids this question entirely by exporting out of band.

## What per-content signal DOES reach an external endpoint out of the box

Connect's own server instrumentation emits per-job metrics that are **not**
filtered (their `service.name` is `posit-connect`), carrying per-content
data-point attributes:

```
job.completion  attrs: content.guid, content.name, job.exit_code, job.status
job.duration    attrs: content.guid, content.name, job.exit_code, job.status
```

These give per-content execution duration and success/failure in Datadog with
no code. They do **not** include CPU or memory — for that, use the sidecar in
`../content-metrics-otel/`.
