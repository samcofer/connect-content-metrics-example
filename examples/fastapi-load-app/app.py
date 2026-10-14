"""Interactive FastAPI content for Connect per-content CPU/memory metrics testing.

The worker process holds a ~1.2 GB resident array for its lifetime (so memory
samples are large and steady) and exposes /burn to drive CPU to ~1 core.
connect-session monitors this uvicorn worker directly, so the per-job metrics
reflect real resource usage.
"""
import time

import numpy as np
from fastapi import FastAPI

app = FastAPI()

# Module-level: allocated once per worker, held for the worker's lifetime.
# ~1.2 GB resident (touched so pages are actually resident, not lazy).
_HOLD = np.ones(int(1.2e9 // 8), dtype=np.float64)
_HOLD += 1.0


@app.get("/")
def root():
    return {"status": "ok", "held_gb": round(_HOLD.nbytes / 1e9, 2)}


@app.get("/burn")
def burn(seconds: int = 20):
    """Spin a single core for `seconds` with dense matmul."""
    end = time.time() + seconds
    x = np.random.rand(1200, 1200)
    iters = 0
    while time.time() < end:
        x = x @ x
        x = x / (np.linalg.norm(x) + 1e-12)
        iters += 1
    return {"iters": iters, "seconds": seconds, "held_gb": round(_HOLD.nbytes / 1e9, 2)}
