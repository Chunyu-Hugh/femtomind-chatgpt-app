# JAM gateway on LQCDMCP

A JAM gateway, `jam-gateway-<user>`, is an MCP server that runs as a Slurm
job on the JLab LQCD cluster, launched through Jie Chen's LQCDMCP platform.
It runs JAM fits on ifarm as the person calling it, from an MCP client
(Claude Code or Codex) or from the browser panel it serves, which mirrors
the FemtoMind JAM app's fitting campaign. A collaborator uses their own
gateway or a colleague's shared one, each with a personal token.

**Collaborators: the next section is written for your AI assistant.** Tell
it: *"Read the section 'For collaborators and their AI assistants' in
lqcdmcp-deploy/README.md and set me up."* The step-by-step guide for people
is [COLLABORATOR_GUIDE.md](COLLABORATOR_GUIDE.md); the rest of this file is
for whoever maintains the gateway.

## For collaborators and their AI assistants

This section is addressed to an AI assistant (Claude Code or Codex) that
helps a JAM collaborator connect to a JAM gateway and run fits. Follow it in
order. Where it says *the collaborator*, the person must do the step
themselves; do not do it for them.

### Rules

1. **Never ask for a secret in the chat.** The gateway token, the JLab PIN,
   the one-time code, the ifarm password and the GitHub token are never typed
   into the conversation. The token goes into a file the collaborator fills
   in (setup step 1); the cluster sign-in and the GitHub token go only into
   the panel page in their browser. If one is pasted into the chat anyway,
   tell the collaborator to have it replaced.
2. **Never start or stop a gateway on the node** unless the collaborator owns
   it and has agreed it with everyone using the node. All gateways run on
   `qcd24s100`, and starting or stopping any of them ends every other one,
   the shared gateway included (see "One node, and its prolog" below). Do not
   run `redeploy_jam.py launch` or `cancel`, `hot_redeploy.py`, or
   `deploy_and_verify.py` without `--check` for someone who uses a shared
   gateway.
3. **The token decides who is calling.** Leave `username` out of tool calls,
   or give the collaborator's own JLab username. Any other name is refused
   (`IDENTITY_MISMATCH`).
4. **Writes wait for the collaborator.** Submitting, adding replicas,
   rerunning, stopping and deleting return a plan and a confirmation phrase.
   Show the plan, ask the collaborator, and send the phrase back only after
   they agree. The gateway takes a phrase at the earliest 20 seconds after
   handing it out (`CONFIRM_TOO_SOON` before that). Do not work around a
   refusal; explain it.
5. **A published analysis comes from the catalog.** To reproduce or vary
   JAM24 (or any entry of `list_analysis_catalog`), build the card with
   `campaign_card_from_catalog` and put every change in its `overrides`. A
   hand-built card named after a catalog analysis is refused
   (`CATALOG_REQUIRED`), and so is an undeclared difference from the catalog
   (`CATALOG_MISMATCH`). `JAM24.final` is the published configuration;
   `JAM24.current` uses the updated data workbooks.

### What to ask the collaborator first

- Their JLab username.
- Whose gateway they will use: a colleague's shared one (the usual case; as
  of 2026-10-28 it is `jam-gateway-chunyu`, run by Chunyu Hu) or their own.
  Below, `<gateway>` stands for that name.
- That they have the personal token the owner handed them privately (for
  their own gateway: `python3 redeploy_jam.py token` made it, see
  COLLABORATOR_GUIDE.md 1.6).
- That they can sign in to `scilogin.jlab.org` (PIN and one-time code) and
  `ifarm.jlab.org` (password).
- For a first workspace: a GitHub token that can read the private repository
  `QCDHUB/fitpack_legacy`, or the path of a FitPack workspace they already
  have under `/w/ceph24/JAM/<username>`.

### Setup, once

1. **Store the token.** The collaborator runs this in their own terminal, not
   through the assistant, and pastes the token at the prompt; nothing is
   echoed. For their own gateway, the file already exists as
   `~/.config/jam_gateway/jam-gateway-<username>.token`; use that path in
   the export line instead.

   ```sh
   mkdir -p ~/.config/jam_gateway && chmod 700 ~/.config/jam_gateway
   stty -echo; printf 'JAM gateway token: '; read -r t; stty echo; echo
   (umask 077; printf '%s\n' "$t" > ~/.config/jam_gateway/shared.token); unset t
   echo 'export JAM_GW_TOKEN="$(cat ~/.config/jam_gateway/shared.token)"' >> ~/.bashrc   # or ~/.zshrc
   ```

2. **Register the gateway with the client**, once. Claude Code (the single
   quotes keep `${JAM_GW_TOKEN}` unexpanded, so Claude Code reads the token
   each time it starts):

   ```sh
   claude mcp add --transport http --scope user jam-gateway "https://lqcdmcp.jlab.org:8000/cloud/<gateway>/mcp" --header 'Authorization: Bearer ${JAM_GW_TOKEN}'
   echo 'export MCP_TOOL_TIMEOUT=900000 CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=900000' >> ~/.bashrc   # or ~/.zshrc
   ```

   Codex: add to `~/.codex/config.toml`

   ```toml
   [mcp_servers.jam-gateway]
   url = "https://lqcdmcp.jlab.org:8000/cloud/<gateway>/mcp"
   bearer_token_env_var = "JAM_GW_TOKEN"
   tool_timeout_sec = 900
   ```

   An older `jam-gateway` entry that sends `LQCDMCP_TOKEN` must be removed
   first (`claude mcp remove jam-gateway -s user`, or edit the Codex file).

3. **Restart the client** from a new terminal, so that `JAM_GW_TOKEN` is set,
   and call `check_hpc_connection`. Expect `auth: "token"` and
   `account.username` equal to the collaborator's JLab username; a shared
   gateway also shows `shared: true` and its `owner`. `connected: false` is
   normal before step 4.

4. **Sign in to the cluster on the panel.** The collaborator opens
   `https://lqcdmcp.jlab.org:8000/cloud/<gateway>/mcp` in a browser, pastes
   the same token when the page asks, and on the Connect tab signs in with
   their own PIN, one-time code and ifarm password. `check_hpc_connection`
   then shows `connected: true`. The sign-in lasts until the gateway job ends.

5. **Workspace, first time only.** Either the collaborator creates one on the
   panel's Project tab (Create and clone, with the GitHub token typed there;
   COLLABORATOR_GUIDE.md 2.3), or the assistant calls `use_workspace` with
   an existing workspace path. `check_project` reports what is missing.

### Each day

- The gateway is a Slurm job of at most 6 hours. When the client cannot
  reach it, or the proxy answers `{"error": "Backend not found"}`, it is not
  running: ask its owner. Do not launch one (rule 2).
- After every gateway restart the collaborator signs in again on the panel
  (setup step 4). Fits keep running, and results stay on ifarm, when the
  gateway stops.

### Running fits

The gateway sends its own instructions when the client connects, and the
`jam_guide` tool explains conventions (χ² definitions, dataset ids, Slurm
sizing, priors). A reproduction of the published JAM24 fit goes:
`list_analysis_catalog`, then `campaign_card_from_catalog` with
`analysis_id: "JAM24.final"`, then `validate_campaign` on that card (show
the collaborator the summary: 215 free parameters, 5853 points), then
`submit_campaign` with the phrase once they agree, then
`campaign_status` with `wait_for: "fitting"`. For other fits, ask the same
questions as the panel's Campaign tab, one step at a time.

When the collaborator asks how an earlier fit was made, which FitPack
version or seeds it used, or what changed between runs, call
`campaign_provenance` with the campaign (add `replicas` for single
replicas); `reproduce_campaign` runs replicas again, after the
collaborator confirms.

A call answers within 25 seconds. Longer work answers `status: "running"`
with a `job_id`: call `analysis_result` with it, one call at a time, until it
is done.

| Reply `code` | Meaning | What to do |
|---|---|---|
| `AUTH_REQUIRED` | no token, or a wrong one | check that `JAM_GW_TOKEN` is set in the client's shell; restart the client |
| `IDENTITY_MISMATCH` | `username` names someone else | leave `username` out |
| `CHAIN_DOWN` | not signed in to the cluster | setup step 4 |
| `CONFIRM_REQUIRED` | a write needs the collaborator's yes | show the plan, ask, then send the phrase |
| `CONFIRM_TOO_SOON` | phrase sent within 20 s | wait, then send it again, once the collaborator has agreed |
| `IDEMPOTENT_REPLAY` | the same request was already submitted | nothing new was queued; this is the earlier result |
| `CATALOG_REQUIRED`, `CATALOG_MISMATCH` | rule 5 | rebuild with `campaign_card_from_catalog`; declare changes in `overrides` |

### For the owner of a shared gateway

Make one token per person, hand each over privately, and relaunch in shared
mode; only the tokens' sha256 are kept, and a new token takes effect at the
next launch. Agree the relaunch with everyone on the node first (rule 2).

```sh
export JAM_GW_USER=<you> LQCDMCP_TOKEN='<dashboard token>'
python3 redeploy_jam.py user-token add <name>      # prints that person's token once
python3 redeploy_jam.py user-token list            # names only; revoke <name> removes one
python3 redeploy_jam.py cancel
JAM_GW_SHARED=1 python3 redeploy_jam.py launch
python3 deploy_and_verify.py --check
```

Then sign in again on your own panel.

## Files

| File | What it is |
|---|---|
| `jam_gateway.py` | The gateway (stdlib only): MCP tools, access check, cluster chain, campaigns, HTTP server. Runs on a cluster node. |
| `jam_panel.html` | The panel (Connect / Project / Campaign / Collections / Analysis), served by the gateway as a browser page and as an MCP Apps / ChatGPT widget resource. |
| `jamai/` | Turns a campaign into a FitPack input card and runs one replica (`run_replica.py`); uploaded to `<workspace>/.jamai/` at every submit. `object_registry.py` (the objects, their libraries, cuts and plot helpers; its source is also shipped to the cluster for reading libraries and plotting) and `reaction_registry.py` (which objects each reaction needs) are the physics maps. |
| `deploy_and_verify.py` | Launch your gateway unless your gateway job is already queued or running, wait for it (it stops early when the job stays queued, or when a job it launched ends), and check it end to end (`--check`: check only). Prints a shareable report without credentials. |
| `redeploy_jam.py` | `launch` / `cancel` / `status` through the dashboard API; `script` writes the job script for the dashboard page. |
| `hot_redeploy.py` | Replace a running gateway's code (gateway, panel, `jamai/`, `plugins/`) inside the same job. |
| `plugins/` | Other physics tasks on the same gateway, each in its own folder (NESAP SBI: a git submodule, optional; see Deploy). See [plugins/README.md](plugins/README.md). |
| `check_dashboard_token.py` | Make the dashboard calls the gateway's access check makes, and print the decision. |
| `gw_auth.py` | Gateway name, URL and token for the scripts: `JAM_GW_USER`, `LQCDMCP_TOKEN`; `JAM_GW_NAME`, `JAM_GW_URL`, `JAM_GW_TOKEN` for a test gateway. |
| `COLLABORATOR_GUIDE.md` | The step-by-step guide for collaborators. |

## Deploy

```bash
export LQCDMCP_TOKEN='...'    # dashboard token (Globus login), with or without "Bearer "
export JAM_GW_USER=<you>
python3 deploy_and_verify.py   # launch, wait, verify
```

