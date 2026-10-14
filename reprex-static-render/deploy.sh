#!/usr/bin/env bash
# Deploy the static-render under-reporting reprex to Connect and render it for ~5 minutes.
#
#   CONNECT_SERVER   Connect base URL
#   CONNECT_API_KEY  a PUBLISHER key (used to deploy)
#   REPREX_API_KEY   an owner/admin key the reprex uses to read ITS OWN job metrics
#   REPREX_SECONDS   workload seconds (default 270; keep < RuntimeSessionStdinTimeout, default 300s)
set -euo pipefail
: "${CONNECT_SERVER:?set CONNECT_SERVER}"
: "${CONNECT_API_KEY:?set CONNECT_API_KEY (publisher)}"
: "${REPREX_API_KEY:?set REPREX_API_KEY (owner/admin key)}"

python build_reprex.py   # -> reprex.ipynb

rsconnect deploy notebook \
  --server "$CONNECT_SERVER" --api-key "$CONNECT_API_KEY" \
  -E REPREX_SERVER="$CONNECT_SERVER" \
  -E REPREX_API_KEY="$REPREX_API_KEY" \
  -E REPREX_SECONDS="${REPREX_SECONDS:-270}" \
  --title "static-render-underreport-reprex" \
  reprex.ipynb

# NOTE: rsconnect applies -E environment variables AFTER the initial deploy-time render,
# so that first render self-measures but has no key for the contrast step. Trigger one more
# render (env now present) to get the full self-vs-Connect contrast in the rendered output:
#
#   guid=$(curl -s -H "Authorization: Key $CONNECT_API_KEY" \
#     "$CONNECT_SERVER/__api__/v1/content?search=static-render-underreport-reprex" | jq -r '.[0].guid')
#   curl -s -H "Authorization: Key $CONNECT_API_KEY" -X POST \
#     "$CONNECT_SERVER/__api__/v1/content/$guid/render" -H 'Content-Type: application/json' -d '{}'
