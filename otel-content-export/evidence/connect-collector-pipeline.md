# How content signals reach (or don't reach) your OTLP endpoint

Observed on a disposable Posit Connect 2026.10.0 server with `[OpenTelemetry]`
enabled and an `[OTLPEndpoint]` pointed at a local capture collector standing in
for the Datadog Agent. Everything below is what actually arrived at that
endpoint. You can reproduce all of it with the captures in this folder, nothing
here depends on Connect internals.

## Metrics: content-origin metrics do not arrive

Connect instruments content and sets `OTEL_METRICS_EXPORTER=otlp`, so a content
item can record its own metrics and its `force_flush()` returns success. But none
of those content metrics (`service.name = posit-connect-content-<guid>`) showed
up at the external endpoint. Only server/node metrics (`service.name =
posit-connect`) did (see `connect-collector-signal-inventory.txt`).

Connect's own per-job metrics `job.duration` / `job.completion` DO arrive,
carrying `content.guid` / `content.name` / `job.exit_code` / `job.status`
data-point attributes.

Takeaway: you cannot get content CPU/memory out as OTel *metrics*.

## Traces: forwarded only when the span is marked `is_public`

Content spans are emitted and captured, but by default they are **retained inside
Connect** (visible through `GET /__api__/v1/content/{guid}/traces`) and are NOT
forwarded to the external endpoint. Forwarding is gated by the server setting
`OpenTelemetry.PublicTracesOnly`, which is on by default: only spans whose
`is_public` attribute is `true` are sent to the `[OTLPEndpoint]`. A content item
can set that attribute on its own span.

### Observed

Same content, same custom span `content.resource.sample`, the only difference is
the `is_public` attribute:

| `is_public` on the span | Reached external `[OTLPEndpoint]`? | In Connect traces API? |
| --- | --- | --- |
| not set (default) | **0 spans** (`spans-dropped-without-ispublic.txt`) | yes (`content-traces-api-sample.ndjson`) |
| `true` | **8 spans** (`spans-external-with-ispublic.txt`, `.sample.jsonl`) | yes |

The forwarded spans arrive under `service.name = posit-connect-content-<guid>`
with `content.guid` / `job.key` plus the CPU/memory attributes the content set.
Note that the `is_public` attribute itself is not present on the exported span
(grep the `.sample.jsonl`): Connect uses it only to decide what to forward and
removes it before the span leaves the server.

## Takeaway

Content CPU/memory cannot leave Connect as OTel *metrics*, but it can as a
*trace* attribute if the span is marked `is_public` (or if
`OpenTelemetry.PublicTracesOnly` is turned off server-wide, which forwards all
content spans). Both `is_public` and `PublicTracesOnly` are undocumented in
Connect 2026.10.0; confirm with Posit before relying on them long term.
