# FemtoMind JAM Gateway — Collaborator Quickstart

A working MCP gateway on the JLab LQCD cluster (node `qcd24s100`) that drives
real JAM fits on ifarm through the two-hop chain
`node → scilogin.jlab.org (PIN+OTP) → ifarm (password)`.

## Before you start

1. **Timing**: the gateway runs as a Slurm job on a daily reservation
   (`chen_mcp_daily`). It is alive roughly **10:00–16:00 US Eastern**. If
   everything reports "Backend not found", the window is closed — try again
   the next morning.
2. **Your own JLab account**: you connect as yourself (your JLab username,
   your PIN+OTP, your ifarm password). Nothing runs under anyone else's
   name. Science tools read/write `/w/ceph24/JAM/<your-username>/...` — ask
   Chunyu if your workspace isn't set up yet.
3. The gateway URL (pick ONE entry):
   - direct: `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp`
   - port-443 tunnel: `https://seniors-loc-directive-memphis.trycloudflare.com/cloud/jam-gateway/mcp`
     (use this if your client refuses port 8000 — Claude Desktop does)

## Option A — Claude Code (terminal)

```bash
claude mcp add --transport http jam-gateway \
  https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp
claude mcp list        # expect: jam-gateway ✔ Connected
claude                 # then ask: "connect to the HPC"
```

Claude will ask for your JLab username (say your username). If the chain is
already alive for you it reuses it and asks for nothing else; only if it is
dead does it need your ifarm password and PIN+one-time-code (concatenated,
no space).

## Option B — web console (no terminal)

Open in a browser:

```
https://seniors-loc-directive-memphis.trycloudflare.com/cloud/jam-gateway/mcp
```

1. Enter your JLab username → click **连接 (Connect)**
   - chain alive → "reused, no credentials needed"
   - chain dead → the page reveals PIN+OTP / password fields; fill them and
     click Connect again
2. Pick a card from the dropdown (e.g. `input_jam24_s7_warm.py`), set the
   output dir, prior .msr, hours, cores, partition → **提交拟合 (Submit)**
3. Watch it with **读 χ² (read chi2)** / **对照论文 band (evaluate)** buttons

## Option C — Claude Desktop (GUI)

Add a custom connector:
- URL: the **port-443 tunnel** URL above
- Preflight may say "Couldn't reach" — click **Continue anyway**
- Authentication: **No sign-in**, headers empty
- In a chat, enable the connector and ask "connect to the HPC"

## Tools (12)

`ping, get_console, connect_hpc, check_hpc, submit_test_fit, get_submit_result,
node_diag, remote_exec, list_jam_runs, submit_jam_fit, get_jam_result,
read_chi2, evaluate_fit`

- `submit_jam_fit` — real apptainer `maxlike_jam` fit on ifarm (fire-and-forget)
- `read_chi2` — live χ²/N series + per-dataset breakdown
- `evaluate_fit` — finished .msr vs the JAM24 paper band
  (arXiv:2501.00665, χ²/N mean 2.566, band [2.190, 2.819])
- `get_console` — the web console URL (agents hand this to users)

## Notes

- One PIN+OTP lasts one gateway job boot (~6 h). After a restart you
  reconnect once and everything rides the same chain.
- Known benchmark: a warm step-7 fit (card `input_jam24_s7_warm.py`, prior
  from `001/step6_v2/msr/8XW0AVCIGLJ8.msr`, 4 cores, jupyter) converges to
  **χ²/N ≈ 1.452** in ~40 min — identical to the human-run ladder.
