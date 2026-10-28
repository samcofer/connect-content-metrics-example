"""
IRD/SAS demo - Posit Connect per-content OpenTelemetry export (Python).

Demonstrates the *documented* content observability path: the bundle does NOT
ship its own opentelemetry-sdk, so when a publisher enables Traces (the
`otel_enabled` content setting) Connect injects its shared OpenTelemetry SDK +
auto-instrumentation onto the worker and sets the standard OTEL_* environment.

With that in place:
  - FastAPI request **traces** are emitted automatically (zero code) and are
    exported to the server's configured OTLP endpoint (e.g. the Datadog Agent),
    tagged with the content resource attributes (service.name =
    posit-connect-content-<guid>, content.guid, job.key, ...).
  - Custom **metrics** recorded through the injected OpenTelemetry API
    (below) are accepted by Connect's embedded collector, BUT as of Connect
    2026.10.0 there is no content-metrics pipeline, so they are filtered out
    before the external endpoint. See the folder README for the proof.

The opentelemetry import is guarded so the content also deploys and runs when
telemetry is disabled (the injected SDK is only present once Traces is on).
"""

import os
import random
import time

from fastapi import FastAPI

try:
    from opentelemetry import metrics

    _OTEL = True
except Exception:  # noqa: BLE001 - telemetry simply off
    _OTEL = False

if _OTEL:
    _meter = metrics.get_meter("ird.demo.workload")
    _records = _meter.create_counter(
        "ird.demo.records_processed",
        unit="{record}",
        description="Records processed by the demo workload",
    )
    _batch_duration = _meter.create_histogram(
        "ird.demo.batch_duration",
        unit="ms",
        description="Per-batch processing time",
    )

    def _rss_bytes() -> int:
        try:
            with open("/proc/self/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
        return 0

    from opentelemetry.metrics import Observation

    _meter.create_observable_gauge(
        "ird.demo.worker_rss_bytes",
        unit="By",
        description="Resident set size of this content worker process",
        callbacks=[lambda _options: [Observation(_rss_bytes())]],
    )

app = FastAPI(title="IRD Connect OTel metrics demo (Python)")


@app.get("/")
def root():
    mp = type(metrics.get_meter_provider()).__name__ if _OTEL else None
    return {
        "service": "ird-otel-demo-py",
        "otel_available": _OTEL,
        "otel_service_name": os.environ.get("OTEL_SERVICE_NAME"),
        "otel_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        "meter_provider": mp,
        "hint": "GET /work?batch=20000 to emit a trace and custom metrics",
    }


@app.get("/work")
def work(batch: int = 1000):
    start = time.perf_counter()
    acc = 0.0
    for _ in range(batch):
        acc += random.random()
    duration_ms = (time.perf_counter() - start) * 1000.0
    if _OTEL:
        _records.add(batch, {"dataset": "demo"})
        _batch_duration.record(duration_ms, {"dataset": "demo"})
    return {"processed": batch, "duration_ms": round(duration_ms, 3), "checksum": acc}
