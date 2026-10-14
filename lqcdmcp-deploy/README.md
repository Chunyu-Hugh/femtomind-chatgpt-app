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
| `deploy_and_verify.py` | Launch your gateway unless your gateway job is already queued or running, wait for it (it stops early when the job stays queued, or when a job it launched ends), and check it end to end (`--check`: check only). Prints a shareable report without credentials. |
| `redeploy_jam.py` | `launch` / `cancel` / `status` through the dashboard API; `script` writes the job script for the dashboard page. |
| `hot_redeploy.py` | Replace a running gateway's code (gateway, panel, `jamai/`) inside the same job. |
| `check_dashboard_token.py` | Make the dashboard calls the gateway's access check makes, and print the decision. |
| `gw_auth.py` | Gateway name, URL and token for the scripts: `JAM_GW_USER`, `LQCDMCP_TOKEN`; `JAM_GW_NAME`, `JAM_GW_URL`, `JAM_GW_TOKEN` for a test gateway. |
| `COLLABORATOR_GUIDE.md` | The step-by-step guide for collaborators. |

## Architecture and extension guide

This project is a UI and workflow layer around FitPack Legacy. It does not
replace FitPack's core physics implementation. New physics objects should be
enabled only after their input-card representation and parameter choices have
been confirmed by a physicist.

### End-to-end workflow

```text
User interaction in jam_panel.html
        ↓
Campaign input card
        ↓
jam_gateway.py validation and normalization
        ↓
Campaign manifest
        ↓
jamai/input_materializer.py
        ↓
Generated input.py
        ↓
fitlib/ai.py
        ↓
FitPack/JAM RESMAN and MAXLIKE
        ↓
Replica outputs, chi2 summaries, and plots
```

### Component responsibilities

#### `jam_panel.html`

The browser UI shows available objects and reactions, lets users select
datasets, edits PDF and dataset-normalization parameters, and builds and
reviews the campaign input card. The UI must not invent physics defaults.
Parameters should be exposed only after their physical meaning and valid
configuration have been confirmed.

#### `jam_gateway.py`

The gateway is the backend validation and orchestration layer. It provides
campaign options, reads the workspace parameter library and dataset catalog,
validates user selections, rejects invalid values and equality cycles, and
converts the UI card into a normalized campaign manifest for Slurm.

#### `jamai/input_materializer.py`

The materializer converts the normalized campaign into FitPack-compatible
`input.py` overrides. Every setting exposed in the UI must be handled here if
it changes the FitPack input.

#### `fitlib/ai.py`

This is the campaign execution entrypoint in FitPack's `ai` branch. It loads
the materialized manifest and `input.py`, runs validation or fitting, calls
FitPack/JAM fitting components, and writes replica outputs and fit summaries.

#### `fitlib/ai_conf.py`

This provides default configuration helpers for AI/JAMAI campaigns. It is
currently lightweight scaffolding; it is not the sole place where a new
physics object's parameters should be defined.

#### `fitlib/ai_registry.py`

This maps reactions to the physics objects they require. For example, DIS and
Drell--Yan require an unpolarized PDF, while SIDIS may require PDFs and pion,
kaon, and hadron fragmentation functions. The registry describes dependencies;
it does not implement the physics parameters or prove that an object is ready
to fit.

### Adding a new physics object

Before changing the UI, provide and verify:

1. The FitPack input-card representation.
2. Parameter names and default values.
3. Which parameters are FREE or FIXED by default.
4. Valid minimum and maximum values.
5. Which reactions require the object.
6. Which datasets use the object.
7. Any equality or normalization constraints.
8. A minimal working FitPack input and test fit.

After the physics configuration is confirmed, integrate it in this order:

1. Add or verify the reaction/object mapping in `fitlib/ai_registry.py`.
2. Add the object and parameter configuration to the campaign options.
3. Update gateway validation in `jam_gateway.py`.
4. Update `input_materializer.py` so the settings reach `input.py`.
5. Add the UI controls in `jam_panel.html`.
6. Add validation and integration tests.
7. Run a minimal real campaign before enabling the object by default.

