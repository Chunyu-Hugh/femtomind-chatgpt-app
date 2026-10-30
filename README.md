# FemtoMind — JAM gateway for LQCDMCP

FemtoMind lets a JAM collaborator run FitPack global QCD analyses on JLab's
ifarm by talking to an AI assistant (Claude Code, Codex) or by pointing and
clicking in a browser panel. Each person runs their own gateway on the JLab
LQCD cluster, as their own Slurm job, holding their own cluster sign-in. The
assistant decides what to do; the gateway checks it, runs it with FitPack's
own code and records it. The model never writes a fitted number.

- **Collaborators:** start with [lqcdmcp-deploy/COLLABORATOR_GUIDE.md](lqcdmcp-deploy/COLLABORATOR_GUIDE.md).
- **Maintainers:** [lqcdmcp-deploy/README.md](lqcdmcp-deploy/README.md) (how it works, security model, deployment, what was verified).
- **Published-analysis catalog and reference results:** [docs/](docs/) and [analyses/JAM24/](analyses/JAM24/).

```
AI assistant (Claude Code, Codex)  ─┐
                                    ├─ MCP ─>  your gateway (LQCD node)  ─ SSH as you ─>  ifarm: FitPack, Slurm
browser panel                      ─┘
```

## What it can do

### Connect and set up

- [x] One gateway per person on the LQCD node, launched and checked with one command (`deploy_and_verify.py`).
- [x] Cluster sign-in (PIN + one-time code, ifarm password) typed only on the panel, never in chat.
- [x] Workspace: clone FitPack's `ai` branch into your area, or reuse a checkout you already have; checks that it is ready.
- [x] Shared mode for colleagues who cannot launch a gateway: one token per person, each acting as themselves.
- [x] Works from Claude Code and Codex; hosts that show MCP Apps also show the panel inside the chat.

### Compose a fit

- [x] Objects: proton PDF, pion PDF (with the leading-neutron companion), pion, kaon and residual-hadron fragmentation functions.
- [x] Eleven reactions: DIS, Drell-Yan, pion Drell-Yan, leading neutron, W asymmetry, Z rapidity, W-lepton asymmetry, W+charm, jets, SIDIS, SIA.
- [x] Datasets grouped by experiment, with labels; normalizations that can be kept, changed, freed, fixed or tied to another dataset.
- [x] Every parameter shown with its library value, bounds and FREE/FIXED state; ties between parameters; nothing preselected for you.
- [x] Cuts per reaction or per dataset; QCD settings (order, Q0², TMC, DGLAP and αs modes, positivity per object).
- [x] Look inside datasets before fitting: points per cut, kinematic coverage, uncertainty breakdown, read exactly as FitPack reads them.
- [x] Validation reads the data with FitPack's own readers and refuses a fit that would have no data points.
- [x] Start replicas from earlier results: the published JAM24 replicas, a file, or another campaign.

### Run and follow

- [x] Submit a campaign as one Slurm array (one task per replica), after you read the plan and confirm it.
- [x] Live status: which replicas are loading, optimizing, fitted, did not fit or failed, and why (logs explained in plain words).
- [x] Stop, delete (with a backup of the record), add more replicas, rerun failed ones (optionally with more memory).
- [x] Checkpoints of accepted optimizer steps, and exact restarts from them.
- [x] Optional automatic recovery, agreed at submission: a replica Slurm ends at its time limit continues from its checkpoint, one out of memory runs again with twice the memory, at most 3 times; every attempt recorded.
- [x] Any other cluster job: draft a Slurm script with the assistant, test it with `sbatch --test-only`, submit after you confirm.

### Look at the results

