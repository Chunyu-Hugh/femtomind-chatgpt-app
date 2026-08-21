# FemtoMind

A ChatGPT plugin for running JAM QCD global analyses on a laboratory HPC
cluster. The model routes and explains; the researcher enters every physics
value; validated code computes on the cluster under the researcher's own
account.

The split is the point. A fit is defined by dozens of numerical choices —
perturbative order, `Q0²`, target-mass treatment, DGLAP solution, dataset
selection, cuts — that are settled by collaboration practice, not by
optimisation, and whose consequences are invisible until the χ² comes back. An
assistant will run the loop unattended, but it overlooks the judgements that end
a stage. So the frame is fixed in code and the model explores inside it:
finding datasets, diagnosing failed jobs, reading results back.

## What it does

Sign-in through a two-hop MFA gateway, workspace setup, dataset selection, fit
configuration, SLURM submission, job tracking, χ² summaries and replica PDF
plots — without leaving the conversation and without opening a terminal.

## Architecture

```
ChatGPT
   │  MCP over HTTPS  (OAuth 2.1, PKCE, per-user bearer token)
   ▼
femtomind_app.py            :8010/mcp
   │  22 tools + 5 ui:// widgets
   │  identity → SSH connection → workspace → history, per collaborator
   │
   ├── plain HTTP ──▶ mcpo gateway :8000
   │                    fitpack MCP server   (datasets, fits, summaries, plots)
   │                    hpc MCP server       (SLURM queue, quota)
   │
   └── SSH ControlMaster ──▶ gateway ──▶ cluster login node
                                            SLURM array jobs
                                            apptainer + the group's fitpack
```

Nothing scientific runs on the application host. Each replica is a SLURM job in
the group's container image, and even the plotted replica curves are computed
on the cluster and returned as numbers the widget draws. The server needs no
compute of its own and no inbound access to any laboratory network.

Only the physics tools know about QCD. Identity, connections, job tracking and
history are domain agnostic, so another field can replace the domain module and
keep the rest — though the two-hop login is specific to this facility.

## Layout

```
femtomind_app.py     MCP server: tools, widget resources, auth middleware
femtomind_auth.py    OAuth 2.1 authorization server (invite codes, PKCE)
fitpack-workflow/    the physics backend — dataset catalogue, job submission,
                     SLURM tracking, chi-square, replica plotting. The app
                     itself holds no domain logic; it authenticates and
                     forwards here. See fitpack-workflow/README.md.
widgets/             the five-tab panel and the inline result cards
scripts/femtoadmin.py   invite / list / revoke collaborators
scripts/run.sh          start the server locally
deploy/                 systemd unit example
```

`femtomind_app.py` must stay at the package root: the systemd unit invokes it by
path, and `widgets/` is resolved relative to it.

## Running it

Requires a reachable mcpo gateway fronting the fitpack and hpc MCP servers —
this repository is the conversational front end, not the physics stack.

```bash
pip install -r requirements.txt
cp .env.example env.conf        # then fill it in
set -a; . ./env.conf; set +a
./scripts/run.sh                # or FEMTOMIND_PYTHON=/path/to/venv/bin/python ./scripts/run.sh
```

For a deployment, copy `deploy/femtomind-app.service.example` and read the note
about `KillMode` before changing it.

## Access

The endpoint is public, so every request must say who is making it. The server
is its own OAuth 2.1 authorization server: a collaborator redeems an invite code
once on a consent screen and thereafter holds a bearer token, with PKCE
enforced and tokens stored hashed.

```bash
./scripts/femtoadmin.py invite "Nobuo Sato" nsato   # mint a code
./scripts/femtoadmin.py list
./scripts/femtoadmin.py revoke u_ab12cd34           # kills live sessions too
```

The authenticated identity — never a client-supplied argument — selects the
user's SSH connection, workspace and history, so each collaborator sees only
their own jobs and runs them under their own cluster account and quota. Every
replica is stored under the researcher who submitted it, with the configuration
that produced it and the χ² it reached, so authorship can be audited.

## Connecting from ChatGPT

The server advertises no dynamic client registration, so register it as a
User-Defined OAuth Client:

| Field | Value |
|---|---|
| MCP server URL | `https://<FEMTOMIND_PUBLIC_HOST>/mcp` |
| Authorization URL | `https://<FEMTOMIND_PUBLIC_HOST>/authorize` |
| Token URL | `https://<FEMTOMIND_PUBLIC_HOST>/token` |
| Client ID | any value; the server does not check it |
| Client secret | leave empty |
| Token endpoint auth method | `none` (public client, PKCE) |

Bumping the widget version in `OPERATIONS_URI` is how a cached copy of the panel
HTML is dropped. Retired versions stay registered and serve today's HTML, so a
bump does not break clients that still hold an old tool list.

## Limits

The boundary is held by the interface and the tool contract, not by the
protocol: `submit_fitting_job` takes ordinary parameters, and its guards reject
known-bad datasets and out-of-range counts rather than values a model invented.
Making the restriction explicit at the protocol level is the obvious next step.

Curves drawn from a running campaign are a snapshot, not a result. A replica's
`parameters.yaml` holds its randomised starting point until the fit ends, so
mid-run curves come from the current parameter vector that the patched
`fitlib.maxlike_jam` appends to `progress.json` on every iteration. Those
replicas are still moving and the band will narrow; `get_pdf_plots` returns
`n_running` alongside `n_completed` so the count can be stated rather than
implied. Pass `include_running=False` for finished replicas only.

This depends on the fitter being the patched one shipped in `fitpack_overlay/`.
Against stock fitlib, `progress.json` carries chi-square but no parameters, and
running replicas are skipped with a note instead of being drawn from a starting
point that means nothing.

RESMAN is bootstrapped from a full multi-distribution input card, so a PDF-only
fit legitimately leaves the fragmentation, polarised and higher-twist families
at their central values. Those positions do not enter a proton PDF curve; the
worker warns only when the family actually being plotted is short of values.