### Scope boundary for physicists

Do not enable PION PDFs, kaon PDFs, or fragmentation functions only because
they appear in `ai_registry.py`. The registry is not proof that an object is
fully supported. The physics parameter definitions, a working input-card
configuration, and a minimal test fit must be provided first.

The intended collaboration boundary is:

- **Physicist:** provide the physical parameter definitions, constraints,
  datasets, input-card example, and plotting rules.
- **Workflow/UI developer:** connect that confirmed configuration to the
  gateway, materializer, UI, validation, and campaign execution.

## Deploy

```bash
export LQCDMCP_TOKEN='...'    # dashboard token (Globus login), with or without "Bearer "
export JAM_GW_USER=<you>
python3 deploy_and_verify.py   # launch, wait, verify
```

The job script carries the gateway, the panel and `jamai/` (zlib + base64)
and no credential. It asks for partition `24s`, reservation `chen_mcp_daily`
(about 10:00–16:00 US Eastern), 6 h and 2 CPUs (`JAM_GW_PARTITION`,
`JAM_GW_RESERVATION`, `JAM_GW_HOURS` override; `JAM_GW_ACCOUNT` adds
`--account`, for a user whose default Slurm account cannot use the partition). The job unpacks the files into
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
- **Shared mode.** `JAM_GW_SHARED=1` at launch (or `"shared": true` in
  `~/.jam_gateway/site.json` on the node) makes the gateway act as the
  user named by `username` in each call, with that user's own chain,
  area and workspace; without a username it acts as its owner.
  `node_diag` stays the owner's. The panel then lets the Connect tab's
  username be typed and sends it with every call. Meant for colleagues
  who cannot launch a gateway (see the guide, 2.5).
- **Not on the reservation.** `chen_mcp_daily` has a user list (Jie adds
  people). It is required: the proxy reaches port 8080 only on the reserved node
  (`qcd24s100`); a gateway on another `24s` node starts but is unreachable
  ("No route to host", 2026-10-13). The partition allows the `hadstruc24s`
  account, not `jlablqcd24s`: `JAM_GW_ACCOUNT` names one when the user's
  default account is not allowed.
- **Dashboard page instead.** `python3 redeploy_jam.py script` writes
  `jam-gateway-<user>.slurm`; launch an MCP server with that name and file.
- **Upgrade a running gateway:** `python3 hot_redeploy.py`, then
  `python3 deploy_and_verify.py --check`. Through `node_diag` (a shell on the
  gateway node, absent from `tools/list` but callable by name; see Access)
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
  forwards `/cloud/<name>/mcp` without checking anyone. The gateway refuses an
  empty token or one shorter than 16 characters, and asks the dashboard about
  any other, with the caller's token. `validate_user` (for the gateway's owner)
  must not answer with an error or `allow_mcp: false`, and when it names an
  account (`user_account`, `user_id`), one of them must be the owner.
  `get_my_mcp_servers` must list this gateway: its name, owner == the
  gateway's owner (when the entry has an owner) and the same Slurm job id
  (when both are known). Anything else, including a listing the gateway
  cannot parse or a failed call, is a refusal (fail closed).
  Decisions are cached per token hash: 10 min when accepted, 60 s when
  refused, 15 s after a dashboard error. A doubled `Bearer ` is tolerated.
  `JAM_GW_TEST_TOKEN` is the token of a test instance (below).
  **This check does not keep others out.** In October 2026 the dashboard
  answered yes for any token (tested with a made-up token and with none):
  `validate_user` trusts its `username` argument, which the gateway sets to
  its owner, and `get_my_mcp_servers` then answers for that user. So the
  gateway accepts any token of 16 or more characters, and whoever reaches it
  can call every tool as its owner, `node_diag` and the open cluster chain
  included. `deploy_and_verify.py` and `check_dashboard_token.py` show only
  that the owner's token works. Until the dashboard checks the token, stop a
  gateway when you are done with it (`python3 redeploy_jam.py cancel`).
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
  guards FitPack's status report, then runs `fitlib/ai.py`. The status report
  (`fitlib.resman.RESMAN.gen_report`, printed while fitting) is output only
  but reads each data table's `col`, `obs` and `target`; idis 10005 has no
  `col`, and the `KeyError` made `ai.py` fall back to χ² at the starting
  values. Now an error there becomes one line of the report, such as
  `(status report unavailable: KeyError: 'col')`, and the fit goes on. The
  array sets `LHAPDF_DATA_PATH` to the shared `lhapdf_extra` tables (never a
  personal folder). The registry is
  `<workspace>/campaigns/collections.json`. Stopping a campaign cancels its
  jobs that still have tasks in the queue; deleting is refused while any of
  its tasks is queued. When `squeue` fails the queue is unknown, never empty:
  stop and delete refuse, and the lists say so.
