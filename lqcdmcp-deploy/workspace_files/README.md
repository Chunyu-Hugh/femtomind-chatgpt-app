# Workspace files

What a fresh clone of QCDHUB/fitpack_legacy lacks for the gateway, laid out
as in a workspace. `setup_workspace` and `use_workspace` write each file the
workspace does not have yet; an existing file is never overwritten.

| File | Why |
|---|---|
| `fitlib/maxlike_jam.py` | The driver of single fits (`submit_jam_fit`). Upstream `maxlike.py` plus: `progress.json` for the panel, the workspace's own code on `sys.path` (upstream hardcodes another user's checkout), `-s` seed, the warm-start `deviation-buffer` fix, non-zero exit on failure. Same copy as `fitpack_overlay/`. |
| `tools/env.py` | `bootstrap_fitpack_path`, imported by `maxlike_jam`. |

Campaigns (the JAM app composer) need neither: they run `fitlib/ai.py` from
the FitPack `ai` branch through `jamai/run_replica.py`. This README is not
copied.
