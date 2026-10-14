#!/usr/bin/env bash
set -euo pipefail
PRIV_IP="$(hostname -I | awk '{print $1}')"
DEB_URL="https://cdn.posit.co/connect/2026.10/rstudio-connect_2026.10.0~ubuntu24_amd64.deb"
echo "=== private IP: $PRIV_IP ==="

echo "=== wait for apt/dpkg lock ==="
for i in $(seq 1 60); do
  if sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || sudo fuser /var/lib/apt/lists/lock >/dev/null 2>&1; then
    echo "lock held; waiting ($i)..."; sleep 10
  else break; fi
done

echo "=== apt deps ==="
sudo DEBIAN_FRONTEND=noninteractive apt-get update -y -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl gdebi-core jq >/dev/null

echo "=== download Connect deb ==="
cd /tmp
if [ ! -f rstudio-connect.deb ]; then curl -fSL --retry 3 -o rstudio-connect.deb "$DEB_URL"; fi
ls -lh rstudio-connect.deb

echo "=== install Connect ==="
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /tmp/rstudio-connect.deb
/opt/rstudio-connect/bin/connect --version || true

echo "=== activate license ==="
if [ -f /tmp/PCT.lic ]; then
  sudo /opt/rstudio-connect/bin/license-manager activate-file /tmp/PCT.lic
else
  echo "WARN: /tmp/PCT.lic not found"
fi

echo "=== write gcfg ==="
sudo tee /etc/rstudio-connect/rstudio-connect.gcfg >/dev/null <<GCFG
; Posit Connect config for metrics test
[Server]
Address = "http://${PRIV_IP}:3939"
DataDir = "/var/lib/rstudio-connect"

[HTTP]
Listen = ":3939"

[Authentication]
Provider = "password"

[Applications]
; KEY SETTING for per-content CPU/memory job metrics (test 1)
MetricsCollectionEnabled = true

[Python]
Enabled = true

[R]
Enabled = true

[Metrics]
; expose Prometheus endpoint on localhost for cross-checking
Prometheus = true
GCFG

echo "=== restart Connect ==="
sudo systemctl restart rstudio-connect
sleep 8
sudo systemctl is-active rstudio-connect || (sudo journalctl -u rstudio-connect --no-pager -n 60; exit 1)

echo "=== health + license status ==="
curl -s -o /dev/null -w "ping: %{http_code}\n" http://localhost:3939/__ping__
sudo /opt/rstudio-connect/bin/license-manager status | grep -iE 'status|expiration|anonymous|users|tier' || true
echo "=== metrics_collection in server settings ==="
curl -s http://localhost:3939/__api__/v1/server_settings | jq '{version, metrics_collection_enabled: .metrics_collection_enabled}' 2>/dev/null || true
echo "=== DONE 01 ==="
