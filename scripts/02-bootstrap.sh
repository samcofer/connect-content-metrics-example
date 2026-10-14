#!/usr/bin/env bash
set -euo pipefail
PRIV_IP="$(hostname -I | awk '{print $1}')"

echo "=== install uv ==="
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

echo "=== tool venv: rsconnect + aiosmtpd ==="
sudo mkdir -p /opt/connect-tools
sudo chown "$(whoami)" /opt/connect-tools
uv venv /opt/connect-tools >/dev/null
uv pip install --python /opt/connect-tools/bin/python -q rsconnect-python aiosmtpd
/opt/connect-tools/bin/rsconnect version

echo "=== dummy SMTP sink service (127.0.0.1:2525) ==="
sudo tee /etc/systemd/system/dummy-smtp.service >/dev/null <<SVC
[Unit]
Description=Dummy SMTP sink for Connect bootstrap
After=network.target
[Service]
ExecStart=/opt/connect-tools/bin/python -m aiosmtpd -n -l 127.0.0.1:2525
Restart=always
User=$(whoami)
[Install]
WantedBy=multi-user.target
SVC
sudo systemctl daemon-reload
sudo systemctl enable --now dummy-smtp
sleep 2
sudo systemctl is-active dummy-smtp
# prove it accepts mail
python3 - <<'PY'
import smtplib
s=smtplib.SMTP("127.0.0.1",2525,timeout=5)
s.sendmail("a@b.c",["d@e.f"],"Subject: test\n\nhi")
s.quit()
print("SMTP sink accepted a test message")
PY

echo "=== bootstrap secret key ==="
head -c 32 /dev/urandom | base64 | sudo tee /etc/rstudio-connect/bootstrap-secret.key >/dev/null
sudo chmod 644 /etc/rstudio-connect/bootstrap-secret.key
echo "key bytes: $(sudo base64 -d /etc/rstudio-connect/bootstrap-secret.key | wc -c)"

echo "=== patch gcfg: email + bootstrap ==="
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

[R]
Enabled = true

[Metrics]
Prometheus = true
GCFG

echo "=== restart Connect ==="
sudo systemctl restart rstudio-connect
for i in $(seq 1 20); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3939/__ping__ || true)
  [ "$code" = "200" ] && break; sleep 2
done
echo "ping: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:3939/__ping__)"

echo "=== rsconnect bootstrap ==="
export PATH="$HOME/.local/bin:$PATH"
set +e
API_KEY=$(/opt/connect-tools/bin/rsconnect bootstrap --raw \
  --jwt-keypath /etc/rstudio-connect/bootstrap-secret.key \
  --server http://localhost:3939 2>/tmp/bootstrap.err)
rc=$?
set -e
if [ $rc -ne 0 ] || [ -z "$API_KEY" ]; then
  echo "BOOTSTRAP FAILED (rc=$rc):"; cat /tmp/bootstrap.err; exit 1
fi
echo "$API_KEY" > /tmp/connect_api_key
chmod 600 /tmp/connect_api_key
echo "BOOTSTRAP OK; api key length: ${#API_KEY}"

echo "=== verify key: whoami ==="
curl -s -H "Authorization: Key ${API_KEY}" http://localhost:3939/__api__/v1/user | jq '{username, user_role, email, locked}' 2>/dev/null || \
  curl -s -H "Authorization: Key ${API_KEY}" http://localhost:3939/__api__/v1/user
echo "=== DONE 02 ==="