- **Data check.** `validate_campaign` also reads each selected dataset's
  `database/<reaction>/expdata/<id>.xlsx` in one container call, derives the
  cut variables as FitPack's readers do (idis `W2` or `X`, dy `Q2`) and
  applies each enabled cut with `tab.query`, as `tools/reader.py` does. It
  returns `summary.points_after_cuts` (`{"idis:10005": 33}`) and `warnings`,
  which never block: no points left after the cuts, a cut that cannot be
  evaluated, a missing column that the status report reads. If the check
  fails, validation still succeeds, with one warning saying so.
- **Replica states.** When the optimization fails, `fitlib/ai.py` computes
  χ² at the starting values instead and still writes `"status": "success"`,
  with `"mode": "validation_only"` and no `.msr` file. So a replica is
  fitted only with status `success` and mode `fit` (a summary without
  `mode`: when its `msr_path` exists), not fitted with any other `success`,
  and failed otherwise or when its summary cannot be read.
  `_REPLICA_STATE` decides this for the status, postprocess and log snippets
  alike: `list_campaigns` and `campaign_status` count `succeeded` (fitted),
  `not_fitted` and `failed` (state `not fitted` when nothing is queued and
  every finished replica did not fit), and postprocessing builds the
  statistics from fitted replicas only (`not_fitted_replicas` apart,
  `good_fraction` = fitted / total).
- **Logs.** `campaign_logs` reads, in one plain-Python call on ifarm, each
  replica's `fit_summary.json`, its Slurm log (`logs/stage_<job>_<replica>.out`
  of the job whose replica range holds it) and the queue. It returns each
  replica's state, χ² and whether its log holds a Python traceback, and for
  the first replica that did not fit, failed or left no output (or for
  replica N) the traceback and the end of the log (`lines`, default 80, at
  most 400). The whole log is searched: the traceback goes to stderr while
  stdout is buffered, so it often sits near the start of the merged file.
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
  analyze_by_experiment, campaign_logs, append_replicas, slurm_partitions,
  draft_slurm_job, submit_slurm_job; hidden: node_diag. The AI-facing
  workflow is `INSTRUCTIONS` in `jam_gateway.py`.

**Test instance.** `jam_gateway.py` also runs off the platform, on a machine
that can reach scilogin: set `JAM_GW_OWNER` (the JLab user), `JAM_GW_NAME`,
`JAM_GW_PORT`, `JAM_GW_BIND`, `JAM_GW_PUBLIC_URL` (the URL the panel links
use), `JAM_GW_ACCESS_DIR` (its log folder) and `JAM_GW_TEST_TOKEN` (the token
it accepts, since the dashboard does not know such a gateway). The scripts
reach it with `JAM_GW_URL` and `JAM_GW_TOKEN`; `hot_redeploy.py` refuses it
(no supervisor loop restarts it).

