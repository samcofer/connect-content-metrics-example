library(plumber)

# IRD/SAS demo - Posit Connect per-content OpenTelemetry metrics export (R).
#
# When a publisher enables Traces (the otel_enabled content setting) and the
# Connect server has [OpenTelemetry] Enabled plus an [OTLPEndpoint] with
# Metrics = true, Connect installs and injects the R OpenTelemetry SDK
# (otelsdk) into this content and configures the default meter provider from
# the OTEL_* environment it sets in the worker. Custom metrics recorded through
# the otel API are exported to the configured OTLP endpoint (e.g. the local
# Datadog Agent) and auto-tagged with Connect's content resource attributes
# (content.guid, job.key, service.name, connect.node.name, service.namespace).
#
# Connect manages otelsdk/otel itself and adds them on the telemetry rebuild, so
# the otel package is absent until Traces is enabled. Guard on its presence so
# the content also deploys and runs cleanly with telemetry off.

has_otel <- requireNamespace("otel", quietly = TRUE)

if (has_otel) {
  meter <- otel::get_meter("ird.demo.workload")
  records <- meter$create_counter(
    "ird.demo.records_processed",
    description = "Records processed by the demo workload",
    unit = "{record}"
  )
  batch_hist <- meter$create_histogram(
    "ird.demo.batch_duration",
    description = "Per-batch processing time",
    unit = "ms"
  )
}

#* @apiTitle IRD Connect OTel metrics demo (R)

#* Health / environment probe
#* @get /
function() {
  list(
    service = "ird-otel-demo-r",
    otel_available = has_otel,
    otel_service_name = Sys.getenv("OTEL_SERVICE_NAME"),
    otel_endpoint = Sys.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
  )
}

#* Process a batch of work and emit custom OpenTelemetry metrics
#* @param batch:int Number of records to process
#* @get /work
function(batch = 1000) {
  batch <- as.integer(batch)
  t0 <- Sys.time()
  checksum <- sum(runif(batch))
  duration_ms <- as.numeric(difftime(Sys.time(), t0, units = "secs")) * 1000

  if (has_otel) {
    otel::counter_add(records, batch, attributes = list(dataset = "demo"))
    otel::histogram_record(
      batch_hist, duration_ms,
      attributes = list(dataset = "demo")
    )
  }

  list(
    processed = batch,
    duration_ms = round(duration_ms, 3),
    checksum = checksum
  )
}
