# Deployment

How the running instance is put together, written from the live host rather
than from intent. Everything below was read off the deployment on
2026-08-21; where the arrangement is surprising, the reason is given, because
those are the parts that get "tidied up" and break.

## What runs where

```
ChatGPT
   │  HTTPS
   ▼
Caddy :443 ──────────────► femtomind_app.py :8010     MCP endpoint + OAuth server
   (TLS, single entry)        │
                              │  HTTP, localhost
                              ▼
                          mcpo :8000                  gateway over two MCP servers
                              ├── fitpack   fitpack-workflow/server.py
                              └── hpc       hpc-monitor/hpc_mcp_server/server.py
                              │
                              │  SSH, two hops, ControlMaster
                              ▼
                          scilogin.jlab.org ──► ifarm.jlab.org ──► SLURM
```

Nothing scientific is computed on this host. It authenticates, routes, and
keeps the run history; every fit and every plotted curve is a job on the
cluster.

## Host

An AWS Lightsail instance is enough — the measured footprint is small:

| | |
|---|---|
| OS | Amazon Linux 2023 |
| Size | 2 vCPU, 916 MB RAM, 40 GB disk |
| In use | ~70 MB resident for the app; 2.8 GB of disk |
| Open to the internet | 22, 80, 443 only |

## Layout

```
~/femtomind/
  .venv/                     one virtualenv for everything
  env.conf                   systemd EnvironmentFile — see Configuration
  mcpo.config.json           which MCP servers the gateway starts
  femtomind-chatgpt-app/     this repository
  fitpack-workflow/          the QCD backend (also in this repository)
  hpc-monitor/               cluster status MCP server
  backup_db.py               daily snapshot of both databases
  backups/                   snapshots, seven kept per database
  duckdns_sync.sh            keeps the DNS record pointed here
```

## Build from scratch

**1. Instance and firewall.** Open 22, 80, 443. Leave everything else closed —
the services bind `0.0.0.0`, so the firewall rule is what actually keeps 8000
and 8010 off the internet. Binding them to `127.0.0.1` instead would be belt
and braces; today the cloud rule is the only thing in the way.

**2. Python.** One virtualenv at `~/femtomind/.venv` serves all three services.
Install this repository's `requirements.txt` plus `mcpo`.

**3. DNS.** `duckdns_sync.sh` reads a token from `~/.duckdns_token` and
re-points the record at the instance's current address. Run it from cron so a
restarted instance recovers its name.

**4. TLS and routing.** Caddy terminates TLS and forwards exactly the four
paths the protocol needs; everything else answers with a plain string, so the
host does not look like a fileserver:

```caddyfile
femtomind.duckdns.org {
    tls you@example.edu
    handle /mcp*          { reverse_proxy localhost:8010 }
    handle /.well-known/* { reverse_proxy localhost:8010 }
    handle /authorize*    { reverse_proxy localhost:8010 }
    handle /token*        { reverse_proxy localhost:8010 }
    handle                { respond "FemtoMind MCP server" 200 }
}
```

`/.well-known/*` is not optional: it serves the RFC 9728 protected-resource
and RFC 8414 authorization-server documents that the client reads before it
will attempt OAuth.

**5. Services.** Three units, examples in `deploy/`. Each takes
`EnvironmentFile=/home/ec2-user/femtomind/env.conf` and `Restart=always`.

```
femtomind-app      .venv/bin/python femtomind-chatgpt-app/femtomind_app.py
femtomind-mcpo     .venv/bin/mcpo --port 8000 --config mcpo.config.json
femtomind-tunnel   tunnel-client run --profile femtomind
```

> **`femtomind-app` must keep `KillMode=process`.** The SSH ControlMasters that
> hold each user's cluster session are children of this unit, so the default
> `KillMode=control-group` tears down every collaborator's live session on any
> restart — including a restart to deploy an unrelated fix. With
> `KillMode=process` only the app is signalled and the sessions survive.

**6. Backups.** `femtomind-backup.timer` runs daily and snapshots both
databases through SQLite's online backup API — not a file copy, so a snapshot
taken mid-write is still internally consistent. Seven are kept per database.
They land on the same host, which protects against corruption and mistakes but
not against losing the instance.

## Configuration

