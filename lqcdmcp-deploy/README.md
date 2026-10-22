# JAM gateway on LQCDMCP

Each JAM collaborator runs **their own** MCP gateway, `jam-gateway-<user>`, as
a Slurm job on the JLab LQCD cluster, launched through Jie Chen's LQCDMCP
platform. It runs JAM fits on ifarm as that collaborator, from an MCP client
(Claude Code or Codex) or from the browser panel it serves, which mirrors
the FemtoMind JAM app's fitting campaign.

**Collaborators: start with [COLLABORATOR_GUIDE.md](COLLABORATOR_GUIDE.md).**
This file is for whoever maintains the gateway.

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
and no credential. It asks for partition `24s`, reservation `chen_mcp_daily`
(no time window since 2026-10-16; before, about 10:00–16:00 US Eastern), 6 h,
16 CPUs (a quarter of the reserved 64-core node, so four gateways fit on it,
each on its own port) and the `hadstruc24s` account, as agreed with Jie Chen
on 2026-10-19
(`JAM_GW_PARTITION`, `JAM_GW_RESERVATION`, `JAM_GW_HOURS`, `JAM_GW_CPUS` and
`JAM_GW_ACCOUNT` override; `JAM_GW_ACCOUNT=none` uses your default Slurm
account). The job unpacks the files into `~/.jam_gateway/app`, and a
supervisor loop restarts the server whenever it exits. The server takes the
first free port from 8080 and registers with the proxy as
`jam-gateway-<user>`, at `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<user>/mcp`.

- **Accounts.** Jie Chen must first enable an account for MCP servers and the
  reservation. Until then the dashboard's `launch_mcp_server_using_script`
  fails with `Slurm command failed: sudo sbatch --uid <user> ... (rc=1)`, and
  `deploy_and_verify.py` stops there with that explanation.
- **The reservation is required.** `chen_mcp_daily` has a user list (Jie
  adds people). The proxy reaches port 8080 only on the reserved node
  (`qcd24s100`); a gateway on another `24s` node starts but is unreachable
  ("No route to host", 2026-10-13). The partition allows the `hadstruc24s`
  account, not `jlablqcd24s`, so the job asks for `hadstruc24s`; every
  collaborator needs access to that account.
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
  the statistics from fitted replicas only (`not_fitted_replicas` apart,
  `good_fraction` = fitted / total). Data vs theory takes a dataset's
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

## Verified

| Date | Where | What | Result |
|---|---|---|---|
| 2026-10-08 | ifarm, through a local test instance (commit `0ad83c4`) | library table (39 rows), a card that differs from `input02.py` only in the changed row, stop and delete (delete refused while tasks are queued), postprocess, fit summary, a PDF plot, the analysis by experiment, an append plan; the panel in headless Chromium; Codex 0.161.0 and Claude Code 2.x connected (the guide's `${LQCDMCP_TOKEN}` header expanded) | all passed |
| 2026-10-09 | the platform, `jam-gateway-chunyu` (launched with `deploy_and_verify.py`, then updated in place with `hot_redeploy.py`; the cluster sign-in survived) | launch and `--check` through the proxy (token accepted, no token refused, 28 tools, `node_diag` hidden); a campaign with idis 10005 and 10006 really fitted (`mode: fit`, 35 fitted parameters, an `.msr` per replica); replica states, the data check, `campaign_logs`, the panel's Logs and warnings; a campaign from before the status-report guard shows as not fitted with its `KeyError: 'col'` traceback | all passed |
| 2026-10-14 | the platform, the owner's workspace (commits `ac1c95f`, `c7de6eb`) | `campaign_datasets` lists the normalizations of 10010 and 10016 from `input02.py` and none for 10005; a card with `g1 a` equal to `uv1 a` and normalization 10010 equal to 10016 wrote `'fixed': 'uv1 a'` and `'fixed': 10016` into `input.py`, the replica really fitted, 35 free parameters (35 − 1 tied + 1 free normalization); a self-reference refused | all passed |
| 2026-10-14 | offline only (commits `a26aac1`, `8d2ec2b`: pion PDF objects, per-dataset cuts, credible intervals) | the gateway's remote snippets and `jamai` run locally against a stand-in workspace and a stand-in pion reference: the proton path's generated `input.py` is unchanged except `conf['dis version'] = 1` (FitPack's default) and the override block's form; the older single `object` card gives the same confirmation phrase as `objects[]`; the pion path drops the second shape, fits `p->pi,n`, freezes the reference's proton PDF, writes per-dataset `ln` filters and `dis version` 0; the panel (jsdom) | all passed |

The offline suites are not part of this repository.

**Not yet verified live:** the pion objects on the platform (whether the
reference file is readable from each account and what the FitPack `ai`
branch does with dy-pion, ln, `pdf-pion` and `p->pi,n` under `dis version`
0); a workspace clone with a GitHub token; the deploy scripts' queue
handling when another gateway holds the reservation; the VS Code and
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
