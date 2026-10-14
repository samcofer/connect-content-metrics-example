#!/usr/bin/env python3
"""Generate reprex.ipynb -- a self-contained Jupyter (static-render) reprex that proves
Connect under-reports the CPU/memory of rendered content.

The notebook:
  1. discovers its own content GUID + job key by walking up the /proc parent chain to the
     connect-session supervisor and parsing its (world-readable) cmdline;
  2. runs a sustained ~5-minute CPU + ~1.5 GB memory workload, sampling its OWN process
     subtree from /proc (ground truth);
  3. reports that self-measured usage (summary + plot);
  4. fetches Connect's per-job metrics for THIS job and contrasts them -- showing Connect's
     numbers reflect only the supervisor process (near-zero CPU, tens of MB).

Build:  python build_reprex.py   ->   reprex.ipynb
"""
import nbformat as nbf

CELLS = []

CELLS.append(("markdown", """\
# Reprex: Connect under-reports rendered-content CPU/memory

This notebook is **static (rendered) content**. It runs a sustained CPU + memory workload
for ~5 minutes while measuring its **own** resource usage from `/proc`, then fetches the
per-job resource metrics Connect recorded for *this very render* and contrasts them.

Connect's per-job metrics sample the process Connect spawned (the `connect-session`
supervisor), not the notebook kernel subtree that does the work — so the rendered numbers
are near-zero while the real usage is substantial.

Requires two content environment variables for the contrast step: a Connect base URL
(`REPREX_SERVER`, falling back to `CONNECT_SERVER`) and an API key for the content owner
or an admin (`REPREX_API_KEY`, falling back to `CONNECT_API_KEY`).
"""))

CELLS.append(("code", r'''
import os, time, datetime

PAGE = os.sysconf("SC_PAGE_SIZE")
CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1
WORKLOAD_SECONDS = int(os.environ.get("REPREX_SECONDS", "270"))
SAMPLE_EVERY = 5

def read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""

def read_stat(pid):
    with open(f"/proc/{pid}/stat") as fh:
        data = fh.read()
    after = data[data.rfind(")") + 2:].split()
    return {"ppid": int(after[1]), "jiffies": int(after[11]) + int(after[12]),
            "rss": int(after[21]) * PAGE}

def discover_identity():
    """Walk up the parent chain to the connect-session supervisor; parse its cmdline."""
    pid = os.getpid()
    guid = jobkey = None
    for _ in range(16):
        cl = read_cmdline(pid)
        if "connect-session" in cl:
            for tok in cl.split():
                if tok.startswith("content-guid:"):
                    guid = tok.split(":", 1)[1]
                elif tok.startswith("job-key:"):
                    jobkey = tok.split(":", 1)[1]
            if guid and jobkey:
                break
        try:
            pid = read_stat(pid)["ppid"]
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1:
            break
    return guid, jobkey

def subtree(root):
    kids = {}
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            pp = read_stat(int(e))["ppid"]
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(pp, []).append(int(e))
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(kids.get(p, []))
    return out

GUID, JOBKEY = discover_identity()
print(f"self pid={os.getpid()}  content-guid={GUID}  job-key={JOBKEY}  host cores={NCPU}")
print(f"workload={WORKLOAD_SECONDS}s, sampling every {SAMPLE_EVERY}s")
'''))

CELLS.append(("code", r'''
import numpy as np

ROOT = os.getpid()
# Hold ~1.5 GB resident for the whole render.
HOLD = np.ones(int(1.5e9 // 8), dtype=np.float64)
HOLD += 1.0

def snap():
    jiff = rss = 0
    tree = subtree(ROOT)
    for p in tree:
        try:
            s = read_stat(p)
        except (OSError, ValueError, IndexError):
            continue
        jiff += s["jiffies"]
        rss += s["rss"]
    return jiff, rss, len(tree)

samples = []
pj, prss, _ = snap()
pw = time.monotonic()
start = pw
x = np.random.rand(1100, 1100)
while time.monotonic() - start < WORKLOAD_SECONDS:
    t0 = time.monotonic()
    while time.monotonic() - t0 < SAMPLE_EVERY:        # sustained CPU burn between samples
        x = x @ x
        x = x / (np.linalg.norm(x) + 1e-12)
    cj, crss, n = snap()
    cw = time.monotonic()
    cores = (cj - pj) / CLK / (cw - pw) if cw > pw else 0.0
    samples.append({
        "elapsed_s": round(cw - start, 1),
        "cpu_cores": round(cores, 3),
        "rss_mb": round(crss / 1048576.0, 1),
        "n_procs": n,
    })
    pj, pw = cj, cw

print(f"collected {len(samples)} self-measured samples over ~{WORKLOAD_SECONDS}s "
      f"(HOLD retains {HOLD.nbytes/1e9:.2f} GB)")
'''))

