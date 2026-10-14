# capacity_gate.R -- Linux-aware host capacity gate (base R only).
#
# wait_for_capacity(min_free_gb, min_free_cores, poll_seconds, deadline_elapsed) blocks
# until MemAvailable >= min_free_gb (decimal GB) AND estimated free cores >= min_free_cores,
# or until the shared elapsed deadline passes. Returns a list with
# ok/unknown/timed_out/mem_gb/cores/waited. On a non-Linux host (no /proc) it reports
# unknown = TRUE so the caller can decide (gate what you cannot measure is impossible).
#
# Elapsed time uses proc.time()[["elapsed"]] (measured from R start), which does not move
# when the wall clock is stepped by NTP. A max-iteration backstop prevents an infinite loop.
#
# Example:
#   source("capacity_gate.R")
#   dec <- wait_for_capacity(4, 2, 5, .elapsed() + 120)
#   if (dec$ok) { ...heavy work... }

.elapsed <- function() as.numeric(proc.time()[["elapsed"]])

.read_meminfo_avail_bytes <- function() {
  if (!file.exists("/proc/meminfo")) return(NA_real_)
  mi <- tryCatch(readLines("/proc/meminfo"), error = function(e) character(0))
  line <- grep("^MemAvailable:", mi, value = TRUE)
  if (!length(line)) return(NA_real_)
  as.numeric(gsub("[^0-9]", "", line)) * 1024
}

.cgroup_v2_mem_avail_bytes <- function() {
  mx <- "/sys/fs/cgroup/memory.max"; cur <- "/sys/fs/cgroup/memory.current"
  if (!file.exists(mx) || !file.exists(cur)) return(NA_real_)
  mxv <- tryCatch(readLines(mx, n = 1), error = function(e) "max")
  if (identical(mxv, "max") || !nzchar(mxv)) return(NA_real_)
  curv <- tryCatch(as.numeric(readLines(cur, n = 1)), error = function(e) NA_real_)
  if (is.na(curv)) return(NA_real_)
  max(0, as.numeric(mxv) - curv)
}

mem_available_bytes <- function() {
  vals <- c(.read_meminfo_avail_bytes(), .cgroup_v2_mem_avail_bytes())
  vals <- vals[!is.na(vals)]
  if (!length(vals)) NA_real_ else min(vals)  # tightest binding constraint
}

.read_proc_stat_totals <- function() {
  if (!file.exists("/proc/stat")) return(NULL)
  l <- tryCatch(readLines("/proc/stat", n = 1), error = function(e) NA_character_)
  if (is.na(l[1]) || !grepl("^cpu ", l[1])) return(NULL)
  v <- as.numeric(strsplit(trimws(sub("^cpu ", "", l[1])), "\\s+")[[1]])
  list(idle = sum(v[c(4, 5)]), total = sum(v))  # idle + iowait
}

.ncpu_effective <- function() {
  n <- tryCatch(parallel::detectCores(logical = TRUE), error = function(e) NA_integer_)
  if (is.na(n)) n <- 1L
  cm <- "/sys/fs/cgroup/cpu.max"
  if (file.exists(cm)) {
    p <- tryCatch(strsplit(readLines(cm, n = 1), "\\s+")[[1]], error = function(e) character(0))
    if (length(p) == 2 && p[1] != "max") {
      q <- suppressWarnings(as.numeric(p[1])); per <- suppressWarnings(as.numeric(p[2]))
      if (!is.na(q) && !is.na(per) && per > 0) n <- min(n, max(1, floor(q / per)))
    }
  }
  n
}

cpu_free_cores <- function(sample_seconds = 0.5) {
  a <- .read_proc_stat_totals(); if (is.null(a)) return(NA_real_)
  Sys.sleep(sample_seconds)
  b <- .read_proc_stat_totals(); if (is.null(b)) return(NA_real_)
  dt <- b$total - a$total; di <- b$idle - a$idle
  if (dt <= 0) return(NA_real_)
  util <- min(1, max(0, 1 - di / dt))
  .ncpu_effective() * (1 - util)
}

wait_for_capacity <- function(min_free_gb = 4, min_free_cores = 2, poll_seconds = 5,
                              deadline_elapsed = NULL, log = TRUE) {
  GB <- 1e9; start <- .elapsed()
  if (is.null(deadline_elapsed)) deadline_elapsed <- start + 120
  max_iter <- as.integer((deadline_elapsed - start) / max(0.1, poll_seconds)) + 5L
  it <- 0L
  repeat {
    it <- it + 1L
    mem_b <- mem_available_bytes(); cores <- cpu_free_cores()
    mem_gb <- if (is.na(mem_b)) NA_real_ else mem_b / GB
    unknown <- is.na(mem_gb) || is.na(cores)
    ok <- !unknown && mem_gb >= min_free_gb && cores >= min_free_cores
    if (log) message(sprintf(
      "[capacity/R] t+%4.0fs free_mem=%s GB free_cores=%s need(>=%gGB,>=%g) -> %s",
      .elapsed() - start,
      if (is.na(mem_gb)) "NA" else sprintf("%.2f", mem_gb),
      if (is.na(cores)) "NA" else sprintf("%.2f", cores),
      min_free_gb, min_free_cores,
      if (unknown) "UNKNOWN" else if (ok) "OK" else "WAIT"))
    if (unknown) return(list(ok = FALSE, unknown = TRUE, timed_out = FALSE, mem_gb = mem_gb, cores = cores, waited = .elapsed() - start))
    if (ok)      return(list(ok = TRUE,  unknown = FALSE, timed_out = FALSE, mem_gb = mem_gb, cores = cores, waited = .elapsed() - start))
    if (.elapsed() >= deadline_elapsed || it >= max_iter)
                 return(list(ok = FALSE, unknown = FALSE, timed_out = TRUE,  mem_gb = mem_gb, cores = cores, waited = .elapsed() - start))
    Sys.sleep(poll_seconds)
  }
}