`env.conf` holds no secrets; it is paths and switches. Secrets live elsewhere:
the DuckDNS token in `~/.duckdns_token`, the tunnel key in
`~/.config/tunnel-client/key`, and cluster passwords nowhere at all — see
Security.

| Variable | What it decides |
|---|---|
| `FEMTOMIND_PUBLIC_HOST` | The public name. Also what an invite prints, so a wrong value hands collaborators an address that cannot work. |
| `HPC_REMOTE_BASE_TEMPLATE` | Where a workspace is created under a user's cluster area. |
| `FITPACK_DB_PATH` | The job database the app and the backend must agree on. |
| `FITPACK_MCP_PATH`, `FITPACK_LOCAL_DIR`, `FITPACK_REMOTE_TEMPLATE` | Where the backend and its remote counterpart live. |
| `FEMTOMIND_PLOT_ON_HPC` | Keep at 1. Computing curves locally needs a full fitpack tree on this host, which is the thing this deployment avoids. |
| `FEMTOMIND_HPC_USER` | Fallback cluster account. Each collaborator supplies their own at sign-in; this is only a default. |
| `FASTMCP_LOG_LEVEL` | Log verbosity. |

Host names for the cluster are read from `FEMTOMIND_GATEWAY_HOST`,
`FEMTOMIND_IFARM_HOST` and `FEMTOMIND_HPC_HOST` in the app layer.
**The backend does not read them** — `fitpack-workflow/ssh_runner.py` hardcodes
`HPC_HOST = "ifarm.jlab.org"`. Pointing the app at another facility without
changing that line leaves the two layers addressing different machines.

## Operating it

```bash
sudo systemctl restart femtomind-app          # after changing femtomind_app.py or a widget
sudo systemctl restart femtomind-mcpo         # after changing fitpack-workflow/
journalctl -u femtomind-app -f                # follow
./scripts/femtoadmin.py invite "Name"         # add a collaborator
./scripts/femtoadmin.py list                  # who has access, and when they last used it
./scripts/femtoadmin.py revoke <user_id>      # ends live sessions as well as future sign-ins
```

Deploying is copying the changed file and restarting its unit. Bump
`OPERATIONS_URI` (and the other `*_URI` constants) when a widget's HTML
changes, or clients keep serving the cached copy.

After deploying, check the thing you changed actually changed. Most of what has
broken here returned HTTP 200 and rendered a panel while being wrong — a
chi-square that was never computed, an ordering that ranked by the replicas
with no chi-square at all, a summary card that had never once rendered. Nothing
in this stack will tell you; only looking will.

## Security

**What reaches the internet.** Ports 80, 443 and 22. Caddy forwards four
paths to the app and answers everything else with a fixed string. The MCP
endpoint returns 401 until an OAuth 2.1 flow completes with PKCE; the
authorization server is this app.

**Who a caller is.** A collaborator redeems an invite code once and holds a
bearer token. Tokens are stored hashed. Revoking an invite ends live sessions
as well as future sign-ins. The authenticated identity — never an argument
supplied by the caller — selects the SSH connection, the workspace and the
history, so collaborators cannot reach each other's runs.

**Cluster credentials.** A one-time code and a password open the two SSH hops.
They are passed to `ssh` through a throwaway askpass helper that contains no
secret — it echoes an environment variable — so neither value is written to
disk, and the prompt log records the server's question, never the answer. Both
values do pass through this host's memory during the handshake. That is the
one real difference from a design where the SSH session lives on the user's own
laptop, and it is the honest thing to say when the question comes up.

**Where the boundary is not.** The fit-defining values are held to the
researcher by the interface and the tool contract, not by the protocol, and
there is no folder-level containment beyond the permissions of the user's own
cluster account. Jobs run under that account and against its quota, and no tool
in the surface executes arbitrary commands.

## Known constraints

- **One facility.** The backend hardcodes `ifarm.jlab.org`, as above.
- **Backups are local.** Same host as the thing they back up.
- **Redirect allowlist.** `femtomind_auth.py` accepts callbacks under
  `https://chatgpt.com/connector/oauth/` by prefix. Pinning it to one
  connector id would lock out every collaborator, since ChatGPT mints a fresh
  callback per connector; widening it past a prefix would make the endpoint an
  open redirector.
- **Private upstream.** Creating a workspace clones a private repository, so
  each collaborator supplies a GitHub token for that one clone. It is used
  once and never stored.