## Verified

2026-10-08, with the code of commit `0ad83c4`, against ifarm through a local
test instance of the gateway:

- live run, 36 checks, all passed: the library table (39 rows), a generated
  card that differs from `input02.py` only in the changed row, stop and
  delete (delete refused while tasks are queued), postprocess, fit summary,
  a PDF plot, the analysis by experiment, an append plan;
- the panel in headless Chromium: 19 checks, all passed;
- Codex 0.161.0 connected and called a tool; Claude Code 2.x stored the
  guide's `${LQCDMCP_TOKEN}` header and expanded it at start (local and user
  scope).

The fixes in commit `a6cc642` (queued or ended gateway jobs and failed
dashboard requests in the deploy scripts, a deleted workspace folder or
template, the panel's Collections note, Generate More Replicas and
GitHub-token field) were tested offline only. With them, the offline
suites (not part of this repository) all pass: 377 checks of the gateway
and scripts, 151 of `deploy_and_verify.py` and `redeploy_jam.py` against
stand-in dashboard and gateway servers (Python 3.10, 3.11 and 3.13), and
631 of the panel (jsdom).

**2026-10-09 on the LQCDMCP platform** (gateway `jam-gateway-chunyu`,
launched with `deploy_and_verify.py`, then updated in place with
`hot_redeploy.py`; the cluster sign-in survived the update):

- `deploy_and_verify.py` and `--check`: launched, reachable through the
  proxy, the owner's token accepted and a call without a token refused,
  28 tools, `node_diag` hidden. The dashboard answers as an event stream
  with CRLF line ends and a log notification before the result;
  `redeploy_jam.py` now reads that (before, every answer was shown as `{}`).
- the changes after `a6cc642` (replica states, the data check,
  `campaign_logs`, the panel's Logs and warnings, the status-report guard in
  `run_replica.py`), live: 18 checks, all passed. A campaign with idis 10005
  and 10006 really fitted (`mode: fit`, 35 fitted parameters, an `.msr` per
  replica; the guarded status report shows as one line in the log);
  postprocess and a PDF plot from the fitted replicas; Create Input Card
  warned that 10001 and 10004 have no points after the cuts and that 10005
  has no `col` column; a campaign run before the guard shows as "not fitted"
  (0 / 10) with its `KeyError: 'col'` traceback in `campaign_logs` and in the
  panel's Logs (headless Chromium, 11 checks).
- offline, all passed: 419 checks of the gateway and scripts, 151 of
  `deploy_and_verify.py` and `redeploy_jam.py` against stand-in dashboard
  and gateway servers (Python 3.10, 3.11 and 3.13), 631 of the panel
  (jsdom); the suites are not part of this repository.

- **2026-10-14, equal-to and normalization controls** (commits `ac1c95f`,
  `c7de6eb`), on the platform with the owner's workspace: `campaign_datasets`
  lists the normalizations of 10010 and 10016 from `input02.py` and none for
  10005; a card with `g1 a` equal to `uv1 a` and normalization 10010 equal to
  10016 produced `input.py` lines `'fixed': 'uv1 a'` and `'fixed': 10016`, the
  replica really fitted with no error, and its free-parameter count was 35
  (35 PDF parameters − 1 tied + 1 free normalization). A self-reference is
  refused. Panel (jsdom, 12 checks): checkbox status, Equal to, normalization
  editing, Clear visible, Review.

**Not yet verified live:** a workspace clone with a GitHub token, and the
deploy scripts' queue handling when another gateway holds the reservation.
In October 2026 the dashboard was found to answer yes for any token (see
Access).

Also not verified: the VS Code and Claude Desktop setups in the guide, and
Claude Code tool calls longer than 60 s with the guide's timeout settings
(taken from the Claude Code documentation).

## History

Earlier gateway versions and the AWS-hosted ChatGPT app (retired on
2026-10-08) are in git history: commit `7b99311` and before.
