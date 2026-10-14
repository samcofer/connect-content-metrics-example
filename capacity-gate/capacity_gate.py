"""Linux-aware host capacity gate (psutil-optional, cgroup v2 + affinity aware).

wait_for_capacity(min_free_gb, min_free_cores, poll_seconds, max_wait_seconds) blocks
until MemAvailable >= min_free_gb (decimal GB) AND estimated free cores >= min_free_cores,
or until max_wait_seconds elapses (monotonic clock). Returns a dict with ok/timed_out/
unknown/mem_gb/cores/waited. On a non-Linux host with no psutil it reports unknown=True.
"""
import os, time

def _meminfo_avail_bytes():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None

def _cgroup_v2_mem_avail_bytes():
    try:
        with open("/sys/fs/cgroup/memory.max") as fh:
            mx = fh.read().strip()
        if mx in ("", "max"):
            return None
        with open("/sys/fs/cgroup/memory.current") as fh:
            cur = int(fh.read().strip())
        return max(0, int(mx) - cur)
    except (OSError, ValueError):
        return None

def mem_available_bytes():
    vals = []
    try:
        import psutil
        vals.append(psutil.virtual_memory().available)
    except Exception:
        m = _meminfo_avail_bytes()
        if m is not None:
            vals.append(m)
    cg = _cgroup_v2_mem_avail_bytes()
    if cg is not None:
        vals.append(cg)
    return min(vals) if vals else None

def _ncpu_effective():
    try:
        n = len(os.sched_getaffinity(0))   # respects cpuset / affinity
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    try:
        with open("/sys/fs/cgroup/cpu.max") as fh:
            parts = fh.read().split()
        if len(parts) == 2 and parts[0] != "max":
            quota, period = float(parts[0]), float(parts[1])
            if period > 0:
                n = min(n, max(1, int(quota // period)))
    except (OSError, ValueError):
        pass
    return n

def _cpu_util_fraction(sample_seconds=0.5):
    try:
        import psutil
        return min(1.0, max(0.0, psutil.cpu_percent(interval=sample_seconds) / 100.0))
    except Exception:
        pass
    def read():
        try:
            with open("/proc/stat") as fh:
                parts = fh.readline().split()
        except OSError:
            return None
        if not parts or parts[0] != "cpu":
            return None
        v = [float(x) for x in parts[1:]]
        idle = v[3] + (v[4] if len(v) > 4 else 0.0)
        return idle, sum(v)
    a = read()
    if a is None:
        return None
    time.sleep(sample_seconds)
    b = read()
    if b is None:
        return None
    dt = b[1] - a[1]
    di = b[0] - a[0]
    return min(1.0, max(0.0, 1 - di / dt)) if dt > 0 else None

def cpu_free_cores(sample_seconds=0.5):
    util = _cpu_util_fraction(sample_seconds)
    return None if util is None else _ncpu_effective() * (1 - util)

def wait_for_capacity(min_free_gb, min_free_cores, poll_seconds, max_wait_seconds, log=True):
    GB = 1e9
    start = time.monotonic()               # monotonic: immune to clock steps
    deadline = start + max_wait_seconds
    while True:
        mem_b = mem_available_bytes()
        cores = cpu_free_cores()
        mem_gb = None if mem_b is None else mem_b / GB
        unknown = mem_gb is None or cores is None
        ok = (not unknown) and mem_gb >= min_free_gb and cores >= min_free_cores
        if log:
            print("[capacity/py] t+%4.0fs free_mem=%s GB free_cores=%s need(>=%gGB,>=%g) -> %s" % (
                time.monotonic() - start,
                "NA" if mem_gb is None else "%.2f" % mem_gb,
                "NA" if cores is None else "%.2f" % cores,
                min_free_gb, min_free_cores,
                "UNKNOWN" if unknown else ("OK" if ok else "WAIT")), flush=True)
        if unknown:
            return {"ok": False, "unknown": True, "timed_out": False, "mem_gb": mem_gb, "cores": cores, "waited": time.monotonic() - start}
        if ok:
            return {"ok": True, "unknown": False, "timed_out": False, "mem_gb": mem_gb, "cores": cores, "waited": time.monotonic() - start}
        if time.monotonic() >= deadline:
            return {"ok": False, "unknown": False, "timed_out": True, "mem_gb": mem_gb, "cores": cores, "waited": time.monotonic() - start}
        time.sleep(poll_seconds)