- [x] Fit summary: fitted / not fitted / failed replicas, χ² per replica and of the mean prediction, per dataset, worst first.
- [x] One χ² convention throughout: the central-data point term, with correlated and normalization penalties reported apart.
- [x] Why each optimizer stopped, in words, and a warning for replicas that count as fitted but stopped far from the data (χ²/N ≥ 5); nothing left out unasked.
- [x] Replica selection for the statistics (best N, χ² cut, outlier cut), without refitting.
- [x] Data vs theory by experiment, PDF bands at any Q² with chosen credible intervals, FF plots, thesis-style PNG figures.
- [x] The per-dataset table in pages, worst-fitting first on request, so a global fit's 96 datasets fit an assistant's reply.
- [x] Per-replica numbers: each replica's χ² (total, by reaction, by dataset) and fitted parameters, with their spread (`campaign_replica_values`).
- [x] Predictions cached incrementally, so later views are fast; many replicas are evaluated by a Slurm array on the farm's compute nodes, one data load per task.
- [x] A campaign report to keep or send: configuration, data, results and provenance in one Markdown file, every input listed with its sha256.

### Reproduce

- [x] Every submission records what it ran: FitPack commit and uncommitted changes, container, input hashes, seeds, CPU model per replica (`reproduce.json`).
- [x] Look up how any earlier campaign was made: each submission's code, saved changes, image and seed rule, what changed between runs, and per replica the seeds, node and commit it actually used, with its reproductions (`campaign_provenance`).
- [x] Run replicas again exactly: rebuilds the recorded FitPack state, runs on the original node type and compares with the earlier results.

### Published analyses (JAM24)

- [x] The JAM24 configuration as a provenance-labelled catalog (published `JAM24.final` and updated `JAM24.current`), with open questions listed.
- [x] A compiler from the catalog to FitPack input, with an exact parity check against the official input.
- [x] Run or extend JAM24 from its catalog entry (all 215 free parameters), changing only what you declare; a JAM24-named fit built otherwise is refused.
- [x] The 791 archived JAM24 replicas as read-only reference results: fit statistics and theory predictions per dataset.
- [x] Compare a new fit with the published one, dataset by dataset: where its χ² falls among same-size subsamples of the published replicas, with multiple-comparison corrected p-values (`compare_with_reference`).

### Guidance for the assistant

- [x] Step-by-step procedures (new campaign, reproduce a published analysis, diagnose replicas, analyze, verify, other Slurm jobs) and JAM conventions, as a guide tool and as MCP prompts.
- [x] Each rule also travels in the tool descriptions and replies, for clients that drop the MCP instructions.
- [x] Long work answers within the platform's 30 s limit with a job id to poll.

### Safety and accountability

- [x] Each gateway has its own token; the platform proxy checks nobody, the gateway does.
- [x] Every write (submit, add, rerun, reproduce, stop, delete, Slurm job) needs a confirmation phrase bound to that exact request, not accepted in the same turn; repeats are answered, not resubmitted.
- [x] Optional human gate: the phrase is shown only on the panel, so only a person can confirm.
- [x] Tools can be switched off per deployment; tools carry read-only / destructive hints for hosts.
- [x] Hash-chained audit log of every call and effect, mirrored into your own area; changing the gateway's code needs a second secret.

### Other physics tasks

- [x] Plugin framework: other tasks share the gateway; questions are routed to the right task.
- [x] NESAP SBI demonstrator (αs from Pythia benchmarks) as an optional plugin with its own panel.

### Not yet

- [ ] ChatGPT in the browser (needs an OAuth sign-in the platform does not offer).
- [ ] HEP-Genesis-Agent and other MCP clients tested end to end (it supports our token header).
- [ ] Automatic postprocessing when a campaign finishes.

## Repository

| Path | What |
|---|---|
| [`lqcdmcp-deploy/`](lqcdmcp-deploy/) | the gateway (`jam_gateway.py`), its panel, `jamai/` helpers, plugins, deployment and audit scripts |
| [`analyses/JAM24/`](analyses/JAM24/) | the JAM24 catalog and the reference-results manifest |
| [`docs/`](docs/) | the analysis catalog and reference-results tools |

Built by Chunyu Hu (gateway, deployment, security, provenance, panel) and
Trey Anderson (analysis catalog and compiler, SIDIS/SIA and fragmentation
functions, plotting, χ² reporting, prior replicas and checkpoints).

The former AWS-hosted ChatGPT app was retired on 2026-10-08; it lives in git
history (commit `7b99311` and before).
