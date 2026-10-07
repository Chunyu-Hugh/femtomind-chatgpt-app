# Migrating FemtoMind off AWS to your own server

The app has no AWS dependency — only the deployment scripts, systemd unit
paths and the DuckDNS hostname are AWS-shaped. This is the checklist for
standing the same stack up on any Linux server (lab machine, university VM,
JLab VM).

## What the server needs

| requirement | why |
|---|---|
| Linux + systemd (RHEL/Debian/Ubuntu all fine) | same systemd layout as now |
| python 3.10+, venv | `requirements.txt` of the app + backends |
| outbound SSH 22 → scilogin.jlab.org | the two-hop cluster chain |
| outbound HTTPS 443 → lqcdmcp.jlab.org:8000 | the jam-gateway bridge (`FEMTOMIND_JAMGW_URL`) |
| outbound HTTPS 443 → github.com | workspace clone / optional auto-deploy |
| ~1 GB RAM, ~3 GB disk | measured footprint (db, logs, no compute) |

## Inbound reachability — pick ONE

ChatGPT must reach the MCP endpoint and your browser must reach the OAuth
consent pages. Two proven options:

1. **Public hostname + Caddy** (what AWS used): DNS A record → server IP,
   ports 80/443 open, `deploy/Caddyfile.example` with your hostname and a
   Let's Encrypt email. Works on any host with a public IP or NAT + port
   forward.
2. **OpenAI Secure MCP Tunnel** (no public IP needed): the repo already
   ships `deploy/femtomind-tunnel.service.example`. The server only needs
   outbound HTTPS. Verify with OpenAI's docs whether the OAuth consent flow
   is fully served through the tunnel for your client config.

## Move steps

1. **Clone the repo on the new host** in the same layout:
   ```
   ~/femtomind/
     .venv/                      # pip install -r requirements.txt (app + backends)
     femtomind-chatgpt-app/      # this repo
     fitpack-workflow/           # sibling (mcpo points here)
     hpc-monitor/                # sibling
     mcpo.config.json            # from deploy/, paths → ~/femtomind/...
     env.conf                    # from deploy/env.conf.example, see below
   ```
2. **Fix the paths** in `env.conf` + the systemd units: every
   `/home/ec2-user/femtomind/...` → `/home/<youruser>/femtomind/...`
   (`FITPACK_DB_PATH`, `FITPACK_MCP_PATH`, `FITPACK_LOCAL_DIR`,
   `ExecStart`, `WorkingDirectory`).
3. **Set `FEMTOMIND_PUBLIC_HOST`** to the new hostname (tunnel or Caddy).
4. **TLS**: install Caddy with the new hostname, or start the tunnel unit.
5. **systemd**: copy `femtomind-mcpo.service.example`,
   `femtomind-app.service.example` (+ optional backup units), `daemon-reload`,
   `enable --now`.
6. **OAuth re-registration (required)**: in the ChatGPT/OpenAI developer
   console, update the app's OAuth client **redirect URIs** to the new
   hostname — the whitelist is host-bound. The invite-code PKCE server
   itself needs no change.
7. **CI (optional)**: point `.github/workflows/deploy.yml` at the new host
   (new SSH key secret + pinned host key), or keep `deploy/generic_deploy.sh`
   for manual deploys:
   ```
   DEPLOY_HOST=you@server.lab.edu DEPLOY_USER=you ./deploy/generic_deploy.sh
   ```
8. **Smoke test** on the host:
   ```
   curl -s -X POST http://127.0.0.1:8000/fitpack/validation_query -d '{}'
   curl -s http://127.0.0.1:8010/health   # or whatever the app exposes
   ```
   Then in ChatGPT: reconnect the app, open the panel, Connect with a fresh
   OTP, and run `jam_gateway_status`.

## What stays behind on AWS

- `auth.db` (OAuth users + invite codes) — copy it over to keep existing
  ChatGPT sign-ins working, or re-invite everyone.
- `hpc_tasks.db` (fit history) — optional; copy for continuity.
- The old DuckDNS hostname — decommission after the switch.

## Watch-outs

- If the new host is inside the JLab network: outbound 22 to scilogin is
  normally open; confirm 443 to lqcdmcp.jlab.org:8000.
- The jam-gateway chain lives only while its 6h Slurm job on the LQCD node
  runs; `jam_gateway_status` says disconnected between boots — that is the
  gateway, not your server.
- Do not run this on ifarm/scilogin login nodes (daemons belong on a VM).
