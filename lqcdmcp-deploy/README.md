# JAM gateway on LQCDMCP

Each JAM collaborator runs **their own** MCP gateway, `jam-gateway-<user>`, as
a Slurm job on the JLab LQCD cluster, launched through Jie Chen's LQCDMCP
platform. It runs JAM fits on ifarm as that collaborator, from an MCP client
(Claude Code or Codex) or from the browser panel it serves, which mirrors
the FemtoMind JAM app's fitting campaign.

**Collaborators: start with [COLLABORATOR_GUIDE.md](COLLABORATOR_GUIDE.md).**

## Files

| File | What it is |
|---|---|
| `jam_gateway.py` | The gateway (stdlib only): MCP tools, access check, cluster chain, campaigns, HTTP server. Runs on a cluster node. |
| `jam_panel.html` | The panel (Connect / Project / Campaign / Collections / Analysis), served by the gateway as a browser page and as an MCP Apps / ChatGPT widget resource. |
| `jamai/` | Turns a campaign into a FitPack input card and runs one replica (`run_replica.py`); uploaded to `<workspace>/.jamai/` at every submit. |
| `deploy_and_verify.py` | Launch your gateway if it is not running, wait, and check it end to end (`--check`: check only). Prints a shareable report without credentials. |
| `redeploy_jam.py` | `launch` / `cancel` / `status` through the dashboard API; `script` writes the job script for the dashboard page. |
| `hot_redeploy.py` | Replace a running gateway's code (gateway, panel, `jamai/`) inside the same job. |
| `check_dashboard_token.py` | Make the dashboard calls the gateway's access check makes, and print the decision. |
| `gw_auth.py` | Gateway name, URL and token for the scripts: `JAM_GW_USER`, `LQCDMCP_TOKEN`; `JAM_GW_NAME`, `JAM_GW_URL`, `JAM_GW_TOKEN` for a test gateway. |
| `COLLABORATOR_GUIDE.md` | The step-by-step guide for collaborators. |

## Deploy

```bash
export LQCDMCP_TOKEN='...'    # dashboard token (Globus login), with or without "Bearer "
export JAM_GW_USER=<you>
python3 deploy_and_verify.py   # launch, wait, verify
```

The job script carries the gateway, the panel and `jamai/` (zlib + base64)
and no credential. It asks for partition `24s`, reservation `chen_mcp_daily`
(about 10:00–16:00 US Eastern), 6 h and 2 CPUs (`JAM_GW_PARTITION`,
`JAM_GW_RESERVATION`, `JAM_GW_HOURS` override). The job unpacks the files into
`~/.jam_gateway/app`, and a supervisor loop restarts the server whenever it
exits. The server takes the first free port from 8080 and registers with the
proxy as `jam-gateway-<user>`, at
`https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<user>/mcp`.

- **Accounts.** Jie Chen must first enable an account for MCP servers and the
  reservation. Until then the dashboard's `launch_mcp_server_using_script`
  fails with `Slurm command failed: sudo sbatch --uid <user> ... (rc=1)`, and
  `deploy_and_verify.py` stops there with that explanation.
- **One at a time.** So far the reservation has run only one gateway job at a
  time; a second one stays PENDING until the first is cancelled
  (`python3 redeploy_jam.py cancel`).
- **Dashboard page instead.** `python3 redeploy_jam.py script` writes
  `jam-gateway-<user>.slurm`; launch an MCP server with that name and file.
- **Upgrade a running gateway:** `python3 hot_redeploy.py`, then
  `python3 deploy_and_verify.py --check`. Through `node_diag` (a shell on the
  gateway node, absent from `tools/list` but callable with the owner's token)
  it uploads the new gateway and test-imports it on the node (nothing is
  replaced if that fails), installs the panel and `jamai/`, keeps the old
  gateway as `jam_gateway.prev.py` and stops the server, which the job's
  supervisor loop restarts. It then checks that the answering server started
  after the swap, runs the uploaded file (sha256) and still accepts the
  token. It refuses a gateway that is not a Slurm job, since nothing would
  restart it. The job and the cluster sign-in survive.

## How it works

- **Access.** Every `tools/call` needs `Authorization: Bearer <dashboard
  token>`; `initialize` and `tools/list` need none, and the LQCDMCP proxy
  forwards `/cloud/<name>/mcp` without checking anyone. The gateway asks the
  dashboard with the caller's token. `validate_user` (for the gateway's owner)
  must not answer with an error or `allow_mcp: false`, and when it names an
  account (`user_account`, `user_id`), one of them must be the owner.
  `get_my_mcp_servers` must list this gateway: its name, owner == the
  gateway's owner (when the entry has an owner) and the same Slurm job id
  (when both are known). Anything else, including a listing the gateway
  cannot parse or a failed call, is a refusal (fail closed).
  Decisions are cached per token hash: 10 min when accepted, 60 s when
  refused, 15 s after a dashboard error. A doubled `Bearer ` is tolerated.
  `JAM_GW_TEST_TOKEN` is the token of a test instance (below).
- **Panel.** A browser GET (`Accept: text/html`) of the gateway URL returns
  `jam_panel.html` with a strict CSP (inline script and style only,
  `connect-src 'self'`, `frame-ancestors 'none'`); `?tab=` picks the tab. The
  page keeps the token in `sessionStorage`. The same file is the MCP Apps /
  ChatGPT widget; there the host sends the token, and the Connect tab only
  links to the gateway's own page, so cluster credentials are typed only
  there. The Project tab's GitHub-token field is likewise shown only on the
  gateway's own page.
  `connect_hpc` and `setup_workspace` also accept the credentials and the
  token as arguments, but the tool descriptions and `INSTRUCTIONS` tell AI
  clients never to ask for them.
