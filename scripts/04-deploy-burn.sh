#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
KEY=$(cat /tmp/connect_api_key)
SERVER=http://localhost:3939
mkdir -p /tmp/burn && cd /tmp/burn

echo "=== build burn env (numpy + jupyter stack) ==="
uv venv /tmp/burnenv >/dev/null
uv pip install --python /tmp/burnenv/bin/python -q numpy jupyter nbclient nbformat ipykernel

echo "=== generate burn.ipynb ==="
/tmp/burnenv/bin/python - <<'PY'
import nbformat as nbf
nb = nbf.v4.new_notebook()
code = '''import numpy as np, time
t0 = time.time()
# allocate ~1.5 GB resident
n = int(1.5e9 // 8)
big = np.ones(n, dtype=np.float64)
big += 1.0  # touch pages so RSS actually grows
print(f"allocated ~{big.nbytes/1e9:.2f} GB resident", flush=True)
# CPU-bound work for ~75s (single-core bound -> cpu_fraction ~1.0)
x = np.random.rand(1400, 1400)
iters = 0
while time.time() - t0 < 75:
    x = x @ x
    x = x / (np.linalg.norm(x) + 1e-12)
    iters += 1
print(f"completed {iters} matmul iterations in {time.time()-t0:.1f}s", flush=True)
print(f"checksum {float(x.sum()):.6f}")
'''
nb.cells = [nbf.v4.new_code_cell(code)]
nb.metadata = {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
               "language_info": {"name": "python", "version": "3.12"}}
nbf.write(nb, "/tmp/burn/burn.ipynb")
print("wrote burn.ipynb")
PY
echo "numpy" > /tmp/burn/requirements.txt

echo "=== deploy notebook to Connect (this runs the 75s render) ==="
rsconnect deploy notebook \
  --server "$SERVER" --api-key "$KEY" \
  --python /tmp/burnenv/bin/python \
  --title "cpu-mem-burn" \
  /tmp/burn/burn.ipynb 2>&1 | tail -25

echo "=== find content guid ==="
GUID=$(curl -s -H "Authorization: Key $KEY" "$SERVER/__api__/v1/content?search=cpu-mem-burn" | jq -r '.[0].guid // empty')
[ -z "$GUID" ] && GUID=$(curl -s -H "Authorization: Key $KEY" "$SERVER/__api__/v1/content" | jq -r '.[]|select(.title=="cpu-mem-burn")|.guid' | head -1)
echo "GUID=$GUID"
echo "$GUID" > /tmp/burn_guid
echo "=== jobs for this content ==="
curl -s -H "Authorization: Key $KEY" "$SERVER/__api__/v1/content/$GUID/jobs" | jq '.[]|{key,app_id,tag,status,start_time,end_time}' 2>/dev/null | head -40
echo "=== DONE 04 ==="