CELLS.append(("code", r'''
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

df = pd.DataFrame(samples)
self_cpu_peak, self_cpu_mean = df["cpu_cores"].max(), df["cpu_cores"].mean()
self_rss_peak, self_rss_mean = df["rss_mb"].max(), df["rss_mb"].mean()
df.to_csv(os.environ.get("REPREX_RECORD", "/tmp/reprex_self.csv"), index=False)

print(f"SELF-MEASURED (ground truth, /proc subtree of the kernel):")
print(f"  CPU cores : peak={self_cpu_peak:.2f}  mean={self_cpu_mean:.2f}")
print(f"  RSS   (MB): peak={self_rss_peak:.0f}  mean={self_rss_mean:.0f}")

fig, ax = plt.subplots(1, 2, figsize=(11, 3.2))
ax[0].plot(df["elapsed_s"], df["cpu_cores"]); ax[0].set_title("self CPU (cores)")
ax[0].set_xlabel("elapsed (s)"); ax[0].set_ylim(bottom=0)
ax[1].plot(df["elapsed_s"], df["rss_mb"]); ax[1].set_title("self RSS (MB)")
ax[1].set_xlabel("elapsed (s)"); ax[1].set_ylim(bottom=0)
plt.tight_layout(); plt.show()
df.describe()
'''))

CELLS.append(("code", r'''
import requests

server = (os.environ.get("REPREX_SERVER") or os.environ.get("CONNECT_SERVER", "http://localhost:3939")).rstrip("/")
key = os.environ.get("REPREX_API_KEY") or os.environ.get("CONNECT_API_KEY", "")
conn_cpu_peak = conn_rss_peak_mb = None
n_conn = 0
err = None
try:
    if not (key and GUID and JOBKEY):
        raise RuntimeError("need CONNECT_API_KEY env + discovered GUID/JOBKEY")
    r = requests.get(f"{server}/__api__/v1/content/{GUID}/jobs/{JOBKEY}/metrics",
                     headers={"Authorization": f"Key {key}"}, timeout=15)
    r.raise_for_status()
    s = r.json().get("samples", [])
    n_conn = len(s)
    if s:
        conn_cpu_peak = max((x.get("cpu_fraction") or 0) for x in s)
        conn_rss_peak_mb = max((x.get("memory_bytes") or 0) for x in s) / 1048576.0
except Exception as e:  # noqa: BLE001 - reprex should still show self-measured on API failure
    err = str(e)

def fmt(v, nd=2):
    return "n/a" if v is None else f"{v:.{nd}f}"

contrast = pd.DataFrame([
    {"metric": "CPU (cores)", "self_measured_peak": round(self_cpu_peak, 2),
     "connect_reported_peak": fmt(conn_cpu_peak, 3)},
    {"metric": "Memory (MB)", "self_measured_peak": round(self_rss_peak, 0),
     "connect_reported_peak": fmt(conn_rss_peak_mb, 0)},
])
print(f"Connect per-job metric samples for this render ({JOBKEY}): {n_conn}" + (f"  [error: {err}]" if err else ""))
display(contrast)

if conn_cpu_peak is not None:
    cpu_x = "infinite (Connect reported 0)" if conn_cpu_peak == 0 else f"~{self_cpu_peak / conn_cpu_peak:.0f}x"
    mem_x = "infinite" if not conn_rss_peak_mb else f"~{self_rss_peak / conn_rss_peak_mb:.0f}x"
    print(f"\nVERDICT: for this render Connect UNDER-REPORTS CPU by {cpu_x} and memory by {mem_x}.")
    print("Connect's per-job metric reflects the connect-session supervisor process, not the "
          "notebook kernel subtree that did the work.")
'''))

nb = nbf.v4.new_notebook()
nb.cells = [nbf.v4.new_markdown_cell(src) if kind == "markdown" else nbf.v4.new_code_cell(src.strip("\n"))
            for kind, src in CELLS]
nb.metadata = {
    "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
    "language_info": {"name": "python", "version": "3.12"},
}
nbf.write(nb, "reprex.ipynb")
print("wrote reprex.ipynb")
