"""
IRD/SAS demo - embedding per-content resource metrics in OpenTelemetry TRACES.

Posit Connect does not forward content-emitted OTel *metrics* to the external
endpoint, so we test the other signal: have the content define its own custom
spans that carry the content's CPU/memory as span attributes. Connect
auto-instruments content and provides the OTel SDK, so `trace.get_tracer(...)`
returns a working tracer and the spans are ingested by Connect along with the
auto-instrumented request spans.

Each call to /work opens a custom span `content.resource.sample` and records the
content worker's own CPU time, resident memory, and thread count (read from
/proc/self) as span attributes. If Connect forwards content traces to the
configured [OTLPEndpoint], these land in Datadog carrying the content resource
attributes (content.guid, job.key, ...) plus our resource numbers.

The opentelemetry import is guarded so the content still deploys and runs when
Traces are disabled (the injected SDK is only present once Traces are on).
"""

import os
import random
import time

from fastapi import FastAPI

try:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, StatusCode

    _OTEL = True
    _tracer = trace.get_tracer("ird.content.resource")
except Exception:  # noqa: BLE001 - telemetry simply off
    _OTEL = False

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _self_resource() -> dict:
    """This worker's own CPU seconds, RSS bytes, and thread count from /proc."""
    out = {"cpu_user_seconds": 0.0, "cpu_system_seconds": 0.0, "rss_bytes": 0, "num_threads": 0}
    try:
        with open("/proc/self/stat") as fh:
            data = fh.read()
        after = data[data.rfind(")") + 2:].split()
        out["cpu_user_seconds"] = int(after[11]) / _CLK_TCK  # utime
        out["cpu_system_seconds"] = int(after[12]) / _CLK_TCK  # stime
        out["num_threads"] = int(after[17])
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    out["rss_bytes"] = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass
    return out


app = FastAPI(title="IRD Connect OTel trace-embedded metrics demo (Python)")


@app.get("/")
def root():
    return {
        "service": "ird-otel-trace-demo-py",
        "otel_available": _OTEL,
        "otel_service_name": os.environ.get("OTEL_SERVICE_NAME"),
        "otel_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        "hint": "GET /work?batch=200000 to emit a custom span carrying CPU/memory",
    }


@app.get("/work")
def work(batch: int = 1000):
    if not _OTEL:
        return _do_work(batch)
    # A custom span carrying this content's resource usage as attributes.
    with _tracer.start_as_current_span(
        "content.resource.sample", kind=SpanKind.INTERNAL
    ) as span:
        result = _do_work(batch)
        res = _self_resource()
        # Opt this span into Connect's external (customer) trace pipeline. With
        # OpenTelemetry.PublicTracesOnly enabled (the default), the embedded
        # collector only forwards spans whose is_public attribute is true to the
        # configured [OTLPEndpoint]; everything else is retained on-host.
        span.set_attribute("is_public", True)
        span.set_attribute("content.cpu.user_seconds", res["cpu_user_seconds"])
        span.set_attribute("content.cpu.system_seconds", res["cpu_system_seconds"])
        span.set_attribute("content.memory.rss_bytes", res["rss_bytes"])
        span.set_attribute("content.process.num_threads", res["num_threads"])
        span.set_attribute("workload.batch", batch)
        span.set_attribute("workload.duration_ms", result["duration_ms"])
        span.set_status(StatusCode.OK)
        return {**result, "resource": res}


def _do_work(batch: int) -> dict:
    start = time.perf_counter()
    acc = 0.0
    for _ in range(batch):
        acc += random.random()
    return {"processed": batch, "duration_ms": round((time.perf_counter() - start) * 1000.0, 3)}
