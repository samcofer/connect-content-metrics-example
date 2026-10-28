# How content signals route in Connect 2026.10.0 (source + live proof)

Connect runs an embedded OpenTelemetry Collector and fans signals out to the
`[OTLPEndpoint]` targets you configure. Pipeline topology is built in
`src/connect/otel/collector/config.go`.

## Metrics: content-origin metrics are dropped

The external `metrics` (and `logs`) pipeline applies a `filter/service_only`
processor and there is **no content-metrics pipeline**. The code comment:

> Content processes are told to export metrics too
> (`OTEL_METRICS_EXPORTER=otlp`), but there is no content metrics pipeline;
> without the metrics filter they would pollute the node-level metrics file.

Live proof: a content item recorded a custom counter/gauge through the injected
SDK; from inside the worker `force_flush()` returned `True`, yet no content
metric (`service.name = posit-connect-content-*`) reached the external endpoint.
Only `service.name = posit-connect` metrics did (see
`connect-collector-signal-inventory.txt`). Connect's own per-job metrics
`job.duration` / `job.completion` DO export, carrying `content.guid` /
`content.name` / `job.exit_code` / `job.status` data-point attributes.

## Traces: forwarded only when the span is `is_public`

`OpenTelemetry.PublicTracesOnly` defaults to **true** (confirmed in
`settings.go` and `settings_onprem.go`; the setting is `internal`). With it on,
the collector splits traces into two pipelines (resolved config read off the
live server):

```yaml
traces/internal:   # full traces -> local files only
  receivers: [otlp]
  processors: [batch]
  exporters: [nop, file/traces]
traces/customer:   # -> external [OTLPEndpoint]
  receivers: [otlp]
  processors: [filter/customer_facing, transform/strip_is_public, ..., batch]
  exporters: [nop, otlp_http/<endpoint>_traces]
```

`filter/customer_facing` drops every span whose `is_public` attribute is not
`true`:

```yaml
filter/customer_facing:
  traces:
    span:
      - attributes["is_public"] != true   # drop
```

The filter checks the **span attribute**, regardless of who set it. Connect's
own `PublicSpanProcessor` stamps `is_public` only on server spans whose names are
in a hardcoded registry, but a content item can set the attribute on its own
span. `transform/strip_is_public` removes the attribute after filtering, so it
never reaches the backend.

### Live proof

Same content, same custom span `content.resource.sample`, only difference is the
`is_public` attribute:

| `is_public` on the span | Reaches external `[OTLPEndpoint]`? | In Connect traces API? |
| --- | --- | --- |
| not set (default) | **0 spans** (`spans-dropped-without-ispublic.txt`) | yes (`content-traces-api-sample.ndjson`) |
| `true` | **8 spans** (`spans-external-with-ispublic.txt`, `.sample.jsonl`) | yes |

The forwarded spans arrive under `service.name = posit-connect-content-<guid>`
with `content.guid` / `job.key` plus the CPU/memory attributes the content set.

## Takeaway

Content CPU/memory cannot leave Connect as OTel *metrics*, but it can as a
*trace* attribute if the span is marked `is_public` (or if
`OpenTelemetry.PublicTracesOnly` is turned off server-wide). Both are internal
mechanisms today.
