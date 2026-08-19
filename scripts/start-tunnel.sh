#!/bin/bash
# Connect the local FemtoMind MCP server (:8010) to ChatGPT via OpenAI's
# Secure MCP Tunnel — no public exposure, outbound-only.
#
# Get these two values from your OpenAI account first:
#   CONTROL_PLANE_API_KEY : platform.openai.com/settings/organization/api-keys
#                           (a Runtime key with Tunnels Read + Use)
#   CONTROL_PLANE_TUNNEL_ID: platform.openai.com/settings/organization/tunnels
#                           (create a tunnel; id looks like tunnel_<32 hex>)
#
# Usage:
#   export CONTROL_PLANE_API_KEY=sk-...
#   export CONTROL_PLANE_TUNNEL_ID=tunnel_....
#   ./start-tunnel.sh
set -e
: "${CONTROL_PLANE_API_KEY:?set CONTROL_PLANE_API_KEY (Runtime key from platform.openai.com)}"
: "${CONTROL_PLANE_TUNNEL_ID:?set CONTROL_PLANE_TUNNEL_ID (tunnel_... from platform.openai.com/settings/organization/tunnels)}"

# make sure the MCP server is up
if ! curl -fsS -o /dev/null http://localhost:8010/mcp 2>/dev/null; then
  echo "Starting MCP server on :8010…"; bash "$(dirname "$0")/run.sh"; sleep 2
fi

# point the profile at the real tunnel id, then validate + run
tunnel-client init --sample sample_mcp_remote_no_auth --profile femtomind \
  --tunnel-id "$CONTROL_PLANE_TUNNEL_ID" --mcp-server-url http://localhost:8010/mcp >/dev/null
echo "── doctor ──"
tunnel-client doctor --profile femtomind --explain || true
echo "── run (foreground; Ctrl-C to stop) ──"
exec tunnel-client run --profile femtomind --log.level=info --log.format=struct-text
