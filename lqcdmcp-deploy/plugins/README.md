# Plugins: other physics tasks on the same gateway

The gateway (`jam_gateway.py`) serves JAM. Other physics tasks plug into it
here, each in its own folder, and share its MCP endpoint, access check,
panel hosting and deployment. JAM is not a plugin and plugins never change
its tools, panel or instructions: with no plugin loaded the gateway answers
exactly as it did before plugins existed.

```
plugins/
  __init__.py        loader: finds, checks and attaches plugins; lists files to ship
  sbi/
    plugin.py        adapter, kept in this repository
    upstream/        git submodule: FelixRinger/femtomind-nesap-sbi, pinned to one commit
```

## NESAP SBI

Adds `sbi_options`, `sbi_infer` and `sbi_validation` (read-only, no cluster
access). The model answers in text from the tool results, in the format its
instructions give (best fit, both confidence sets, checks, what ran live),
and the hosts that show MCP Apps or ChatGPT widgets also show upstream's
interactive panel under the answer: the likelihood curve with both
confidence sets, the generating truth on request, another benchmark run from
the panel and the result JSON (resources `ui://nesap/sbi-panel` and
`ui://nesap/sbi-panel.skybridge`). It draws the conversation's result, which
upstream's panel does from commit `f41c675`. `PANEL = False` in
`sbi/plugin.py` turns the panel off, for text only. The science stays in the SBI repository,
which is not modified here; `plugin.py` only maps its tools onto the gateway,
removes `username` (a shared gateway applies it first) and appends the SBI
guidance to the MCP instructions. Only the runtime files named in `SHIP`
travel to the node (about 40 kB in the job script): no docs, tests or
reference snapshots.

```bash
git clone --recurse-submodules <this repository>    # or, in an existing clone:
git submodule update --init
```

Without access to the SBI repository the submodule stays empty: the deploy
scripts print a note and the gateway runs JAM alone.

The deploy scripts ship the files on disk, so they first check the submodule:
it must be at the commit this repository pins and have no edited or
untracked files. `git pull` does not move a submodule; after a pull that
changed the pin, run `git submodule update`. Otherwise SBI is left out with
that reason, never shipped at an unrecorded version.
`deploy_and_verify.py --check` lists each plugin's tools on the running
gateway, or says it is missing.

**Updating SBI.** Changes are made and reviewed in the SBI repository. To take
a new version here:

```bash
cd lqcdmcp-deploy/plugins/sbi/upstream
git fetch && git checkout <commit or tag>
cd ../../../..
git add lqcdmcp-deploy/plugins/sbi/upstream    # records the new pinned commit
```

then redeploy (`hot_redeploy.py` or `deploy_and_verify.py`). Going back is
checking out the previous commit the same way.

## How the model picks a task

All tools of all tasks reach the model in one list, so the gateway tells it
which questions go where. When a plugin is loaded, the MCP instructions open
with a short routing note (one line per plugin, its `ROUTING`, then one line
for JAM), followed by the JAM text exactly as before and then each plugin's
`INSTRUCTIONS`. The SBI tool descriptions start with "NESAP SBI only." and
name what they answer (alpha_s, the Pythia benchmarks, the classifier) and
that they need no HPC sign-in, workspace or Slurm. These descriptions live in
`sbi/plugin.py` and replace upstream's short ones without changing upstream.

This steers the model; it cannot force it. Try mixed questions in the real
client: an SBI question ("infer alpha_s for benchmark 2"), a JAM question
("fit the proton PDF to DIS data"), and ambiguous ones ("run a fit", "what
alphaS do we use?"). The model should call only that task's tools, and ask
when the task is unclear.

## Adding a plugin

Create `plugins/<name>/plugin.py` (`<name>`: lowercase letters and digits)
with `NAME`, `SHIP`, `ROUTING`, `INSTRUCTIONS`, `load()`, `tools()` and `resources()`, as
described at the top of `__init__.py`. Tool names must start with `<name>_`
and must not exist yet; resource URIs must be new `ui://` URIs. A plugin that
breaks any rule, or raises in `load()`, is left out whole and the gateway
starts without it (the start-up log says why). `hot_redeploy.py` refuses to
install an application whose plugin fails to load on the node.

`JAM_GW_PLUGINS` chooses plugins at launch and in the job: unset loads every
folder, `none` loads none, `sbi` loads only that one.

## What was checked (2026-10-19, local test gateway, not on the cluster)

A job script from `redeploy_jam.py script`, unpacked as the job does, was run
locally (loopback, test token, no Slurm) and compared with the gateway before
plugins:

- `JAM_GW_PLUGINS=none`: `initialize`, `tools/list`, `resources/list`,
  panel reads and `ping`, `get_console` and unknown-tool calls are identical,
  in normal and shared mode.
- With SBI: the same 29 JAM tools, panel resources and calls are identical;
  the JAM instructions are unchanged, preceded by the routing note and
  followed by the SBI text (in shared mode the shared-gateway note stays
  last). Three tools and the two panel resources are added.
- `sbi_infer` gives the archived Benchmark 3 result (best fit 0.11023130887189442,
  68% set [0.109866, 0.110594]); unknown benchmarks and extra arguments are
  tool errors; `username` is accepted in shared mode.
- An altered SBI data file leaves SBI out and the gateway identical to before;
  `hot_redeploy.py`'s stage check refuses that package. A missing submodule
  is left out of the job script with a note, as is a submodule at another
  commit than the pinned one or with an edited or untracked file.

Not yet checked: a deployed gateway on the cluster, whether a real model
routes questions to the right task and answers in the given format, and
a live MCP Apps or ChatGPT host rendering the SBI panel (only simulated
hosts in jsdom so far, in the SBI repository's commit `f41c675`).