- **Cluster.** A two-hop SSH chain per user, as ControlMasters in `~/fm-cm` on
  the gateway node: `scilogin.jlab.org` (PIN + one-time code), a port forward,
  then `ifarm.jlab.org` (password); `ControlPersist 8h`. `connect_hpc` reuses
  a live chain; while the login hop is alive it needs only the password. Site
  settings are `SITE_IFARM` in `jam_gateway.py`; `~/.jam_gateway/site.json`
  (or `JAM_GW_SITE_FILE`) overrides them.
- **Workspace.** A clone of `QCDHUB/fitpack_legacy`, branch `ai`, in
  `/w/ceph24/JAM/<user>/<folder>`, recorded in
  `/w/ceph24/JAM/<user>/.jam_workspace`, with links to the shared grids in
  `/w/ceph24/JAM/share/fitpack_grids`. An existing folder is adopted, not
  cloned. `check_project` checks code, data, grids, `examples/input02.py` and
  `fitlib/ai.py`; `use_ai_branch` switches a clean checkout to `ai`.
- **Campaigns.** The parameter table is read from
  `<workspace>/examples/input02.py` (its uncommented
  `conf['params']['pdf'][...]` lines; cached 10 min per user, dropped when the
  workspace or branch changes). Only the rows the user changed go into the
  card. `jamai` copies `input02.py` to `stages/pdf_stage/input.py` and appends
  an override block. A Slurm array runs one replica per task in the site's
  container: `jamai/run_replica.py` applies max iterations and the optimizer,
  then runs `fitlib/ai.py`. The array sets `LHAPDF_DATA_PATH` to the shared
  `lhapdf_extra` tables (never a personal folder). The registry is
  `<workspace>/campaigns/collections.json`. Stopping a campaign cancels its
  jobs that still have tasks in the queue; deleting is refused while any of
  its tasks is queued. When `squeue` fails the queue is unknown, never empty:
  stop and delete refuse, and the lists say so.
- **Confirmation phrases.** `submit_campaign`, `append_replicas`,
  `cancel_campaign`, `delete_campaign` and `submit_slurm_job` act only when
  called again with the phrase from a first call (`CONFIRM JAM <kind>
  <hash>`), which ties it to exactly that card or plan.
- **Other Slurm jobs.** `draft_slurm_job` writes `<workspace>/slurm/<name>.sh`
  and checks it with `sbatch --test-only`; `submit_slurm_job` submits it.
- **Partition.** Auto: `priority` when the wall clock per replica is up to
  4 h, `production` beyond.
- **Tools.** ping, get_console, open_panel, connect_hpc, check_hpc_connection,
  check_project, setup_workspace, use_workspace, use_ai_branch, fs_list,
  fs_read, campaign_options, campaign_datasets, validate_campaign,
  submit_campaign, list_campaigns, campaign_status, cancel_campaign,
  delete_campaign, postprocess_campaign, campaign_summary, campaign_pdf_plots,
  analyze_by_experiment, append_replicas, slurm_partitions, draft_slurm_job,
  submit_slurm_job; hidden: node_diag. The AI-facing workflow is
  `INSTRUCTIONS` in `jam_gateway.py`.

**Test instance.** `jam_gateway.py` also runs off the platform, on a machine
that can reach scilogin: set `JAM_GW_OWNER` (the JLab user), `JAM_GW_NAME`,
`JAM_GW_PORT`, `JAM_GW_BIND`, `JAM_GW_PUBLIC_URL` (the URL the panel links
use), `JAM_GW_ACCESS_DIR` (its log folder) and `JAM_GW_TEST_TOKEN` (the token
it accepts, since the dashboard does not know such a gateway). The scripts
reach it with `JAM_GW_URL` and `JAM_GW_TOKEN`; `hot_redeploy.py` refuses it
(no supervisor loop restarts it).

## Verified

2026-10-08, with the final code of this version, against ifarm through a
local test instance of the gateway:

- live run, 36 checks, all passed: the library table (39 rows), a generated
  card that differs from `input02.py` only in the changed row, stop and
  delete (delete refused while tasks are queued), postprocess, fit summary,
  a PDF plot, the analysis by experiment, an append plan;
- the panel in headless Chromium: 19 checks, all passed;
- offline suites: 368 checks of the gateway and scripts and 92 of the panel
  (jsdom), all passed (the suites are not part of this repository);
- Codex 0.161.0 connected and called a tool; Claude Code 2.x stored the
  guide's `${LQCDMCP_TOKEN}` header and expanded it at start (local and user
  scope).

**Not yet verified live with this version:** the owner check against the
real dashboard (run `python3 deploy_and_verify.py --check` after the next
deploy), `hot_redeploy.py` on the platform, and a workspace clone with a
GitHub token. Earlier the same day, with the previous version, the platform
deploy and the access control were verified.

Also not verified: the VS Code and Claude Desktop setups in the guide, and
Claude Code tool calls longer than 60 s with the guide's timeout settings
(taken from the Claude Code documentation).

## History

Earlier gateway versions and the AWS-hosted ChatGPT app (retired on
2026-10-08) are in git history: commit `7b99311` and before.