**The NESAP SBI plugin is optional; JAM does not need it.** SBI lives in a git
submodule, `plugins/sbi/upstream` (Felix Ringer's `femtomind-nesap-sbi`). A plain
`git clone` or `git pull` leaves that folder empty, and JAM still works in full:
the gateway starts with every JAM tool, and `deploy_and_verify.py`,
`redeploy_jam.py` and `hot_redeploy.py` only print
`note: plugin sbi is left out of the gateway: ... git submodule update --init`
and deploy as usual. The gateway then has no `sbi_*` tools, and its instructions
do not mention SBI. To include SBI, fetch the submodule before deploying:

```bash
git submodule update --init      # or: git clone --recurse-submodules ...
```

`JAM_GW_PLUGINS=none` leaves every plugin out on purpose. (Checked 2026-10-19
on a clone of `main` without the submodule: 31 JAM tools, and `hot_redeploy.py`'s
check on the node passes.)

The job script carries the gateway, the panel and `jamai/` (zlib + base64)
and no credential. Its header is the one tested on 2026-10-23: the
reservation `chen_mcp_daily`, no partition line (the job goes to the
reservation's partition, `mcp`), no `--nodes`, `--ntasks=1`, 16 CPUs, 6 h and
the `hadstruc24s` account. (`JAM_GW_RESERVATION`, `JAM_GW_PARTITION`,
`JAM_GW_HOURS`, `JAM_GW_CPUS`, `JAM_GW_MEM` and `JAM_GW_ACCOUNT` override it;
`JAM_GW_PARTITION=none` and `JAM_GW_ACCOUNT=none` leave that line out.) The job
unpacks the files into `~/.jam_gateway/app`, and a
supervisor loop restarts the server whenever it exits. The server takes the
first free port from 8080 and registers with the proxy as
`jam-gateway-<user>`, at `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<user>/mcp`.

- **Accounts.** Jie Chen must first enable an account for MCP servers, add it
  to the user list of `chen_mcp_daily` and allow it the `hadstruc24s` account
  in partition `mcp`. Until then the dashboard's
  `launch_mcp_server_using_script` fails with
  `Slurm command failed: sudo sbatch --uid <user> ... (rc=1)`, and
  `deploy_and_verify.py` stops there with that explanation. The list on
  2026-10-23: chen, chunyu, edwards, jkarpie, wmoore, treyand.
- **The reservation is required.** The proxy reaches gateways only on
  `qcd24s100` (64 CPUs, in partition `mcp` since 2026-10-22, before that in
  `24s`), and `chen_mcp_daily` holds all of that node until 2027-10-15. A job
  without `--reservation=chen_mcp_daily` stays PENDING even when the node is
  idle: job 7342219 (`--partition=mcp`, no reservation) waited about 20 h.
- **One gateway at a time, unless started together:** see
  [One node, and its prolog](#one-node-and-its-prolog-running-two-gateways).
- **Shared mode.** `JAM_GW_SHARED=1` at launch (or `"shared": true` in
  `~/.jam_gateway/site.json` on the node) makes the gateway act as the
  user named by `username` in each call, with that user's own chain,
  area and workspace; without a username it acts as its owner.
  Each person has a token of their own (`python3 redeploy_jam.py user-token
  add <name>`; only its sha256 is stored, in `site.json` `"users"`), and a
  call acts as the token's user and nobody else: a `username` that names
  anyone else is refused. Meant for colleagues who cannot launch a gateway
  (see the guide, 2.5). A shared gateway launched without token hashes
  (legacy) still takes the typed username, and says LEGACY at start-up.
- **Dashboard page instead.** `python3 redeploy_jam.py script` writes
  `jam-gateway-<user>.slurm`; launch an MCP server with that name and file.
- **Upgrade a running gateway:** `python3 hot_redeploy.py`, then
  `python3 deploy_and_verify.py --check`. It uses the hidden tool `deploy`
  (absent from `tools/list`; the owner's token plus the admin secret in the
  `X-JAM-Admin` header, see Security model): it stages the new gateway, panel,
  `jamai/`, `plugins/` and `analyses/` in chunks under
  `~/.jam_gateway/app/.hot_redeploy.stage-<stamp>`, has the gateway verify
  that the complete staged application imports in a fresh interpreter
  (nothing is replaced if that fails), installs it (the previous application
  stays in `.hot_redeploy.previous`), cleans the stage and asks the server to
  stop, which the job's supervisor loop restarts. It then checks through
  `deploy status` that the answering server started after the swap and runs
  the uploaded file (sha256). It refuses a gateway that is not a Slurm job,
  since nothing would restart it. The job and the cluster sign-in survive.
  A gateway from before the deploy tool still has `node_diag` and answers
  "unknown tool": upgrade it once with `python3 hot_redeploy_legacy.py`
  (same job; it also writes the token and admin hashes to the node, so the
  restarted gateway takes the gateway token), or relaunch it.

### One node, and its prolog: running two gateways

Slurm itself would run four 16-CPU gateways on `qcd24s100`: it allocates CPUs
only (`SelectTypeParameters=CR_CPU`, so the default `ReqMem=1000G` blocks
nothing), with `OverSubscribe=NO` and `PreemptMode=OFF`. What ends them is the
node's Prolog and Epilog, `/usr/local/libexec/slurm/proepilog.sh`, the script
of every LQCD node, which assumes one job per node. Its "hanging job check"
sends TERM, then KILL, to every non-root process with a UID above
`SYS_UID_MAX`, whoever owns it and whatever job it belongs to; it also empties
`/scratch` and `/dev/shm`. Hence (sacct and `scontrol`, read on the node
through `node_diag` on 2026-10-23):

- **A gateway that starts kills every gateway already on the node.** Trey
  Anderson's 7342594 ended `FAILED` with exit code `0:15` (SIGTERM) at
  14:39:04, one second after 7342596 started; 7342597 the same way at
  14:42:41, one second after 7342598, a second gateway of the same user,
  started. The two jobs' headers were identical; leaving out `--nodes=1` or
  adding `--mem` changes nothing.
- **A gateway that ends kills the others too**, since the Epilog runs the same
  check: stopping one, or one reaching its 6 h limit, takes the rest down, and
  with them the cluster sign-in (its ControlMasters are user processes too).
- **Gateways submitted together all survive**: their Prologs run before any
  gateway process exists. At 14:45 two gateways launched at the same moment
  (7342599 and 7342600) both ran, on ports 8080 and 8081, and both answered
  through the proxy.

To run two, until the Prolog changes: stop every gateway on the node, then
launch them in the same moment and verify each.

```bash
JAM_GW_NAME=jam-gateway-<you>  python3 redeploy_jam.py launch &
JAM_GW_NAME=jam-gateway-<you>2 python3 redeploy_jam.py launch &
wait
JAM_GW_NAME=jam-gateway-<you>  python3 deploy_and_verify.py --check
JAM_GW_NAME=jam-gateway-<you>2 python3 deploy_and_verify.py --check
```

Two people do the same: both stop their gateways, then each runs
`python3 redeploy_jam.py launch` in the same minute, and each signs in on
their own panel. After that nobody starts or stops a gateway until all are
done, and all stop together. Two gateways of one user share
`~/.jam_gateway/app`; both jobs unpack the same files into it.

The lasting fix is the system team's: skip the kill step (and the scratch
cleanup) on `qcd24s100`, or for jobs in `chen_mcp_daily`. Then gateways can
start and stop independently, four at a time.

## How it works

- **Access.** Every `tools/call` needs `Authorization: Bearer <gateway
  token>`; `initialize` and `tools/list` need none, and the LQCDMCP proxy
  forwards `/cloud/<name>/mcp` without checking anyone. The gateway token is
  the gateway's own: `redeploy_jam.py` makes a secret per gateway
  (`~/.config/jam_gateway/<name>.token`, mode 600; `python3 redeploy_jam.py
  token` shows it) and writes only its sha256 into the job script
  (`JAM_GW_TOKEN_SHA256`, with `JAM_GW_DASHBOARD_AUTH=0`). The gateway
  compares `sha256(bearer)` with it in constant time and never asks the
  dashboard. The dashboard token (`LQCDMCP_TOKEN`) only launches, cancels and
  inspects the job. A doubled `Bearer ` is tolerated; a token over 1024
  characters is refused unread. `JAM_GW_TEST_TOKEN` is the plain secret of a
  test instance (below).
  **Why not the dashboard.** Until October 2026 the gateway asked the
  dashboard about every token (`validate_user` for its owner, then
  `get_my_mcp_servers`). The dashboard answered yes for any token (tested
  with a made-up token and with none): `validate_user` trusts its `username`
  argument, which the gateway set to its owner. So that check kept nobody
  out. A gateway launched without a token hash still runs it (legacy mode),
  prints `auth: dashboard (LEGACY ...)` at start-up, and
  `deploy_and_verify.py` reports that a made-up token is accepted; relaunch
  it with the current `redeploy_jam.py`.
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
- **Objects and libraries.** `jamai/object_registry.py` defines the objects:
  `pdf` (Unpolarized PDF, reactions idis and dy), `pdf-pion` (Pion PDF,
  reactions dy-pion and ln) and the `p->pi,n` companion that leading-neutron
  data needs. A library is read on the cluster by running the registry's
  source: the proton table by executing `<workspace>/examples/input02.py`
  (`conf['params']['pdf']`, plus each reaction's `datasets` entry, which
  gives the normalization parameters and default cuts) on the login host;
  the pion and companion tables from the final-fit reference
  (`PION_REFERENCE`, a NumPy pickle in `/w/ceph24/JAM/cyang/...`, read
  inside the container), restricted to one shape (`one_shape_pion_params`
  drops the `*2` blocks and refuses a tie into them). When the reference
  cannot be read, `campaign_options` still answers for the proton path and
  marks Pion PDF disabled with the reason. Nothing is cached: every
  `campaign_options`, `campaign_datasets` and card check reads the files
  again, and the card records each library's sha256, which the materializer
  checks again at submit. `jamai/reaction_registry.py` maps reactions to
  the objects they need and, for `ln`, adds the fitted `p->pi,n` companion
  and freezes the proton `pdf` (replaced by the reference's) unless `pdf`
  is also selected. A fitted `pdf-pion` selects FitPack's `dis version` 0,
  everything else 1 (FitPack's own default); this is a temporary policy
  recorded in `DIS_COMPATIBILITY`.
- **Campaigns.** The card selects one or both objects (`objects:
  [{type, parameters}]`; the older single `object` is still accepted) and,
  for leading neutron, `companions` edits of `p->pi,n`. Only the rows the
  user changed go into the card: value, min, max, FIXED, or `equal_to`
  another parameter of the same object (FitPack's `'fixed': '<name>'`);
  dataset normalizations likewise (`'fixed': <int dataset id>` ties one to
  another). Cuts are a list per reaction, or a mapping per dataset id
  (leading neutron), which FitPack's `tools/reader.py` reads as a
  `filters` dict. `jamai` copies `input02.py` to `stages/pdf_stage/input.py`,
  prepends the pion library and its settings when a pion object is active,
  and appends an override block. A Slurm array runs one replica per task in
  the site's container: `jamai/run_replica.py` applies max iterations and
  the optimizer, guards FitPack's status report, then runs `fitlib/ai.py`.
  The status report (`fitlib.resman.RESMAN.gen_report`, printed while
  fitting) is output only but reads each data table's `col`, `obs` and
  `target`; idis 10005 has no `col`, and the `KeyError` made `ai.py` fall
  back to χ² at the starting values. Now an error there becomes one line of
  the report, such as `(status report unavailable: KeyError: 'col')`, and
  the fit goes on. The array sets `LHAPDF_DATA_PATH` to the shared
  `lhapdf_extra` tables (never a personal folder). The registry is
  `<workspace>/campaigns/collections.json`. Stopping a campaign cancels its
  jobs that still have tasks in the queue; deleting is refused while any of
  its tasks is queued. When `squeue` fails the queue is unknown, never empty:
  stop and delete refuse, and the lists say so.
- **Data check.** `validate_campaign` also reads each selected dataset's
  `database/<reaction>/expdata/<id>.xlsx` in one container call, derives the
  cut variables as FitPack's readers do (idis `W2` or `X`, dy `Q2`; the
  pion reactions and idis under `dis version` 0 through FitPack's own
  readers) and applies each enabled cut with `tab.query`, as
  `tools/reader.py` does. It returns `summary.points_after_cuts`
  (`{"idis:10005": 33}`) and `warnings`, which never block: no points left
  after the cuts, a cut that cannot be evaluated, a missing column that the
  status report reads. If the check fails, validation still succeeds, with
  one warning saying so. Only a card whose selected datasets all keep no
  point is refused, by `validate_campaign` and `submit_campaign` alike: FitPack
  would run it and report success with the starting values and no χ²
  (`test_OCT_13`, JLab Hall C 10001 and 10004 under `W2>10`, 2026-10-13).
- **Replica states.** When the optimization fails, `fitlib/ai.py` computes
  χ² at the starting values instead and still writes `"status": "success"`,
  with `"mode": "validation_only"` and no `.msr` file. So a replica is
  fitted only with status `success` and mode `fit` (a summary without
  `mode`: when its `msr_path` exists), not fitted with any other `success`,
  and failed otherwise or when its summary cannot be read. A `success`
  without `chi2_total` is failed as well: no data point entered the fit, and
  `campaign_logs` says so instead of "fitted".
  `_REPLICA_STATE` decides this for the status, postprocess, plot and log
  snippets alike: `list_campaigns` and `campaign_status` count `succeeded`
  (fitted), `not_fitted` and `failed` (state `not fitted` when nothing is
  queued and every finished replica did not fit), and postprocessing builds
  the statistics from fitted replicas only (`not_fitted_replicas` apart).
  Reported χ² uses central-data point residuals only; penalties and bootstrap
  diagnostics are separate. `good_fraction` is selected/finished-fitted only
  when a scientific quality criterion is specified, otherwise N/A;
  `completion_fraction` is finished/submitted. Data vs theory takes a dataset's
  experiment name from its data file's `col` column, else from
  `DATASET_EXPERIMENTS` (idis 10005 has no `col`), so "HERMES" finds it.
- **Plots.** `campaign_pdf_plots` evaluates every fitted replica's
  distribution (`object_key` pdf or pdf-pion; pion flavors valence = ub − u,
  sea = u, in the π⁻ convention) at Q² and draws the curves, the mean and
  an equal-tailed pointwise credible interval (`credible_interval` in
  percent, default 95, at most 100 = the full envelope; it needs at least
  two fitted replicas). The answer carries the interval metadata and which
  replicas were excluded and why.
- **Logs.** `campaign_logs` reads, in one plain-Python call on ifarm, each
  replica's `fit_summary.json`, its Slurm stdout and stderr logs under
  `/farm_out/<user>/` (the job name, job ID and array task distinguish each
  replica) and the queue. It returns each replica's state, χ² and whether
  either log holds a Python traceback, and for the first replica that did not
  fit, failed or left no output (or for replica N) the traceback and the ends
  of both logs (`lines`, default 80, at most 400). Older campaigns keep using
  their workspace log.
- **Reproducibility.** `submit_campaign`, `append_replicas` and
  `rerun_replicas` each add one entry to `<campaign>/reproduce.json`
  (`femtomind.campaign_reproduction.v1`), written on ifarm right after
  `sbatch`. It holds the workspace's FitPack commit, branch, remote (any token
  stripped) and changed tracked files, with the uncommitted changes saved as
  `fitpack_<sha256 prefix>.diff` beside it (up to 50 MB; a larger diff is
  noted, not saved). Untracked files that are not ignored, other than
  `campaigns/`, `.jamai/` and links, go into the same diff as new files when
  they are regular files of at most 2 MB and the diff stays under 50 MB
  (`fitpack.untracked.saved`); the rest are listed with size and sha256
  (`not_saved`). `git apply` of the diff on a clone at the commit gives the
  workspace back. The container is recorded by path, real path, size,
  modification time and sha256 (about 10 s per image, cached in
  `campaigns/.image_sha256.json` by path, size and mtime). The external data
  folders the replicas read (the LHAPDF tables and the shared grids) are
  recorded by real path and a sha256 of their sorted (path, bytes, mtime)
  listing, not their content. It also records the sha256 of each JAMAI file,
  of `campaign.request.json`, `manifest.resolved.yaml` and `input.py`, and the
  gateway's own sha256. The seeds are read from `input.py` and derived as
  `fitlib/ai.py` does: parameter seed `base + replica`, bootstrap seed
  `base + 100000 + replica` (base 120000 unless the card sets one), listed for
  every replica (verified on 2026-10-25 against `fit_summary.json` of a live
  campaign; a rerun of a replica reproduced its results byte for byte).
  `how_to_reproduce` says how to use the record. Writers take
  `reproduce.json.lock` (flock), so an append and a rerun submitted together
  both keep their entries; an unreadable record is set aside as
  `reproduce.json.unreadable.<time>`, never overwritten. If the record cannot
  be written, the jobs stay queued and the answer carries a `warning`. Each
  array task also notes the FitPack commit and the number of changed tracked
  files it found at start (`git` without index writes), and `run_replica.py`
  writes them, with the image and its real path, Slurm ids, library versions
  and JAMAI hashes, to `replica_<n>/replica_provenance.json` before the fit.
  `fit_summary.json` keeps the `seed` and `bootstrap_seed` that `ai.py` used.
- **Why a fit stopped, and fits that stopped far from the data.** FitPack
  writes status 'success' whenever its optimizer returns, wherever that is.
  The runtime keeps SciPy's result (status, message, nfev, njev, optimality) in
  `checkpoints/replica_<n>/current_progress.json`; `campaign_logs` gives each
  finished replica's `stopped_because` in words (ftol, xtol, gtol, the
  evaluation limit, interrupted), and the summary counts them
  (`optimizer_stops`). A fitted replica whose central-data chi2/N is 5 or more,
  the 'critical' band of the panel and jam_guide, is listed in `far_from_data`
  with its chi2/N and stop reason, and `campaign_summary`, `postprocess_campaign`
  and `campaign_report` say so in words, with the exclude selection that would
  leave them out. Nothing is left out automatically. Found on 2026-10-30: two of
  three finished replicas of a single-stage JAM24.final fit from the cataloged
  Gaussian starts ended at chi2/N 538 and 3301, reported as fitted.
- **Automatic recovery, if the submission asked for it.** A card's
  `job.recovery` = `{timeout: 'resume'|'stop', oom: 'more_memory'|'stop',
  max_attempts: 1-3}` (off by default) is part of the confirmed card, so the
  submission's phrase covers it, and the summary says in words what it allows.
  Every `JAM_GW_RECOVERY_INTERVAL` seconds (300; 0 turns it off) the gateway
  looks at its signed-in users' campaigns with a policy: a replica with no
  result whose latest task Slurm ended with TIMEOUT runs again through
  `rerun_replicas(resume=true)` (from its checkpoint; a refused checkpoint is
  not replaced by a fresh start), one ended OUT_OF_MEMORY with twice the memory
  of that task, up to 64G. At most `max_attempts` per replica; a refusal (no
  checkpoint, sbatch refused, the memory cap) stops recovery for those replicas.
  Nothing else is resubmitted. Every attempt is in the registry entry
  (`campaign_status` shows `recovery`: the policy, the attempts, the last ten
  actions), the audit log (`auto_rerun`) and reproduce.json (an ordinary rerun
  entry). A gateway with the human gate refuses a recovery policy.
- **The cache on compute nodes.** When at least `JAM_GW_CACHE_SLURM_MIN` (12)
  replicas wait for their central-data predictions, the gateway submits a Slurm
  array (`jam_cache_<campaign>`, up to `JAM_GW_CACHE_WORKERS` tasks, 16, one per 8
  replicas, the campaign's partition and memory, 1 CPU, 2 h) instead of
  evaluating them on the login host in batches of 8, each with its own FitPack
  data load. Each task loads the data once, evaluates the replicas whose number
  falls to it and journals each one; the gateway then publishes the cohort,
  evaluating whatever a task left. While it runs, cache_progress shows the job,
  its tasks and how many are left; a refused sbatch is shown there too, and the
  login-host path does the work. Checked on 2026-10-29 on test_OCT_9: three
  tasks (3, 4 and 3 replicas, 7 s each) and the login-host build give the same
  cohort, every prediction and chi2 identical. `JAM_GW_CACHE_WORKERS=0` turns it off.
- **A fit against the published one.** `compare_with_reference(collection_id)`
  (default `JAM24.final`) reads the campaign's central-data cohort and the
  registered archive on ifarm (in the container, `jamai/reference_comparison.py`)
  and compares every dataset whose data (value and alpha) are identical on both
  sides: the χ² of the mean prediction and the mean replica χ² (point term, as
  `campaign_summary`), the published ensemble's, and a null distribution from
  as many published replicas as the campaign has, drawn without replacement
  `subsamples` times (1000), the same draws for every dataset: z, two-sided
  p (+1 corrected), Benjamini-Hochberg and Holm corrected p, the global sums,
  the datasets that differ at p_BH < 0.05, and those not compared with why.
  Pages of 20 datasets, smallest p first; the computation is kept per cohort,
  so later pages are instant. Each row carries the dataset's label (`TASSO pion
  (√s = 44 GeV)`), so a reply names it without a `campaign_datasets` call.
  `replicas` takes the selection `campaign_summary` takes (`{'exclude': [1, 4]}`,
  `chi2_max`, `best`, ...): only the kept replicas are compared, the null is
  drawn at that size, and `replica_selection` says which were kept and why
  the others were not, so replicas stuck far from the data need not swamp it.
  Checked on 2026-10-29 on a JAM24.final campaign
  of 3 replicas: all 96 datasets comparable, the archive side equal to the
  archive's own chi2_data, the campaign side equal to campaign_summary's table.
- **Per-replica numbers.** `campaign_replica_values(collection_id, kind)`:
  `chi2` reads each fitted replica's central-data χ² from the cache (total, by
  reaction, and chi2, chi2_cor, chi2_norm and norm for up to 20 named
  datasets); `parameters` reads each replica's `fitted_parameters.json` (mean,
  sd, min and max of every parameter, and each replica's values for up to 20
  named ones). Pages of replicas; statistics over all selected.
- **The data-vs-theory table in pages.** `analyze_by_experiment` answers with
  `limit` datasets (20) from `offset`, `sort='chi2_red'` worst first, with the
  charts, PNGs and experiments of the page: for JAM24 the reply to a client went
  from 242 kB to 36 kB. The panel asks with `all=true`.
- **Reading the record back.** `campaign_provenance(collection_id)` reads
  `reproduce.json` on ifarm and answers in one call (read-only, about 1 s):
  every submission (submit, append, rerun, reproduce) with its job, replicas,
  FitPack commit, branch and remote, whether uncommitted changes and untracked
  files were saved (the diff file, its sha256, whether it is still there),
  the image and its sha256, the external data fingerprints, one fingerprint
  for the whole JAMAI package, the gateway version, the fit settings and the
  seed rule; and which of these changed from one fit of the campaign to the
  next (reproductions left out; a new gateway version alone is listed, not
  counted; the image, external data and untracked files that records before
  2026-10-26 did not measure appear as `first_recorded`, not as a change).
  `replicas` (`'1,3'`, `'2-4'`, `'all'`; pages of `limit`, default
  20, at most 100, from `offset`) adds per replica the submission that
  produced it, its recorded seeds, the host, node type, CPU, commit, image
  and library versions from `replica_provenance.json`, status, fitted
  parameter count and `optimizer_chi2` from `fit_summary.json` (the
  optimizer's own score on the replica's pseudo-data, not the central-data
  χ² that `campaign_summary` reports), whether the seeds it used and its
  commit match the record, and each reproduction of it with its node type
  and whether its fitted parameters equal the current ones. A history of more
  than 20 submissions shows the first and last 10. Use it instead of paging
  through `fs_read` with `offset` for a large record. Checked on
  2026-10-28 against the October test campaigns on ifarm: in the one
  reproduced four times, the three runs on `farm18` nodes have the same
  fitted parameters and the one on `farm19` does not.
- **Reproducing a campaign.** `reproduce_campaign(collection_id, replicas)`
  runs recorded replicas again exactly. It finds the submission that last
  produced each replica in `reproduce.json` (replicas from different
  workspace states are refused together), measures the live workspace with
  the same code and compares: when commit and changes still match, the
  replicas run in the live workspace (`in_place`); otherwise a clone of the
  workspace at the recorded commit with the recorded diff applied is built
  under `<area>/reproductions/<campaign>/<stamp>/fitpack`, verified (commit,
  the untracked files' sha256, the same changes sha256 the record has, the
  site's grid links, a `.jam_reproduction.json` marker) and used (`clone`).
  The current JAMAI package is uploaded into it; whether its files equal the
  recorded ones is reported. The job is the ordinary array script with the
  code workspace, output folder and name changed: results go to
  `stages/<stage>/reproduce/<stamp>/replica_<n>` (the current results are
  not touched; logs are `/farm_out/<user>/jam_repro_<campaign>_<job>_<n>.*`),
  and the run is itself an entry of kind `reproduce` in `reproduce.json`
  (with `reproduces_job_id`, `mode`, `workspace_used`, `output`). Like every
  submission it asks for a confirmation phrase first. A record whose diff was
  too large to save cannot be rebuilt, and the tool says so. Afterwards
  `reproduce_campaign(collection_id, check='latest')` (or a run stamp)
  compares each replica's `fit_summary.json` (paths aside) and `msr` files
  with the current ones: `identical`, `differs` (with the differing keys and
  both values), `running`, `no_output`, each with the hosts of both runs.
  **Node type matters:** on 2026-10-26 the same replica reproduced byte for
  byte three times on `farm18` nodes and differed at the 2e-6 level on a
  `farm19` node (same code, image, seeds and libraries: different CPU, so
  different vectorised arithmetic). `reproduce_campaign` therefore asks Slurm
  for the original node type (`#SBATCH --constraint=farm18`, from the host in
  `replica_provenance.json`; `node_constraint='none'` lifts it, a feature name
  sets it), the check names the node types when a differing pair ran on
  different ones, and `replica_provenance.json` records the CPU model.
- **Registry merges.** An upsert of `campaigns/collections.json` merges with
  the stored entry: the union of both job lists (by job id, the newer record
  of a repeated id) and the larger replica count, so an append and a rerun
  that read the entry before either wrote both keep their jobs (one such
  lost update happened on 2026-10-25).
- **Confirmation phrases.** `submit_campaign`, `append_replicas`,
  `rerun_replicas`, `reproduce_campaign`, `cancel_campaign`,
  `delete_campaign` and `submit_slurm_job` act only when called again with
  the phrase from a first call (`CONFIRM JAM <kind> <hash>`), which ties it
  to exactly that card or plan. The gateway remembers when it first showed
  each phrase and refuses a confirm that comes within 20 seconds of that
  (`CONFIRM_TOO_SOON`), or carries a phrase it never showed
  (`CONFIRM_NOT_SHOWN`, after a restart): on 2026-10-26 Claude Code, asked in
  one message to reproduce a campaign, confirmed its own plan in the same
  turn without the user seeing it. Every `needs_confirmation` answer also
  carries the rule in its message. `JAM_GW_HUMAN_GATE=1` goes further and
  keeps the phrase off AI clients altogether (the panel shows it).
- **Other Slurm jobs.** `draft_slurm_job` writes `<workspace>/slurm/<name>.sh`
  and checks it with `sbatch --test-only`; `submit_slurm_job` submits it.
  Their stdout and stderr go to separate, unique files under `/farm_out/<user>/`.
- **Partition.** Auto: `priority` when the wall clock per replica is up to
  4 h, `production` beyond.
- **Reading files.** `fs_read` returns a byte window and always says where it is
  (`total_bytes`, `offset`, `returned_bytes`, `returned_lines`, `truncated`,
  `next_offset`): a large log or `reproduce.json` is paged with
  `offset=next_offset` instead of coming back as an unexplained fragment, and
  `max_bytes` (at most 20000) asks for more at once. `fs_list` likewise reports
  `total_entries`, `returned` and `truncated`. Both run a small `python3`
  snippet on the compute host, so the size and the window are measured there
  rather than by the ssh helper's 4 KB output cap.
- **Tools.** `tools/list` is the contract (42 JAM tools): the cluster and
  workspace (ping, get_console, open_panel, connect_hpc, check_hpc_connection,
  check_project, setup_workspace, use_workspace, use_ai_branch, fs_list,
  fs_read), the campaign (campaign_options, campaign_datasets,
  validate_campaign, preview_prior_replicas, submit_campaign, list_campaigns,
  campaign_status, campaign_logs, append_replicas, rerun_replicas,
  reproduce_campaign, cancel_campaign, delete_campaign), its results
  (postprocess_campaign, campaign_summary, analyze_by_experiment,
  campaign_pdf_plots, campaign_ff_plots, get_thesis_plot, analysis_result),
  other Slurm jobs (slurm_partitions, draft_slurm_job, submit_slurm_job), the
  published catalog and reference results (list_analysis_catalog,
  get_analysis_catalog, get_analysis_requirements,
  preview_analysis_configuration, preview_compiled_input,
  list_analysis_results, get_analysis_fit_statistics,
  get_analysis_theory_predictions), pending_confirmations (human-gate mode);
  hidden: deploy. Every tool carries MCP annotations (read-only, destructive,
  idempotent). `JAM_GW_TOOLS_DENY` leaves tools out (see Security model). The
  AI-facing workflow is `INSTRUCTIONS` in `jam_gateway.py`.

**Test instance.** `jam_gateway.py` also runs off the platform, on a machine
that can reach scilogin: set `JAM_GW_OWNER` (the JLab user), `JAM_GW_NAME`,
`JAM_GW_PORT`, `JAM_GW_BIND`, `JAM_GW_PUBLIC_URL` (the URL the panel links
use), `JAM_GW_ACCESS_DIR` (its log folder) and `JAM_GW_TEST_TOKEN` (the token
it accepts, since the dashboard does not know such a gateway). The scripts
reach it with `JAM_GW_URL` and `JAM_GW_TOKEN`; `hot_redeploy.py` refuses it
(no supervisor loop restarts it).

## Security model

### What the gateway is

One process per owner on a cluster node, reached through the LQCDMCP proxy
(which checks nobody). It holds the owner's open SSH chain, so access to it is
access to the cluster as the owner. The gateway therefore decides who may
call it, what a call may do, who may change its code, and records all of it.

### Who may call it

- **Gateway token.** `redeploy_jam.py` makes a secret per gateway and puts
  only its sha256 into the job script (`JAM_GW_TOKEN_SHA256`). The gateway
  compares `sha256(bearer)` with it in constant time and never asks the
  dashboard. `python3 redeploy_jam.py token` shows the token; `rotate token`
  replaces it (relaunch for it to take effect).
- **Shared gateway.** One token per person (`redeploy_jam.py user-token add
  <name>`; `site.json` `"users"` holds the hashes). A call acts as the
  token's user and nobody else; `username`, when given, must be that user.
- **Legacy mode.** A gateway launched without a token hash runs the
  October-2026 dashboard check, which lets anyone through. It says so at
  start-up and in `deploy_and_verify.py`.
- **Test instance.** `JAM_GW_TEST_TOKEN` is the plain secret of a gateway
  that runs off the proxy (never set in a launch).

### What a call may do

- A call answers within `JAM_GW_REPLY_WAIT` seconds (25): the LQCDMCP proxy
  closes a request after 30 s (HTTP 504). Longer work answers `status:
  running` with a job id for `analysis_result`, and `campaign_status(wait_for=)`
  waits at most that long per call. A client waiting for a fit calls
  `campaign_status` again and again, so it answers with the card in brief
  (counts, QCD, job, Slurm; 6 kB for a JAM24 campaign instead of 104 kB);
  `detail='card'` gives the whole card summary.

- `tools/list` is the contract. A hidden tool (`deploy`) does not exist for
  anyone but the owner; a denied tool (`JAM_GW_TOOLS_DENY="a,b"` or
  `site.json` `"tools_deny"`) does not exist on that deployment: absent from
  the list, "unknown tool" when called, unnamed in the instructions. With the
  reference-results tools denied, the archive is not a prior source either.
  `check_hpc_connection` reports the effective list.
- A write (submit, append, rerun, reproduce, cancel, delete, submit_slurm_job)
  needs a confirmation phrase, `CONFIRM JAM <kind> <hash>`, bound to the exact
  request: any change to the request changes the phrase. The same confirmed
  request within `JAM_GW_IDEMPOTENT_HOURS` (6; 0 turns it off) is answered
  with the earlier result (`IDEMPOTENT_REPLAY`) instead of queued again.
  The phrase is a check that the confirmed plan is the plan that runs; it is
  not a credential and not, by itself, proof that a person agreed (an AI
  client receives it and can send it back).
- **A published analysis is reproduced from its catalog entry, or not at
  all.** A campaign whose name claims a catalog analysis (`JAM24...`) but whose
  card was not built with `campaign_card_from_catalog` is refused
  (`CATALOG_REQUIRED`): the library defaults are another model (140 free FF
  parameters, not JAM24's 215). A catalog-built card may differ from the
  catalog only where the user declared the change in `overrides`, which the
  card records; any other difference withholds the confirmation phrase and
  refuses submission (`CATALOG_MISMATCH`). On a deployment where the catalog
  tools are denied there is no catalog to claim, so neither rule applies.
- **Human gate.** `JAM_GW_HUMAN_GATE=1` (or `site.json` `"human_gate"`) keeps
  the phrase away from AI clients: a reply that needs confirmation says a
  person must read the phrase off the panel page, whose "Pending
  confirmations" card lists it (`pending_confirmations` answers only to
  requests carrying the nonce the served page holds). Off by default.
- Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`), so hosts can show and gate them.
- `delete_campaign` first copies `reproduce.json`, the request and the
  registry entry to `<area>/.jam_trash/<folder>/`.
- Refusals carry a stable `code` (`REFUSAL_CODES` in `jam_gateway.py`)
  beside their message.

### Who may change the gateway's code

`deploy`: the owner's token AND a second secret, the admin secret
(`JAM_GW_ADMIN_SHA256`; the `X-JAM-Admin` header, or `_meta` `jam/admin` for
a proxy that strips headers; the LQCDMCP proxy passes the header and drops
`_meta`, measured 2026-10-28). Eight fixed operations: stage a file in chunks, seal
it with its sha256, verify the complete staged application in a fresh
interpreter, install with rollback, clean, restart, status, node_info. Paths
are confined to the application package; stage names to timestamps. There is
no shell on the node. Without an admin hash the tool is off (`ADMIN_DISABLED`)
and the gateway is upgraded by relaunching.

### What is recorded

`~/.jam_gateway/audit.jsonl` (mode 600, hash-chained: each line carries the
sha256 of the line before) gets one line per `tools/call` (the token's user
and the user acted as, which check accepted the token, session and request
ids, the tool, the argument keys and a digest of the arguments, the outcome's
status and code, the duration), per finished background job and per write
effect. Never argument values: the PIN, passwords, GitHub tokens and campaign
cards stay out; the confirmation phrase appears only as its sha256. Records
about a user are also appended to `<area>/.jam_audit.jsonl` through that
user's own chain (`JAM_GW_AUDIT_MIRROR=0` keeps the log local).
`python3 audit_verify.py <file>` checks the chain; `--anchor` prints
`seq:head` to keep elsewhere.

### What it does not protect against

Anyone holding the gateway token; the owner's own account (the same uid can
rewrite the local log, hence the mirror and the anchor); a proxy that strips
headers (the admin secret could then travel in `_meta`, which the LQCDMCP
proxy, as it happens, drops while passing the header); and everything on the
cluster side (Slurm, filesystems), which is unchanged. The proxy registers a
gateway's route without authentication, and the gateway listens on
`0.0.0.0`; both are the platform's to change.

### Settings

`JAM_GW_TOKEN_SHA256`, `JAM_GW_ADMIN_SHA256` (or the files
`~/.jam_gateway/{token,admin}.sha256`, mode 600), `JAM_GW_DASHBOARD_AUTH=0`,
`JAM_GW_TOOLS_DENY`, `JAM_GW_IDEMPOTENT_HOURS`, `JAM_GW_HUMAN_GATE`,
`JAM_GW_AUDIT_MIRROR`, `JAM_GW_APP_DIR`; operator side `JAM_GW_TOKEN`,
`JAM_GW_ADMIN`, `JAM_GW_SECRETS_DIR` (default `~/.config/jam_gateway`).

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

## Conversation PNG delivery

`get_thesis_plot` is the image-only MCP entry point for natural-language requests
to show thesis-style figures. It reuses the existing experimental data/theory,
PDF and FF backends and always enables PNG export. Scientific calculations,
replica selection, credible intervals and styling are unchanged. There is no UI
resource/template metadata on this tool; the standalone panel and existing
interactive plotting tools remain available when the user explicitly wants them.

Request one plot family, with an existing campaign name/collection ID and the
same scientific options exposed by its backend:

```json
{"plot_type":"experimental","collection_id":"my_campaign","options":{"datasets":"wc:10001,wc:20001","credible_interval":95}}
```

```json
{"plot_type":"pdf","collection_id":"my_campaign","options":{"object_key":"pdf","flavors":"g,db+ub,sp,Rs","q2":10}}
```

```json
{"plot_type":"ff","collection_id":"my_campaign","options":{"modes":"combined,individual","hadrons":"pion"}}
```

`plot_type` is `experimental`, `pdf` or `ff`. `options` is optional and follows
that family's discovery schema/defaults. PNG export cannot be disabled here;
interactive-only/configured-value previews are not accepted as fitted figures.
Unknown options are rejected. There is no arbitrary path/URL argument.

If a call returns `status:running`, use **the same tool** with only
`{"job_id":"<returned ID>"}` until done. Do not switch to `analysis_result` or
`open_panel`. The existing owner-scoped asynchronous backend job is reused;
polling neither restarts the analysis nor changes the shared interactive result.
Jobs expire after the existing retention window and do not survive restart.

Completion returns native MCP `image/png` content, one independent block per
figure, plus concise existing scientific metadata and ordered `plots` entries
with zero-based `image_index`, byte size and SHA-256. PNG bytes are not resized,
re-encoded or recomputed by the delivery adapter. Widget charts/data URLs are
excluded. Missing PNGs produce an explicit error with the backend's rendering
reason; there is no automatic app or alternate-style fallback. Metrics tools
remain unchanged (`analyze_by_experiment`, `campaign_summary`).

Plotting may evaluate existing fitted replicas and write its existing caches and
PNG outputs, so the tool is not advertised as read-only/idempotent. It never
initializes replicas, fits, submits jobs or modifies fitted results. Client
presentation of native MCP images still needs a fresh-client test after approved
deployment: protocol image delivery alone cannot guarantee a particular Codex
version's inline/persistent display behavior.

On-demand verification uses the actual backends, not an existing-PNG lookup:
PDF/FF requests always render a new figure; missing numerical curves invoke the
existing fitted-replica evaluator. Experimental requests build missing/stale
per-dataset predictions, then run the existing thesis renderer. Only numerical
predictions are reused; interval, selection, ordering and display options are
applied again for each render. Experimental outputs use a unique request
directory so concurrent requests cannot overwrite one another's PNGs.

Cache identities include exact binary64 Q2/range keys, exact grid coordinates,
curve shapes/version, SHA-256 of each fitted `.msr`, summary metadata and stage
input content. Old timestamp-only/rounded-key caches are invalidated naturally;
no cache files are deleted or migrated. Curve JSON retains full float precision,
so a repeat does not round a previously computed band. A first request after
upgrade can therefore require reevaluation and write larger caches. These are
cache/output-isolation changes, not changes to FitPack science or figure style.

Known rendering limitation: some two-flavor FF requests (observed for `up,g`)
have no clear in-panel space for annotations at the approved font sizes. The
existing renderer reports this explicitly and returns no image. The delivery
tool does not silently choose a different layout, substitute a saved figure or
change styling. A separately requested six-flavor layout passed validation.

## Looking inside datasets, and campaign reports

**`inspect_datasets`** (read-only) answers "what does this dataset hold and
what do my cuts keep" from the data files, read the way the fit reads them.
`_DATA_LOAD` is the loader the validation's data check (`_DATA_CHECK`)
already used: the reaction's FitPack reader in the site container, then each
cut in order with `tab.query`, a cut that cannot be evaluated skipped as
`tools/reader.py` skips it. `_DATA_INSPECT` adds, per dataset: the points in
the file, the points each cut keeps in turn, cuts FitPack would skip
silently, kinematic ranges before and after the cuts, labels, and the
uncertainties as `tools/residuals.py` reads the columns (a `%` column is a
percentage of `value` and becomes absolute; `_u` columns add in quadrature
to alpha; `_c` columns without `norm` are correlated; the one `_c` column
with `norm` gives dN = column / value at the first nonzero point, and two
such columns are reported because FitPack stops on them), with `rows` the
first points. Input: `reaction` + `datasets` (cuts: structured
`{variable, op, value}` only, as cards; default the library's cuts; SIDIS
and SIA always use the catalog's filters), or `collection_id` for a
campaign's own datasets and cuts. At most 40 datasets per call: a larger
request is refused with the count per reaction and the ids in
`datasets_by_reaction`, to narrow by `reaction` and, for JAM24's 53 SIA
datasets, by `datasets` in two calls.

**`campaign_report`** writes `report/campaign_report.md` (and
`campaign_report.json`, the record it is made from) in the campaign folder:
configuration (`_card_summary`), data after the cuts (`_data_warnings`, the
validation's own count), results (`campaign_summary`: chi2 per replica and of
the mean prediction, per dataset with the replica range), the submissions in
`reproduce.json` with their FitPack commits, warnings (a stale summary, mixed
gateway versions, uncommitted FitPack changes), and Sources: the sha256 of the
card's canonical JSON, of the replicas' `fit_summary.json` listing, of the
central-metrics cache and of `reproduce.json`. Every number is read, none is
written by the model; the reply tells the client to show it as returned.
`write=false` returns it without saving. The write is atomic (temporary file,
then `mv`) and recorded as an audit effect; the tool is marked not read-only,
not destructive, idempotent. At JAM24's size (96 datasets, 365 parameters
set from the catalog) the Markdown stays readable: parameters changed from
the library are counted per object beyond twelve (the JSON keeps the list),
and per-dataset cuts are grouped by the cuts the datasets share.

Both run as background jobs under the 25 s reply limit (`analysis_result`).

**Clients that drop the MCP instructions** (HEP-Genesis-Agent, for one) still
get each rule where it applies: `campaign_options` says never to preselect;
`connect_hpc` that the sign-in is typed on the panel; `jam_guide` to read a
procedure first; a confirmation reply carries `CONFIRM_RULE`; the campaign and
Slurm drafts say to show the script exactly as returned; submit, append and
rerun replies name `campaign_status(wait_for='fitting')` as the next step; a
running reply names `analysis_result`. The confirmation and catalog rules are
enforced by the gateway in any case.

## Verified

| Date | Where | What | Result |
|---|---|---|---|
| 2026-10-08 | ifarm, through a local test instance (commit `0ad83c4`) | library table (39 rows), a card that differs from `input02.py` only in the changed row, stop and delete (delete refused while tasks are queued), postprocess, fit summary, a PDF plot, the analysis by experiment, an append plan; the panel in headless Chromium; Codex 0.161.0 and Claude Code 2.x connected (the guide's `${LQCDMCP_TOKEN}` header expanded) | all passed |
| 2026-10-09 | the platform, `jam-gateway-chunyu` (launched with `deploy_and_verify.py`, then updated in place with `hot_redeploy.py`; the cluster sign-in survived) | launch and `--check` through the proxy (token accepted, no token refused, 28 tools, `node_diag` hidden); a campaign with idis 10005 and 10006 really fitted (`mode: fit`, 35 fitted parameters, an `.msr` per replica); replica states, the data check, `campaign_logs`, the panel's Logs and warnings; a campaign from before the status-report guard shows as not fitted with its `KeyError: 'col'` traceback | all passed |
| 2026-10-14 | the platform, the owner's workspace (commits `ac1c95f`, `c7de6eb`) | `campaign_datasets` lists the normalizations of 10010 and 10016 from `input02.py` and none for 10005; a card with `g1 a` equal to `uv1 a` and normalization 10010 equal to 10016 wrote `'fixed': 'uv1 a'` and `'fixed': 10016` into `input.py`, the replica really fitted, 35 free parameters (35 − 1 tied + 1 free normalization); a self-reference refused | all passed |
| 2026-10-28 | ifarm, through the owner's chain from a local gateway (branch `evidence/report-datasets`) | `inspect_datasets` on idis, dy, sidis, ln and jet with the library cuts (idis 10010: 661 points, `Q2>1.69` keeps 564, `W2>10` 218), custom cuts, a campaign's cuts, seven refusals (SIA 1012 among them); `_DATA_CHECK` after the split gives output identical to the previous snippet on 13 datasets of seven reactions, both DIS versions, a missing file and an unevaluable cut; `campaign_report` on a campaign with seven recorded submissions (written, read back with the same sha256) and on one from before `reproduce.json` whose replicas all failed (the report shows why: both datasets keep no point with `W2>10`); both tools over MCP HTTP with a 1 s reply wait (running, then `analysis_result`) | all passed |
| 2026-10-14 | offline only (commits `a26aac1`, `8d2ec2b`: pion PDF objects, per-dataset cuts, credible intervals) | the gateway's remote snippets and `jamai` run locally against a stand-in workspace and a stand-in pion reference: the proton path's generated `input.py` is unchanged except `conf['dis version'] = 1` (FitPack's default) and the override block's form; the older single `object` card gives the same confirmation phrase as `objects[]`; the pion path drops the second shape, fits `p->pi,n`, freezes the reference's proton PDF, writes per-dataset `ln` filters and `dis version` 0; the panel (jsdom) | all passed |

| 2026-10-30 | the platform, `jam-gateway-chunyu` (job 7345983, hot-redeployed to `3d92bb1c0a09` from `28a9e52`) | `campaign_card_from_catalog`: brief by default (3.7 KB reply, `card` = source/job/slurm, six-stage calibration note), `detail="full"` still 50 KB; `validate_campaign` on the brief card returned valid in 7.6 s with the staged-calibration warning shown as a warning; `fs_read` on `JAM24_final_full_20261029_202647/reproduce.json` (9106 B): 4000-byte window with `total_bytes`/`truncated`/`next_offset`, three pages rebuilt the file byte for byte, `max_bytes=20000` returned it whole, a directory, a missing file and `/etc/passwd` refused; `fs_list` on the area: 19 entries, 8 shown, `truncated: true` | all passed |

The offline suites are not part of this repository.

**Not yet verified live:** the pion objects on the platform (whether the
reference file is readable from each account and what the FitPack `ai`
branch does with dy-pion, ln, `pdf-pion` and `p->pi,n` under `dis version`
0); a workspace clone with a GitHub token; the VS Code and
Claude Desktop setups in the guide; Claude Code tool calls longer than 60 s
with the guide's timeout settings (taken from the Claude Code
documentation). In October 2026 the dashboard was found to answer yes for
any token (see Access).

## Published scientific analysis catalog (read-only MCP)

The repository's `analyses/<analysis>/catalog/` entries are available through
`list_analysis_catalog`, `get_analysis_catalog` and `get_analysis_requirements`.
These tools use the existing gateway authentication. They do not need an HPC
connection, read original scientific files, create campaigns, generate inputs,
or submit jobs. Catalog validation and reproduction readiness are distinct;
JAM24 remains preliminary and not reproduction-ready.

The LLM orchestrates existing scientific and farm services; the gateway retains
the numerical configuration. Discovery and overview are compact by default.
Parameter requests default to object/group counts, not hundreds of definitions;
dataset requests default to reaction IDs/counts, not experimental arrays.
`detail:"full"` explicitly requests full records. Exact document sections are
explicit lossless exports, not a required step in normal orchestration.
Catalog text responses use compact JSON while retaining MCP structured content
by default. `get_analysis_catalog` alone accepts `response_format:"json_text"`
to return the full page **once**, as JSON in `content[0].text`, without
`structuredContent`. Parse that JSON to read the same scientific records.
Omit the option (or use `"legacy"`) for existing clients. Discovery,
requirements and other tools keep their existing response formatting.
Check MCP `isError` before parsing: authentication and validation errors keep
their existing error responses, rather than becoming catalog pages.

Examples of `tools/call` arguments:

```json
{"name":"list_analysis_catalog","arguments":{}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"overview"}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"parametrization","object_key":"pdf"}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"parameters","object_key":"ffpion","group":"u1","status":"free","detail":"full"}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"datasets"}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"qcd"}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"statistics"}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"documents"}}
{"name":"get_analysis_catalog","arguments":{"analysis_id":"JAM24","section":"parameters","detail":"full","response_format":"json_text","limit":50}}
{"name":"get_analysis_requirements","arguments":{"analysis_id":"JAM24"}}
```

Parameter `group` is an exact shape prefix (`g1`, `u1`, etc.); `status` is `free`,
`fixed` or `linked`. Target one dataset with reaction `object_key` and quoted
`dataset_id`. Target a theory/statistical setting with `setting_id`, e.g.
`Q20`, `positivity`, `theory.order`, or `Gaussian_initialization`. Target an
unresolved requirement with `requirement_id`, e.g. `U03`. Requirements default
to current blockers only; `detail:"full"` also exposes historical/conditional notes.

Discovery and retrieval return `catalog_ref`, a stable canonical ID pinned to
the SHA256 revision of all three approved documents. Pass this string directly
as `analysis_id` on later catalog calls. Changed source documents make the
reference stale and produce an error; aliases alone select the current revision.
`catalog_ref` plus a record's JSON pointer identifies its exact source without
copying its values into subsequent requests. No client-supplied paths are followed.

Future campaign-from-catalog operations should resolve this reference server-side,
validate current requirements, and pass the saved configuration to the existing
campaign/input-generation/farm services. They should return campaign/job IDs and
reuse existing cached-analysis tools for compact result summaries, avoiding an
LLM download/retransmission loop. Those operations are not implemented here.
The existing scientific prediction cache is campaign/replica-specific and remains
unchanged. Catalog paging instead uses a bounded, document-hash-keyed in-memory
cache; each request still validates and hashes the approved documents. It writes
no campaign cache, numerical dataset, or artifact.

Other sections are `provenance`, `analysis_config`, `paper_findings` and
`unresolved_settings`. `section:"documents"` without `object_key` returns a
compact index of the three registered documents with their named sections,
byte/character counts and SHA256 hashes. Fetch a document through its named
section, or keep using `section:"documents", object_key:"analysis_config"`
(and the equivalent Markdown document names). No document text is in the index.
Dataset `object_key` filters by reaction. Each page
reports its catalog hash, section/filter, `items`, `complete` and `next_cursor`.
Use the returned revision-pinned `catalog_ref` as `analysis_id`. Keep that
reference, section, selectors, `detail` and `response_format` unchanged and pass
each `next_cursor` as `cursor` until `complete` is true. Do not increment offsets
or decode/edit cursors. **The maximum `limit` is 50; the default is 10.** This is
an item ceiling, not a promise of that many items: the entire MCP result is
bounded to 10 KiB, counting both payload copies in legacy mode and the single
copy in `json_text` mode. Changed documents invalidate references/cursors;
changing format or selectors invalidates cursors too. Restart retrieval rather
than combining pages from different revisions or formats. The expanded-view
cache is bound to the same revision, selection and representation.

Structured records retain original values, evidence labels and source references,
with a JSON pointer back into `analysis_config`. An oversized record is returned
as numbered JSON-text fragments: concatenate `text` by `fragment_offset`, check
`fragment_sha256`, then decode the full JSON record. No tail is dropped.

#### Opt-in complete configuration and targeted evidence

For complete scientific reconstruction, use the existing tool with
`section:"configuration", detail:"full", response_format:"json_text", limit:50`.
An ID or existing `catalog_ref` selects the first page. It supplies a manifest
and a `configuration_ref` pinned to both the source revision and projection
schema. Reuse that `configuration_ref` as `analysis_id`, with each `next_cursor`
as `cursor`, until `complete:true`. Keep section, detail and response format
unchanged. **Do not restart retrieval for each model or reaction.**
`detail:"summary"` returns only the manifest; full retrieval includes it without
an extra overview call. Existing sections and legacy/default envelopes remain
available and unchanged. These views are read-only, not campaign generators.
Manifest continuation arguments match the current detail, response format and
limit, including legacy-format requests. Summary manifests also advertise a
separate `full_configuration_request`; start that request without a summary
cursor to retrieve the full configuration.

Data pages pack multiple compact named parts together:

```json
{"configuration_ref":"configuration:Example.final@<sha256>","section":"configuration","response_format":"json_text","offset":0,"items":[{"part":"parameters/model","records":[{"key":"u N","value":{"definition":{"value":0.2,"min":0,"max":1,"fixed":false},"status":"free"}}]},{"part":"reactions/reaction","records":[{"kind":"configuration","value":{"filters":{"global":["Q2 > 0"]}}},{"key":1001,"value":{"xlsx_path":"reaction/expdata/1001.xlsx","normalization":null}}]}],"next_cursor":"<opaque cursor>","complete":false}
```

Concatenate records **in order within each part**. Initialize empty parts from
manifest `parts` entries with zero records. Model/reaction names use JSON Pointer
segment escaping (`~` to `~0`, `/` to `~1`); keys retain their literal JSON types.
Reaction headers use `kind:"configuration"` rather than a dataset key, so even
a dataset named `configuration` cannot collide. Original flags, publication
settings, implementation findings, reproduction overrides, initialization and
current unresolved requirements are separate named parts, not flattened into
one potentially misleading effective configuration. Literal linked declarations
remain literal: no target substitution or sum-rule calculation occurs.
If a future source row lacks its identity field, its compact wrapper has
`source_index` instead of `key`; this preserves absence rather than inventing
an identity or replacing a missing identity with null.

Only identical, explicitly present interpretation fields are factored into
`common_semantics`; a missing field is never made equivalent to explicit null.
The common parameter interpretation applies to every parameter row; the common
normalization interpretation applies to every non-null normalization object.
The schema adapter removes repeated record provenance from the compact stream
and routes historical/audit branches to on-demand evidence. Unknown top-level
or source-verification branches are included losslessly in `unclassified`, with
`configuration_records_complete:false` and their source pointers. This flag does
not assert historical reproducibility; original `reproduction_ready`, blockers
and campaign restrictions remain authoritative.

Request original records, including removed provenance/explanations, using
`section:"configuration_evidence"`, `analysis_id:<configuration_ref>`, and
`record_ref:"evidence:<configuration_sha256>:<registered source JSON pointer>"`.
The manifest explains row pointers and lists source/source-verification branches.
For example, `/source_verification/parameter_definitions/model/0` selects the
first original parameter row; `/reproduction_configuration` selects the exact
original reproduction policy. Only registered branches/rows are accepted; pointers
are dictionary lookups, never filesystem paths. The reply includes the original
record, canonical source-record SHA256, pointer and both revision references.
Existing exact document exports remain the route to original UTF-8 source text.

The 10 KiB MCP response budget includes the selected envelope and escaping, with
384 bytes reserved for JSON-RPC overhead. The item limit remains **1–50**, default
10. In these streams it counts wire records/fragments across all named parts,
not the number of parts. Offsets also count wire records. A small manifest is a
standalone first page with offset zero and a continuation at offset zero; it
does not consume a data-record offset. Oversized manifests and records are
losslessly serialized into `part:"fragment"` records with `record_fragment`
fields `offset`, `total_characters`, `sha256`, `text`. Join contiguous fragments
by offset, verify the SHA256 of the UTF-8 concatenation, then decode the JSON
`{"part":...,"record":...}` wrapper and insert its record into that part.
`part:"manifest"` reassembles the manifest itself. Never parse an incomplete
fragment or infer that one wire record equals one scientific record.

##### Canonical hashing and revision contract

`femtomind.configuration_snapshot.v1` uses `python-json-canonical-v1`:

- Parse with the existing catalog JSON reader: integer-form tokens remain
  arbitrary-precision integers; decimal/exponent tokens use Python binary64 floats. Nonfinite values
  are rejected. There is no scientific rounding or float quantization.
- Serialize using Python `json.dumps(sort_keys=True, ensure_ascii=True,
  separators=(",",":"), allow_nan=False)`. Keys sort by Unicode code point;
  arrays retain their order. No Unicode normalization occurs. Non-ASCII characters
  use lowercase JSON escapes; non-BMP characters use surrogate-pair escapes.
- Hash those UTF-8 bytes without BOM or trailing newline using SHA256. Numeric
  JSON types remain distinct: `1`, `1.0`, `false`, and `null` have distinct bytes;
  `-0.0` remains distinct from `0.0`. Float spelling follows Python's shortest
  round-trip formatter, including its exponent formatting. This is **not RFC
  8785/JCS**; other-language clients must reproduce this contract or verify via
  a compatible Python implementation. Source-document hashes still refer to
  original bytes, including their original number spelling and Unicode encoding.

Each part hashes its complete ordered records. The configuration hash covers
`{"schema":<schema>,"canonical_encoding":<encoding>,"source_catalog_ref":<ref>,"common":<common_semantics>,
"groups":<all named parts and records>}`. Compare each part's count/hash, then
the complete configuration hash. A change in any source document invalidates the
configuration reference even if numerical values are unchanged. Projection or
canonicalization changes require a schema version change after release.

Cursors bind the configuration reference, section, evidence selector, detail,
response format and packing version. Replay with identical arguments is
idempotent; stale/cross-view/cross-format cursors are rejected. Cursors are opaque
continuation positions, not authorization credentials. Evidence references also
embed the exact configuration hash. A bounded four-entry in-memory projection
cache is keyed by fixed catalog root, freshly validated source revision, schema
and canonicalization version. It is format-independent; pages and size checks are
constructed for the requested format. Every request still reads, validates and
hashes source documents before cache lookup. No numerical/prediction caches or
scientific source files are modified.

#### Read-only configuration compiler and parity preview

`preview_analysis_configuration` accepts the `configuration_ref` returned by the
configuration manifest. It compiles a complete **server-side intermediate
representation (IR)** directly from that validated snapshot. It does not use
campaign-card defaults, object/dataset registries, a FitPack template, readers,
scientific archives, or the input materializer. No input, directory, campaign,
job, fitted value or scientific prediction is created.

```json
{"name":"preview_analysis_configuration","arguments":{"configuration_ref":"configuration:Example.final@<sha256>","options":{"reproduction_policy":"catalog_author_confirmed","label":"parity review","normalization_probes":[{"reaction":"sia","dataset_id":"1001","value":2.7}]}}}
```

Options are strict and optional:

- `reproduction_policy`: currently only `catalog_author_confirmed` (the default).
  No automatic application defaults or arbitrary scientific overrides are accepted.
- `label`: at most 80 characters, for diagnostics only; not a campaign name/path.
- `normalization_probes`: at most eight `{reaction,dataset_id,value}` assertions
  that the authoritative bounds permit a finite value. They do **not** replace
  declared starting values or assert a fitted result. An absent declaration or
  out-of-bounds probe is a hard blocker, not a warning.

The compact report appears once as JSON in `content[0].text`. It returns counts,
exact-parity checks, authorized author overlays, normalization-bound groups,
independent penalty evidence, unresolved historical IDs and a `compiled_ref`.
Scientific errors, missing requirements and unsupported settings are paginated
in `items`; follow `next_cursor` with identical configuration/options. Limit is
1–50 (default 10), subject to the existing 10 KiB envelope budget. No hundreds of
parameter records are returned through MCP. Any mismatching scientific field is
an error with `blocks_materialization:true`; unsupported fields are preserved
in the IR rather than discarded or guessed.
For unusually large future metadata, oversized summary fields return their
canonical hash/size and `retained_server_side:true`; issue records still paginate
losslessly and the full scientific IR remains intact. Catalog evidence tools can
retrieve original supporting records independently.

The IR schema is `femtomind.scientific_configuration_ir.v1`:

- `literal_catalog`: the complete original catalog JSON, including provenance,
  original declarations, publication evidence and historical restrictions.
- `reproduction.parameter_definitions`: all original parameter rows and literal
  fixed/link declarations. The only generated parameter metadata is the explicit
  author-approved lowercase `gaussian`, coefficient-specific `mean`/`std` on
  `fixed is False` entries. Starting values/bounds/links are never substituted.
- `reproduction.datasets`: complete original reaction containers/ordered records,
  identifiers, XLSX paths, normalization declarations, literal filter strings,
  field-presence markers, zero-accepted-point datasets and catalog reader evidence.
- `reproduction.native_settings`: original flags plus recorded author overlays
  such as Q20, positivity and `flat par=True`. The original `flat par=False`
  remains untouched in `literal_catalog`. No Gaussian objective term is added.
- `reproduction.normalization_evidence`: the original normalization audit and
  workbook hashes/uncertainties/covariance and penalty-convention descriptions.
  Bounds are independent of experimental deltaN. Finite deltaN around 1000 is
  kept exactly, including floating-point distinctions; it is never converted to
  zero penalty. Bounds are not tightened to an ordinary campaign-card maximum.
- `reproduction.policies` and `overlays`: original author policies and explicit
  before/after setting records; operational options remain separate.

The compiler recognizes transport for original boolean/numeric QCD flags,
evolution/order/grid settings and parametrization/positivity declarations. Unknown
native/reproduction/initialization settings and unclassified catalog branches
block consumption pending review. Links must terminate within their own object
or reaction; no effective linked value is calculated. Missing Gaussian means/stds
are errors. No random values are drawn and no normalization prior is evaluated.

The compiled identifier hashes the full IR with the established canonical JSON
contract, including compiler schema, encoding, exact source/configuration
revision, provenance and options. A bounded four-entry process-local cache keeps
the full IR. `configuration_compiler.resolve_compiled(store, compiled_ref)` is
an **internal proposed adapter boundary**, returning a defensive copy only after
fresh revision checks, checksum verification and scientific parity validation.
Evicted references require another preview. This is not a public materialization
operation, and the existing input materializer is intentionally unchanged.

`parity.passed:true` means catalog-to-IR integrity only. Reports always state
`reader_selection_verified:false` and `materialization_allowed:false` in this
stage. Catalog point-count equality is not proof of selected-point identity.
Historical reproduction readiness and unresolved requirements are reported
separately, without solving them or granting campaign-generation permission.

#### Isolated compiled-input validation (not a campaign)

`jamai.compiled_input.render_compiled_input(store, compiled_ref)` consumes
`configuration_compiler.resolve_compiled` and returns a FitPack-compatible input
as text. It uses no campaign card, example input, ordinary materializer, dataset
default or FF registry. Literal original settings/parameters/datasets are emitted
first, followed by an explicit author-confirmed reproduction overlay section.
All original declarations and normalization evidence remain in the full IR.
Canonical decimal catalog dataset/filter IDs become native integer keys; other
string IDs remain strings and ambiguous leading-zero IDs are rejected.

`check_generated_input(ir, text)` independently parses literal assignments without
executing the source. It compares both the original and reproduction phases,
including types, map-key types, missing fields, nulls, exact numbers and order.
Unsupported dataset fields, conflicting filter evidence, altered bounds or other
scientific differences are hard errors. Workbook normalization uncertainties are
not invented as input priors: the original FitPack workbook/preprocessing and
finite penalty conventions remain authoritative.

`write_isolated(store, compiled_ref, scratch_root)` is an internal development
API, **not an MCP tool**. It accepts only an owned, non-symlink top-level
`/w/ceph24/JAM/<account>` directory. It creates a unique private
`femtomind-materialization-*` scratch directory containing `input.py`, the complete
`configuration-ir.json` and a checksum/parity report. Existing files are never
overwritten. It cannot target an application checkout or FitPack directory.

The external `femtomind-tests` suite renders/parses inputs in memory locally.
Its reader verification runs from stdin in an existing scientific container,
with Ceph mounted read-only except for the one scratch directory. It compares
the actual original input's data-only declarations with generated declarations,
then runs existing native experimental readers against both and compares retained
row identities and experimental arrays. Only native percentage-to-absolute and
normalization-uncertainty preparation are used; no parameter manager, random
sampling, theory evaluation, residual objective, campaign or optimizer is run.
Swallowed native filter exceptions and catalog/native rounding distinctions are
reported explicitly. Current-reader parity does not resolve historical blockers
or establish historical ensemble reproduction. Campaign execution remains
disabled, and normal campaign-card behavior is unchanged.

#### A campaign card from the catalog

`campaign_card_from_catalog(analysis_id="JAM24.final", replicas=10, overrides, detail)`
returns a validate-ready card built from the compiled configuration, never by
copying values: every parameter with its bounds and ties, both FF shapes (215
free parameters for JAM24, where the composer's defaults give 140), the
datasets with their normalizations and cuts, the QCD settings and the
author-confirmed positivity flags, and Slurm resources sized for the fit.
`detail="brief"` (the tool's default) returns only `source`, `job` and `slurm`,
which is all `validate_campaign` needs; `detail="full"` adds every table, about
50 KB for JAM24.final (2.5 × 10⁴ tokens), which is more than a client should take
in one reply and nothing gains by sending back. Both forms carry `notes` and
`calibration`.
Datasets the current readers keep no point of (JAM24: SIA 1012 and 2006) are
left out, because FitPack stops on an empty dataset, and the card records that.
`overrides` updates the sections `job`, `slurm`, `qcd`, `parameters`,
`datasets` or `reactions`; the changed keys are recorded on `card.source`.
A brief card carries their values too (`card.overrides`, beyond the job and
slurm it has), because `card.source.overrides` names only the keys and
`validate_campaign` rebuilds the rest from the catalog; a brief card that
declares changes without their values (one made before 2026-10-30) is refused
(`CARD_INVALID`, `missing_overrides`) instead of being rebuilt without them,
which had turned a requested Q20 of 1.69 back into the catalog's 1.6129 and
dropped a published-replica start. The calibration record says whether its
warning applies: a card that starts from prior replicas (`parameters.priorSource`)
is not the one-stage fit from the cataloged starts, so validation does not
warn about the staged calibration for it (`calibration.applies` false).

`calibration` is what the catalog records about how the published fit was made.
For JAM24 that is six stages (DIS, then DY, then W/Z and jets, then the strange
sector, then an SIA-only FF calibration, then the final fit from the calibrated
starts), with the catalog's own limitation: a dependency description, not a
runnable schedule. A catalog card runs one stage from the cataloged starting
values, so the note appears in the card's `notes` and in `validate_campaign`'s
warnings. On 2026-10-30 a single-stage JAM24.final fit from those starts stopped
at χ²/N 1.8 to 3300 (every replica by `ftol` at 10⁻³, with a gradient still
about 10⁹), while a fit started from the published replicas reproduced χ²/N
0.998: users are told before they spend a night of replicas.

`validate_campaign` and `submit_campaign` accept such a card in full, or
compactly as `{source, job, slurm, overrides}`, which the gateway rebuilds from
the catalog. Two rules then fail closed:

- A campaign whose name claims a catalog analysis (`JAM24...`) but whose card
  was not built from the catalog is refused (`CATALOG_REQUIRED`).
- A catalog-built card may differ from the catalog only where an override
  declares the change (or by the recorded zero-point datasets). Any other
  difference returns `valid: false` with `CATALOG_MISMATCH` and the list of
  `undeclared_differences`, and gives no confirmation phrase.

`catalog_comparison` in the validation result lists datasets, free parameters,
normalizations, QCD settings, cuts and the initialization against the published
fit.

`JAM24.final` runs as published: the COMPASS SIDIS workbooks the paper read
(1005, 1006, 2005, 2006, 3000, 3001) are selectable, and the gateway reads them
with the published filters (`W2SIDIS>0`, `0.2<Z<0.8`); the updated workbooks
(1105-3111, `JAM24.current`) keep `0.2<=Z<=0.8`; the two cannot be mixed. With
the cuts the published card keeps exactly the paper's 5853 points. A catalog
card starts as the authors did: `initialization: random_within_bounds`
(FitPack's `flat par`) with each free parameter's Gaussian mean and std; linked
and fixed parameters are not drawn. Card parameter rows accept `gaussian`,
`mean` and `std` for the PDF too. The name reads as uniform starts, and an
assistant reading it told a user so (2026-10-30); so the card summary
(`campaign_status`, the report) and `campaign_provenance` give
`initialization_meaning` beside it: with `random_within_bounds`, a parameter
with a Gaussian start is drawn from N(mean, std) within its bounds, any other
uniformly between them, and how many of the free parameters have one. A
catalog card that starts from prior replicas at another Q20 than they were
fitted at is flagged by `campaign_card_from_catalog` (`conflict`), before
`validate_campaign` refuses it (`PRIOR_SOURCE_INVALID`: starting values are
not converted between scales). A campaign made from a catalog card that
started from the cataloged values carries the staged-calibration warning
when its results are read too: `campaign_status` (`calibration`), the
far-from-data note of `campaign_summary` and `postprocess_campaign`, and the
report's Configuration.

Cancelling or deleting a campaign forgets its replay: the same confirmed
request may then queue it again.

#### Read-only compiled-input MCP preview

`preview_compiled_input` accepts a revision-pinned `configuration_ref` from
`get_analysis_catalog(section="configuration")`. The server resolves the entire
scientific configuration, calls the validated compiler and existing in-memory
materializer, then checks the rendered text with the existing independent parity
checker. Clients never retransmit parameters, datasets or normalization records.
Only the manifest's `configuration_ref` is needed; downloading its configuration
continuation stream is unnecessary for this preview.

```json
{"name":"preview_compiled_input","arguments":{"configuration_ref":"configuration:Example.final@<sha256>","section":"summary"}}
```

The default summary returns analysis/configuration/compiled identities, the full
UTF-8 input SHA256 and `input_ref`, scientific counts, reaction-qualified dataset
identities, normalization bound groups, Gaussian policy, QCD/positivity settings,
original-versus-reproduction overlays and blockers. It does **not** return source
text. The input reference is an identity, not a filename or a materialization
capability. With identical pinned source and options its hash is deterministic.
Diagnostic options are the same strict compiler options (`reproduction_policy`,
`label`, `normalization_probes`). These do not override scientific declarations.
They bind the compiled reference, which is embedded in the input header, so even
a metadata-only option change changes the complete input hash while preserving
the scientific definitions. Normalization probes assert admissibility; they do
not replace literal starts or calculate effective linked values.

Targeted sections avoid large default responses:

| `section` | Records |
| --- | --- |
| `summary` | Compact report; errors in `items` when blocked |
| `datasets` | Every configured ID/path, literal cuts/presence and prior point count, including zero-point records |
| `normalizations` | Every original value/bound/fixed-or-link declaration; null reproduction override; prior finite deltaN evidence and unchanged penalty policy |
| `settings` | Original and reproduction native settings, Gaussian policy, named constants, full reaction filter maps, options and prior reader metadata |
| `overlays` | Explicit author-confirmed before/after settings and complete Gaussian policy |
| `issues` | Scientific mismatches, missing requirements and unsupported settings |

Every successful MCP result contains one JSON page in `content[0].text`, without
duplicated `structuredContent`. `limit` is 1–50 (default 10), subject to the same
10 KiB serialized-response budget. Follow `next_cursor` with identical reference,
options and section until `complete:true`. Large records use the existing
lossless JSON-fragment protocol. Large future summary fields become hash/size
references naming a valid targeted section; the complete records stay available.
Cursors and page caches bind the exact configuration/compiled/input identities,
preview/compiler/materializer schema versions and complete projected-record hash.

Valid requests with scientific errors return `status:"blocked"`, `parity.passed:
false`, explicit hard issues and no usable input identity. Blocked detail views
return issue records (`record_section:"issues"`), never repaired declarations.
Malformed or stale requests are MCP errors. Existing catalog tools and response
formats are unchanged.

`reader_validation.executed_this_request` and `verified_this_request` are always
false: no reader or workbook is accessed. Expected point counts and the separate
`prior_reader_evidence` are copied from pinned catalog evidence and are **not** a
new verification of selections or the historical ensemble. Current checks cover
compiler-to-IR and IR-to-rendered-input structural integrity only.

The preview writes no input files, manifests, artifacts or campaign directories,
uses no SSH/HPC connection, and runs no readers, random sampling, theory,
optimization or jobs. Only bounded existing in-memory catalog/compiler caches are
used. Normal gateway HTTP logging is unchanged. `materialization_allowed` and
`campaign_execution_allowed` remain false; historical reproduction blockers remain
separate and unchanged. Both existing deployment packagers include the new Python
module automatically, without adding scientific archives to deployment snapshots.

For a complete lossless export, request `section:"analysis_config"` and concatenate
document `text` chunks by `char_offset` across pages, after reassembling any record
fragments. UTF-8 encoding of the joined text must match `document_sha256` and
`document_bytes`. The two Markdown document sections use the same procedure.
Historical paper-only unknowns are preserved in these documents; use
`get_analysis_requirements` for current blockers and resolved-policy distinctions.
Priorities are reported from the catalog; unprioritized historical notes remain
unassigned. The confirmed reproduction configuration supersedes older audit notes.

To add an analysis, add a regular directory under the approved repository catalog
with `analysis_config.json`, `paper_findings.md` and `unresolved_settings.md`.
Use the existing `femtomind.paper_analysis_catalog` schema (supported version 0.x),
a unique `analysis_id`, publication reference, scope and validation/readiness
metadata. Follow the existing layered scientific/source-verification structure
for selective sections; complete document retrieval works for every valid entry.
Every active blocker ID must have a current, unresolved resolution-register record.
No per-analysis tool changes are needed. Invalid metadata, duplicate IDs and
symlink/junction escapes are rejected. MCP arguments cannot select filesystem paths.

Launch payloads and hot-redeploy staging include the same validated catalog files.
The catalog directory participates in the existing swap/backup/rollback transaction.
This implementation does not itself deploy or modify a running gateway.

Local transport and package checks (stdlib; no production credentials):

```text
FEMTOMIND_APP_ROOT=/path/to/femtomind-chatgpt-app python -B -m unittest discover -s ../femtomind-tests/tests -p test_analysis_catalog_mcp.py -v
```

### Updated JAM24 settings in the ordinary campaign workflow

New campaigns use the existing `campaign_options`, `campaign_datasets`,
`validate_campaign(card)`, materializer and `submit_campaign(card, confirm)`
workflow. There is no analysis-specific runner, scheduler or submission path.

`JAM24` now resolves to **`JAM24.current`**, the recommended updated configuration.
Explicit **`JAM24.final`** and its existing pinned references continue to describe
the untouched historical published reference and archived results. The optional
`analyses/JAM24/catalog/campaign_defaults.json` describes the current overlay;
the original three catalog documents and their historical hashes are unchanged.
Discovery marks the recommended entry, and configuration/cursor revisions bind
the complete derived configuration and overlay. Both deployment packagers include
the overlay and its projection module through their existing catalog/package
collection mechanisms. Numerical archives are not packaged.

The updated defaults replace SIDIS 1005/1006/2005/2006/3000/3001 with
1105/1106/2105/2106/3100/3101 and their matching workbook paths. Existing native
readers apply the configured DIS Q2 and W2 thresholds plus `Z>=0.2 and Z<=0.8` once.
The SIDIS reader derives W2 but does not inherit IDIS filters automatically, so
the application supplies that reader filter from the existing DIS control.
`W2cut_SIDIS` has no effect and no W2SIDIS filter is introduced. Jet uses only
`pT>8`; its obsolete `x<1` filter is absent from the effective generated settings.

Bounded non-fitting validation with current workbooks retained **1,528 SIDIS
points and 5,891 total points**, across 98 configured and 96 nonempty datasets.
All other reaction row identities match the unchanged selection. The six new
workbooks each contain 17 Q2/W2-accepted points exactly at Z=0.8. The inclusive
upper bound retains all 102 boundary points, as required by the updated analysis. Historical 5,853-point parity is
not a validation target for this updated configuration. As a separate control,
the historical six SIDIS workbooks retain the same 1,490 points and identical
row identities with and without the z filters.

SIA normalization overrides accept finite ordered bounds above 2, including 100;
defaults remain unchanged (1.5 or 2 as cataloged). Starting values and links are
validated against the supplied bounds, so `sia:1001` can take N=2.7 with max=100.
No uncertainty override is accepted here: experimental normalization uncertainties
and their finite penalties remain controlled by existing workbooks/FitPack. The
eight effectively unpenalized entries retain finite deltaN near 1000. Explicit
SIA dataset filter maps can preserve the selected analysis's original cuts
instead of silently inheriting a different application filter.

The MCP QCD card exposes `betaVersion` and `siaVersion`, integer 0 or 1, default 1.
These become `conf['beta version']` and `conf['sia version']` in every new input.
`sea_type`, `strange off`, `s_veto` and `grid-8 path` are ignored/removed from
effective newly generated configuration. FF Gaussian metadata and the existing
FitPack initialization mode are preserved; PDF Gaussian metadata remains optional
and is not required by the ordinary card path. The ordinary workflow explicitly
sets `flat par` from the initialization mode without changing the sampler.

The LLM can retrieve `JAM24.current` and populate the ordinary card with the
explicit model definitions, normalizations and cuts, then call `validate_campaign`.
There is currently no reference-only import argument on that existing campaign
tool, so the resolved records must still be supplied in the card. No jobs were
submitted during this implementation; known historical reconstruction limitations
remain separate from readiness to configure this updated campaign.


### Readable future campaign input cards

Native FitPack loads `input.py` one line at a time. New cards retain the legacy
template and a small, line-compatible loader; readable generated definitions
and overrides live in `stage_configuration/<sha256>.py` beside the card.
The loader verifies those bytes before executing them. Literal initialization
and bootstrap seed declarations remain in the card for existing provenance
readers. This changes source formatting/storage, not the effective `conf`.

For prior-initialized campaigns, the complete, original prior snapshot lives
once in `prior_replicas/<snapshot-sha256>.json` beside the stage input, shared
by every replica. Both its exact file bytes and canonical snapshot hash are
verified. Publication is atomic and refuses to overwrite different existing
bytes. The existing manifests/lock retain their full audit snapshots, and the
pre-submission barrier checks the new settings/reference as well as those
manifests. Native initialization still receives the same complete dictionary;
historical inline cards remain supported. Keep the card and its referenced
files together on shared Ceph. Relocation requires rematerializing references;
copying only the card is not a portable standalone campaign export.

FF publication figures search all selected panels for text space, then try
smaller readable fonts. If annotations or legends still cannot fit without
covering bands, a separate footer is reserved below the x labels. Long campaign
labels wrap and expand that footer. Dense flavor panels retain a small corner
label rather than causing the entire figure to fail. Numerical FF values,
interval calculations, hadron colors, LaTeX, three columns and logarithmic
display limits remain unchanged.

### Central-data scientific chi-square reporting

Scientific replica scores and chi-square selections use only point residuals
against experimental central values. The analysis cache evaluates each fitted
vector with native FitPack and `bootstrap=False`, so correlated shifts are
profiled against central data rather than reused from bootstrap optimization.
For the nuisance formulation, `prediction = theory / normalization + shift` and
the point residual is `(central_data - prediction) / alpha`. Data, squared
correlated-nuisance residuals and squared normalization residuals are separate;
only the point term is reported chi-square. Their sum is an explicitly named
complete objective diagnostic, never reported chi-square. Native `_get_nres` supplies the
normalization penalty, preserving linked/fixed exclusions and covariance-mode
normalization conventions without double counting. Native covariance-mode
whitened residuals remain authoritative for the complete objective diagnostic.
Reported chi-square always uses the stated point-residual/alpha formula;
optimization itself is unchanged.

The final reported ensemble chi-square first averages the predictions point by point across
the selected replicas. It does not average individual chi-square values. Its
separately reported penalty diagnostics are the squares of the mean signed nuisance/normalization
residuals, not the means of their squares. For the fixed additive correlated
errors used in JAM24 (`beta version=1`, nuisance mode), averaging central-profiled
predictions and nuisances agrees with native reprofiling of the mean normalized
theory. With theory-dependent beta, this equivalence is not asserted: the output
retains the requested mean predictions and labels the mean-nuisance convention;
it does not silently perform another profiling operation that changes them.

The **By experiment** dataset table, overview/KPI totals, CSV and LaTeX exports
use that same ensemble calculation and selected, pinned replica cohort.
`chi2`/`chi2_red` are the central point-residual chi-square of the pointwise mean
complete predictions, not the mean of individual chi-squares; `z_score` applies
the existing chi-square-CDF-to-normal transformation with dataset Npts degrees
of freedom. `chi2_cor` is the sum of squared mean signed nuisance residuals.
`chi2_norm` evaluates the native normalization penalty at mean fitted N: native
`_get_nres` is `(N-1)/dN` or zero for its fixed/linked/covariance exclusions, so
the square of its mean equals the penalty at mean N. Finite `dN=1000` remains
finite. `norm`/`norm_std` retain the fitted normalization mean and sample standard
deviation across selected replicas (no standard deviation for one replica).
Cached `theory` already contains normalization and the central-profiled shift;
reporting never applies either again. `mean_individual_chi2*`, `objective_mean`,
and the explicitly labeled best-replica experiment diagnostic remain separate
from the ensemble result. Unavailable zero-point datasets remain unavailable;
a complete cached partial cohort is labeled by its actual replica count.

`campaign_summary` and `postprocess_campaign` reuse the prediction cache and the
existing asynchronous analysis jobs for expensive initial evaluation. Poll
`analysis_result` when they return `running`. Cache metric records use a separate
`metrics_version`, full-precision arrays, signed nuisance/normalization residuals
and existing content signatures. Scoring version 2 and an explicit point-only
basis reject older penalty-inclusive score ledgers without changing scientific
prediction arrays. Old rounded prediction caches are rebuilt when
needed, rather than treated as complete scientific metrics. Distribution-plot
cache formats and numerical calculations are unchanged. Chi-square-based PDF,
FF and experimental selections require matching central scores; there is no
bootstrap fallback. Default selections still keep every fitted replica.

#### Incremental prediction cohorts and dashboard progress

Summary, Postprocess and experimental analysis coordinate one central-cache
builder per campaign. An OS advisory lock also serializes builders across
gateway processes. Each invocation evaluates at most eight missing/changed
replicas, using the existing scientific container and calculation. Every
completed replica (all configured datasets together) is journaled atomically
with full-precision arrays, a payload hash, dataset identity and exact source
fingerprints. A restart replays committed rows; interrupted temporary files
are never treated as completed replicas. Failed replicas remain explicit
blockers rather than being dropped from the requested cohort.

The store is `<campaign>/analysis/cache/incremental/`: `rows/` and `static/`
hold recoverable batches, `builds/` pins target identities and signatures,
`cohorts/` holds complete snapshots, and atomic `active.json` selects the last
complete cohort. Legacy `datasets/` files remain untouched; the first adoption
uses hard links where possible, with a copy fallback. No cleanup or destructive
migration is performed. New fitted replicas cannot enter an in-progress cohort;
they belong to a later cohort. Changed input invalidates the scientific context;
changed MSR/summary fingerprints invalidate only the corresponding replica.

While extending a valid cohort, the browser displays its actual replica count
and metrics immediately, with cached/new/remaining/failed progress, and refreshes
after publication. It never labels those metrics as the complete current
ensemble. `campaign_metrics_progress(collection_id)` reads compact durable
progress without starting a scientific calculation. First-ever cohorts retain
the existing asynchronous `analysis_result` workflow. Chi-square selections and
experimental plots use the same pinned cohort and score ledger, even if another
cohort finishes during a request. Ordinary PDF/FF plotting without a chi-square
selection remains independent of central-cache construction.

This changes persistence and orchestration, not scientific evaluation,
normalization, profiling, threading, credible intervals or figure styling.
Repeated W+charm grid loading remains in the existing FitPack implementation;
no numerical-library thread settings or protected FitPack code are changed.

Summary fields `mean_total_chi2`, `median_total_chi2`, `min_total_chi2` and
`max_total_chi2` describe individual central-data point-residual chi-square and
carry `chi2_basis=central_data_point_residuals`. `ensemble_central_chi2.total`,
`chi2_reported` and `chi2_per_point` exclude penalties. Correlated/normalization
penalties and `objective_total` are separately named diagnostics. No chi-square
statistic or selection uses the complete objective. The old
`mean_central_value_chi2_per_point` name is retained as an alias of the ensemble
point chi-square/point; the distinct individual mean is explicitly named
`mean_individual_central_chi2_per_point`. Saved optimization scores remain under
`bootstrap_diagnostic`, labeled data-only where historical summaries omitted
explicit penalties. Original per-replica summaries and MSR files are not rewritten.
Experimental-table legacy `chi2`/`chi2_red` remain the central point term;
`chi2_total`/`chi2_total_red` are also point-only aliases. `objective_mean` and
`objective_best` are separate penalty-inclusive diagnostics. Its `ensemble` field is
separate, and experiment grouping remains a clearly labeled best-replica diagnostic.

There is no automatic scientific-goodness threshold or convergence certificate.
`good_fraction` is therefore null (N/A in the dashboard) by default, not fitted/submitted. If the user
supplies existing `chi2_max`/`sigma` quality cuts, it counts passing fitted results
over all finished fitted results, using central point-only scores; missing score
coverage leaves it undefined. Manual only/exclude/best selections are not new
quality thresholds. `fitted_fraction` is fitted/submitted. `completion_fraction`
is finished-summary/submitted and explicitly does not infer missing scheduler-only
outcomes. Failed, not-fitted, missing-MSR and missing-summary results remain
separate. Empty selections have no ensemble chi-square; zero-point datasets stay
listed without adding invented residuals or penalties.

### Replica progress, checkpoints and explicit restart

New campaign arrays write Slurm-managed logs under
`/farm_out/<user>/<collection_id>/out/` and `/err/`. Both directories are created
before submission. Each job records its stdout/stderr prefixes separately;
historical flat log prefixes and legacy stage logs remain readable.

The application runner suppresses repeated full parameter/dataset tables while
retaining native residual calculations. It emits a concise objective/progress
line about every 30 seconds. Python stdout and stderr have per-process budgets
of 1 MiB and 512 KiB (plus a single budget notice); they are never reopened or
truncated. Low-level native/subprocess output is not governed by Python streams.
Full-precision independent parameter values/order, point/correlated/normalization
terms, recent diagnostics and solver termination metadata are available on Ceph:
`stages/pdf_stage/checkpoints/replica_N/current_progress.json`, `latest.json`,
and immutable `attempt-*/bootstrap.npz`. Current progress is replaced atomically
about every five seconds. The verified initial optimizer base is checkpointed
immediately, and every accepted step replaces it immediately at full precision.
Periodic/exit flushes update evaluation counts without replacing those parameters
with numerical-Jacobian probes or rejected trial vectors. Previous attempt records
remain available. Checkpoints live outside `current/`, so ordinary rerun result
archival does not move or destroy them.

`rerun_replicas(collection_id=..., replicas=..., resume=true)` is an explicit
restart option in the existing confirmed More/Rerun workflow. Its read-only plan
checks every selected checkpoint and pins checksums in the confirmation phrase.
Missing, corrupt, changed-input or incompatible checkpoints fail without falling
back to prior/random starts. Records also bind the campaign ID and absolute stage
path: moving a checkpoint into another campaign is rejected even for identical
inputs and replica numbers. The runtime restores parameters by their scientific
identities **after** native/prior initialization, preserving fixed/link rules and
bounds. A bootstrap payload stores sampled values and deviation arrays with
original-data identities; it is restored before worker creation. The initial
restored objective must match exactly before SciPy starts. Seed replay was
verified for JAM24 in the existing scientific container, but persisted data—not
that assumption—protect resumption. A central-data post-fit diagnostic still uses
central data. `resume=false` (default) preserves fresh/prior/random behavior;
More Replicas continues to add new identities rather than resume old ones.

The existing Rerun MCP tool accepts an optional per-submission memory override:
`rerun_replicas(collection_id=..., replicas="failed", mem="6G", resume=false)`.
Use the preview's confirmation phrase on a second call with the same options.
Only memory is overridden; saved time, CPU count and partition policy are retained.
An automatic partition is resolved from the saved time limit (48 hours selects
the production partition on JLab). The plan shows effective `slurm` resources
and `resource_overrides`; changing resources invalidates the confirmation.
Omitting `mem` retains the original card's resources, including on resumes.
Pass `mem="6G"` again with `resume=true` to use that memory for a later restart.

Each confirmed rerun writes a small `slurm/rerun_<unique>_request.json` resource
manifest and records effective resources in its new registry job entry. The
original `input_card`, prior job entries, full pinned prior snapshot, scientific
`input.py` and `manifest.resolved.yaml` are not rewritten. Replica IDs and their
prior/bootstrap mapping remain unchanged. `replicas="failed"` selects unfinished
replicas and excludes fitted results (including a fitted result whose Slurm exit
was unsuccessful); an explicit replica list retains the existing intentional
refit behavior. Earlier output is archived only for selected IDs. This does not
append or renumber replicas, create a second fitting path, or change checkpoint,
logging or central-data chi-square conventions. The memory override is available
through MCP; existing interactive More/Rerun calls without it remain compatible.

This is **optimizer restart**, not restoration of trust-region/Jacobian/history
state. SciPy starts normally at the saved vector and recalculates derivatives.
`point_evaluations` identifies the saved base/accepted point; `evaluations`
counts all completed objective calls cumulatively across resumes, including the
exact restart-boundary verification. `last_evaluation` in current progress is
diagnostic only, and may describe a probe/rejected trial. Accepted-step counts
are cumulative, with separate per-attempt counts.

The existing SciPy 1.14.1 has no public iteration callback. An application-local
copy of its Python entrypoint observes its existing Jacobian-base notifications:
the initial base, then bases requested only after TRF/dogbox accepts a step. Native
Jacobian calculation, trust-region logic, objective calls and scientific settings
are unchanged; no derivative/state serialization is implemented. This private
integration is restricted to the reviewed SciPy 1.14.1 TRF/dogbox, two-point, linear-loss
path and checks source fingerprints before optimization. Unknown implementations
fail closed rather than guessing acceptance. SciPy/FitPack files and process-wide
SciPy bindings are not modified. Legacy last-evaluation checkpoints lack an
acceptance guarantee and are rejected for resume; they are not rewritten or
silently treated as accepted points.

SIGTERM/INT trigger a best-effort flush and runner-owned
worker cleanup. SIGKILL/OOM cannot flush: at most the last valid persisted record
is available. Existing campaigns without checkpoints cannot be resumed this way.

### Prior-replica starts (local implementation; partial source-format support)

Campaign manifests are parsed as JSON first, including JSON requests with a YAML
filename. Supported non-JSON YAML still uses the existing YAML parser. This
preserves numerical exponent literals such as `1e-10` as floats. Materialization
must verify the pinned prior snapshot in the final lock, campaign manifest,
resolved stage manifest and generated input before returning success. Any
integrity mismatch stops the existing submission command before `sbatch`.

The on-disk `campaigns/collections.json` format remains unchanged, with complete
scientific snapshots. Registry writes return small acknowledgements. Read
transport uses bounded, revision-checked pages and replaces only the large
prior-snapshot array with an internal hash/count reference. Updating campaign
metadata resolves that reference against the existing stored snapshot, preserving
its full values exactly; a stale reference fails without writing. Exact
collection-ID lookup reads only that entry, and existing name lookup remains
supported. These are internal data-transport changes, not new MCP tool arguments.

Use the ordinary campaign card with `parameters.priorSource` and inspect it with
`preview_prior_replicas(card)` before `validate_campaign` and the existing confirmed
`submit_campaign`. No alternate executor or objective penalty is introduced.

Campaign validation, prior preview, and submission use the existing background
job mechanism. A fast request returns the usual final result. Otherwise, within
about five seconds it returns `status="running"` and a `job_id`; call
`analysis_result(job_id)` until the final result arrives. Campaign polls also
wait at most about five seconds. Do not repeat the original request, especially
a confirmed submission. The composer follows this continuation automatically.
Validation and unconfirmed submission preparation never create a campaign or
submit a Slurm job. All scientific checks and the existing confirmation phrase
still apply before submission. As with existing analysis jobs, pending jobs are
process-local and are not retained across gateway restarts.
If the gateway restarts during a confirmed submission, check `list_campaigns`
and `campaign_status` before attempting another submission. A completed
submission retains its Slurm `job_id`; `preparation_job_id` identifies the
background preparation separately.

Prior preview verifies the immutable snapshot once per ensemble, builds one
target-first scientific identity map, and checks the bounds of **every** matched
value in **every** assigned replica. It returns the full deterministic assignment
list and one shared parameter-identity report, without returning replica value
arrays. No completed-validation cache is used: a new validation or submission
checks the current source again. Identical in-flight requests join the same job;
polling does not reload sources, repeat remote checks, or redo the mapping.

Example for two explicitly selected published source replicas:

```json
{"parameters":{"priorSource":{"kind":"archive","analysis_id":"JAM24.final","replica_ids":[0,1]}},"job":{"replicas":2}}
```

Sources are resolved server-side through existing user/workspace checks. Supported:
registered numeric published archives; non-pickled structured `.npy` records with
exact fields `replica, kind, object, parameter, value`; canonical JSON snapshots;
and completed campaigns containing canonical `fitted_parameters.json` snapshots.
For external NPY supply `scientific_settings` in the selector: `Q20`,
`beta version` when fitting PDFs, and the relevant `ffpion parametrization`,
`ffkaon parametrization`, `ffhadron parametrization` settings when fitting FFs.
Missing or incompatible scale/parametrization metadata blocks preview/submission.
Native FitPack `.msr` and order-bearing object-container NPY are supported by a
data-only opcode reader. It reconstructs only primitive containers and observed
numeric NumPy array states; it never invokes Python's pickle loader or serialized
functions/classes. Unknown constructors/opcodes/layouts are explicitly rejected.
The supplied NPY layout is `{conf, order, params}` with an ensemble matrix; the
supplied MSR layout is `{order: {step: [...]}, params: {step: vector}, chi2: ...}`.
Single-step MSR selects its only step. Multi-step files require explicit integer
`step`; no implicit latest-step choice is made. Each vector is paired with its
source step's own order, then mapped to target identities independently of position.
NPY carries source conf metadata. MSR without conf requires explicit scientific
metadata (or available saved campaign-card metadata), never guessed from its target.
Completed campaign loading also supports legacy native MSR, with source file hashes
and IDs. Missing fitted outputs require explicit source selection rather than an
implicit omission. Existing campaign permissions and read roots are unchanged.

Identity keys are FitPack triples `[1, object, coefficient]` or
`[2, reaction, dataset_id]`. Values, identities, source hashes, scale and replica
selection are frozen into the campaign manifest/input, not reread from mutable
source files by each job. Source Q20 must match the destination Q20. No scale or
parametrization conversion is implemented; compatible parameter definitions are
required. Bounds mismatches are hard failures, not clamped values.

Without `replica_ids`, source and job counts must match exactly. With explicit
IDs, the list must contain exactly one unique source ID per requested job. Its
order defines the mapping: element 0 -> zero-based job 0 -> Slurm replica 1.
Repeating, truncating or reshuffling is never implicit. Reruns keep the same
assignment; append is refused until a new explicit assignment is constructed.

The application runner wraps native `MAXLIKE.get_guess`, lets native random /
Gaussian sampling run, then overlays only matching free parameters and calls
native `set_new_params` for links. It covers both the FitPack audit and optimizer
instances. `prior_initialization.json` records effective starts before optimization.
Absent parameters retain the native sampled values. Fixed/linked declarations
are excluded. With no prior selected the wrapper is not installed.

Adding SIDIS proton datasets to JAM24 introduces no new fit parameters: these
measurements have no normalization parameters. New independent PDF/FF coefficients
or normalizations in other analyses retain native random/Gaussian initialization.
Removed or newly linked coefficients are excluded from the target free order;
old independent source values for those identities are reported unused. The
surviving independent identity supplies its own prior value, and native FitPack
link propagation enforces the new dependencies (for example sea2 linked to sea1).
Source values are never relabeled merely because the new fit has different links.

Read-only checks against the supplied JAM24 NPY reproduce all 791 x 258 archived
values exactly. Three supplied MSR examples contain step 1 and 205 ordered
parameters; reordered target mappings were verified. Input hashes are unchanged.
Remote source filenames use POSIX normalization even when a local MCP test host
runs Windows. Native NPY/MSR fixtures and the supplied original NPY were checked
through the actual local MCP handler and existing file-root checks, using the real
JLab scientific worker. No permissions or permitted roots were changed. Source
selection does not imply access to arbitrary filesystem locations; use a registered
archive or an already authorized source. No user-facing replica upload/import tool
is currently provided; inaccessible files must use an existing authorized file
placement mechanism, not a new read-root exception.

The isolated non-fitting end-to-end check uses `JAM24.current` plus the six
registered COMPASS proton SIDIS datasets: 1115 (254 points), 1116 (254),
2115 (250), 2116 (243), 3110 (254), 3111 (254), after the existing inclusive
Z and DIS cuts. This adds 1,509 points: 3,037 SIDIS and 7,400 total, with
104 configured/102 nonempty datasets. No parameters or normalizations are added.
Published rows 0/1 match all 258 native free starting values for Slurm tasks 1/2,
including `prior_initialization.json`, native PARMAN values and propagated links.
Existing retained row identities, settings and all 297 archive file hashes are
unchanged. The runner dispatch is intercepted before the fit module executes;
native readers and native initialization are exercised separately, with no
minimization or submission. Both resolved YAML and the materializer's JSON fallback
are supported when detecting the prior assignment. A future real fitting run still
requires ordinary campaign validation and explicit submission confirmation.
