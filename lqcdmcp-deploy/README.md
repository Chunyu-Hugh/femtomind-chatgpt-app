# JAM gateway on LQCDMCP

Each JAM collaborator runs **their own** MCP gateway on the JLab LQCD cluster,
launched through Jie Chen's LQCDMCP platform. It runs JAM fits on ifarm as the
collaborator, from an AI client (Claude Code, VS Code, Claude Desktop, …) or
from a browser panel that mirrors the FemtoMind JAM app.

**Collaborators: start with [COLLABORATOR_GUIDE.md](COLLABORATOR_GUIDE.md).**

## Files

| File | What it is |
|---|---|
| `jam_gateway.py` | The gateway: MCP tools, access check, cluster chain, campaigns, HTTP server. Runs on the cluster node. |
| `jam_panel.html` | The browser panel (Connect / Project / Campaign / Collections / Analysis), served by the gateway. |
| `jamai/` | Turns a campaign into a FitPack input card and runs one replica (`run_replica.py`); copied into the workspace at submit time. |
| `workspace_files/` | Files a fresh FitPack clone lacks for single fits (`maxlike_jam.py`, `tools/env.py`). |
| `deploy_and_verify.py` | Launch your gateway and check it end to end. |
| `redeploy_jam.py` | `launch` / `cancel` / `status` through the dashboard API; `script` writes the job script for the dashboard page. |
| `hot_redeploy.py` | Update a running gateway's code without restarting its job. |
| `check_dashboard_token.py` | Show what the dashboard answers for your token. |
| `gw_auth.py` | Gateway URL and token for the scripts (`JAM_GW_USER`, `LQCDMCP_TOKEN`). |

## Deploy

```bash
export LQCDMCP_TOKEN='...'    # dashboard token (Globus login)
export JAM_GW_USER=<you>
python3 deploy_and_verify.py   # launch, wait, verify
```

The gateway is then at `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp`
(panel: append `?ui=panel`). It runs on the daily reservation `chen_mcp_daily`
(about 10:00–16:00 ET), **one gateway job at a time**: stop yours with
`python3 redeploy_jam.py cancel` so a colleague's can start.

## How it works

- **Access.** The only credential is the owner's dashboard token. The gateway
  asks the dashboard (`get_my_mcp_servers`) which servers the token's user
  launched and accepts it when this gateway is listed (cached 10 min). The
  proxy forwards `/cloud/<name>/mcp` without checking anyone, so this check is
  what keeps an open cluster session private.
- **Cluster.** A two-hop SSH chain per user: `scilogin.jlab.org` (PIN +
  one-time code) → `ifarm.jlab.org` (password), opened on the panel's Connect
  tab and kept for the window. Site details (hosts, `/w/ceph24/JAM/<user>`,
  container image, partitions, shared grids) are in `SITE_IFARM` in
  `jam_gateway.py`; `~/.jam_gateway/site.json` on the node overrides them.
- **Workspace.** A clone of `QCDHUB/fitpack_legacy`, branch `ai` (it has
  `fitlib/ai.py`), with links to the shared `share/fitpack_grids`.
- **Campaigns.** Same steps as the FemtoMind JAM app: object and its
  parameter table, reactions and datasets (by experiment) and cuts, QCD
  settings, job config with Slurm resources, review. `jamai` copies the
  repository's `examples/input02.py` and appends the choices; a Slurm array
  runs one replica per task. Then postprocess, fit summary, PDF plots, χ² and
  data vs theory by experiment, more replicas (confirmation phrase).
  Relative to the app, these now actually reach the fit: parameter-table
  edits, cuts, datasets missing from the template, max iterations, optimizer,
  TMC "off".
- **Other Slurm jobs.** The AI drafts a script with you (`draft_slurm_job`,
  checked with `sbatch --test-only`) and submits it once you confirm.
- **Partition.** Auto: `priority` for jobs up to 4 h, `production` beyond.

## Verified

2026-10-08 on the platform: gateway launched as `jam-gateway-chunyu`,
reachable through the proxy, dashboard token accepted and no-token refused,
cluster chain open, workspace and campaigns read back. On ifarm through the
same code: campaigns submitted, run, postprocessed and plotted; replicas
appended with confirmation; a Slurm job drafted and submitted; a Claude Code
session walked the whole composer in chat and the generated card matched
every answer.

Earlier versions (v2–v7, the shared gateway and its tokens) are in git history.
