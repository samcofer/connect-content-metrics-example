#!/usr/bin/env bash
set -euo pipefail
PRIV_IP="$(hostname -I | awk '{print $1}')"
cd /tmp
echo "=== wait for apt lock ==="
for i in $(seq 1 60); do
  if sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then sleep 8; else break; fi
done

echo "=== download runtime debs ==="
curl -fSL --retry 3 -o python-posit.deb "https://cdn.posit.co/python/ubuntu-2404/pkgs/python-3.12.11_1_amd64.deb"
curl -fSL --retry 3 -o r-posit.deb      "https://cdn.posit.co/r/ubuntu-2404/pkgs/r-4.5.1_1_amd64.deb"
curl -fSL --retry 3 -o quarto.deb       "https://github.com/quarto-dev/quarto-cli/releases/download/v1.10.19/quarto-1.10.19-linux-amd64.deb"

echo "=== install ==="
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ./python-posit.deb ./r-posit.deb ./quarto.deb >/dev/null
PYBIN=/opt/python/3.12.11/bin/python3
RBIN=/opt/R/4.5.1/bin/R
QBIN=$(command -v quarto || echo /opt/quarto/bin/quarto)
echo "python: $($PYBIN --version 2>&1)  @ $PYBIN"
echo "R: $($RBIN --version 2>&1 | head -1)  @ $RBIN"
echo "quarto: $($QBIN --version 2>&1)  @ $QBIN"

echo "=== rewrite gcfg with interpreters registered ==="
sudo tee /etc/rstudio-connect/rstudio-connect.gcfg >/dev/null <<GCFG
; Posit Connect config for metrics test
[Server]
Address = "http://${PRIV_IP}:3939"
DataDir = "/var/lib/rstudio-connect"
SenderEmail = "connect@example.local"
EmailProvider = "SMTP"

[SMTP]
Host = "127.0.0.1"
Port = 2525

[HTTP]
Listen = ":3939"

[Authentication]
Provider = "password"

[Bootstrap]
Enabled = true
SecretKeyFile = "/etc/rstudio-connect/bootstrap-secret.key"

[Applications]
MetricsCollectionEnabled = true

[Python]
Enabled = true
Executable = ${PYBIN}

[R]
Enabled = true
Executable = ${RBIN}

[Quarto]
Enabled = true
Executable = ${QBIN}

[Metrics]
Prometheus = true
GCFG

echo "=== restart Connect ==="
sudo systemctl restart rstudio-connect
for i in $(seq 1 20); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3939/__ping__)" = "200" ] && break; sleep 2
done
echo "ping: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:3939/__ping__)"

KEY=$(cat /tmp/connect_api_key)
echo "=== Connect-discovered interpreters ==="
echo "-- python --"; curl -s -H "Authorization: Key $KEY" http://localhost:3939/__api__/v1/server_settings/python | jq '.installations[]?|{version,cluster}' 2>/dev/null || curl -s -H "Authorization: Key $KEY" http://localhost:3939/__api__/v1/server_settings/python
echo "-- r --"; curl -s -H "Authorization: Key $KEY" http://localhost:3939/__api__/v1/server_settings/r | jq '.installations[]?|{version}' 2>/dev/null || true
echo "-- quarto --"; curl -s -H "Authorization: Key $KEY" http://localhost:3939/__api__/v1/server_settings/quarto | jq '.installations[]?|{version}' 2>/dev/null || true
echo "=== DONE 03 ==="
