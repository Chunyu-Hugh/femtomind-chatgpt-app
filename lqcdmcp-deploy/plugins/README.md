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
access) and the panel resources `ui://nesap/sbi-panel` and
`ui://nesap/sbi-panel.skybridge`. The science stays in the SBI repository,
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

## Adding a plugin

Create `plugins/<name>/plugin.py` (`<name>`: lowercase letters and digits)
with `NAME`, `SHIP`, `INSTRUCTIONS`, `load()`, `tools()` and `resources()`, as
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
  the JAM instructions are unchanged and followed by the SBI text (in shared
  mode the shared-gateway note stays last). Three tools and two resources are
  added.
- `sbi_infer` gives the archived Benchmark 3 result (best fit 0.11023130887189442,
  68% set [0.109866, 0.110594]); unknown benchmarks and extra arguments are
  tool errors; `username` is accepted in shared mode.
- An altered SBI data file leaves SBI out and the gateway identical to before;
  `hot_redeploy.py`'s stage check refuses that package. A missing submodule
  is left out of the job script with a note.

Not yet checked: a deployed gateway on the cluster, ChatGPT or another MCP
Apps host rendering the SBI panel.
