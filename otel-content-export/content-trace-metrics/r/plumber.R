library(plumber)

# IRD/SAS demo - embedding per-content resource metrics in OTel TRACES (R).
#
# This is the R parallel of ../python/app.py. The content opens a custom span per
# request, records its own CPU/memory as span attributes, and marks the span
# is_public = TRUE so Connect's embedded collector forwards it to the configured
# [OTLPEndpoint] (the customer trace pipeline keeps only is_public spans when
# OpenTelemetry.PublicTracesOnly is enabled, which is the default).
#
# NOTE: R content telemetry needs the otelsdk package, which Connect installs on
# the telemetry rebuild. otelsdk is Rust-backed; on a host without a Rust
# toolchain (or a matching prebuilt binary) its install fails and get_tracer()
# is a no-op. This file was NOT live-verified in the lab (otelsdk did not install
# on the vanilla test host); the Python example is the verified reference. The
# otel R tracing API used here is otel::get_tracer / start_span / end_span.

has_otel <- requireNamespace("otel", quietly = TRUE)
CLK_TCK <- 100 # Linux default; SC_CLK_TCK

self_resource <- function() {
  out <- list(
    cpu_user_seconds = 0, cpu_system_seconds = 0,
    rss_bytes = 0, num_threads = 0L
  )
  stat <- tryCatch(readLines("/proc/self/stat", warn = FALSE), error = function(e) NULL)
  if (!is.null(stat)) {
    after <- strsplit(sub("^.*\\)\\s", "", stat), "\\s+")[[1]]
    out$cpu_user_seconds <- as.numeric(after[12]) / CLK_TCK # utime
    out$cpu_system_seconds <- as.numeric(after[13]) / CLK_TCK # stime
    out$num_threads <- as.integer(after[18])
  }
  status <- tryCatch(readLines("/proc/self/status", warn = FALSE), error = function(e) NULL)
  vmrss <- grep("^VmRSS:", status, value = TRUE)
  if (length(vmrss)) {
    out$rss_bytes <- as.numeric(strsplit(vmrss, "\\s+")[[1]][2]) * 1024
  }
  out
}

#* @apiTitle IRD Connect OTel trace-embedded metrics demo (R)

#* @get /
function() {
  list(
    service = "ird-otel-trace-demo-r",
    otel_available = has_otel,
    otel_service_name = Sys.getenv("OTEL_SERVICE_NAME")
  )
}

#* Process a batch and emit a custom span carrying CPU/memory
#* @param batch:int Number of records
#* @get /work
function(batch = 1000) {
  batch <- as.integer(batch)
  t0 <- Sys.time()
  checksum <- sum(runif(batch))
  duration_ms <- as.numeric(difftime(Sys.time(), t0, units = "secs")) * 1000

  if (has_otel) {
    res <- self_resource()
    span <- otel::start_span(
      "content.resource.sample",
      attributes = list(
        is_public = TRUE,
        "content.cpu.user_seconds" = res$cpu_user_seconds,
        "content.cpu.system_seconds" = res$cpu_system_seconds,
        "content.memory.rss_bytes" = res$rss_bytes,
        "content.process.num_threads" = res$num_threads,
        "workload.batch" = batch,
        "workload.duration_ms" = duration_ms
      ),
      tracer = "ird.content.resource"
    )
    otel::end_span(span)
  }

  list(processed = batch, duration_ms = round(duration_ms, 3), checksum = checksum)
}
