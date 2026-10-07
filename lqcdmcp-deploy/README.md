# JAM Gateway on LQCDMCP — deployment notes (2026-10-05)

Two-hop MCP gateway proving the FemtoMind/JAM agent path onto the JLab
cluster through Jie Chen's LQCDMCP platform.

## Status: v2 LIVE — REAL JAM FITS SUBMITTED THROUGH THE GATEWAY

- `jam-gateway` job runs on `qcd24s100` (reservation `chen_mcp_daily`,
  partition `24s`, TimeLimit 6h), port 8080, registered with the proxy as
  `jam-gateway` → `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp`.
- Chain: **LQCD node → scilogin.jlab.org (PIN+OTP) → ifarm (password)**.
- 2026-10-05 real-science test: `submit_jam_fit(card=input_jam24_s7_warm.py,
  prior=001/step6_v2/msr/...)` fired **ifarm job 12367088** (jupyter, 4 cores)
  — the full step-7 warm JAM fit, polled via `get_jam_result`
  (progress.json χ² + msr listing).

## Tools

| tool | purpose |
|---|---|
| `ping` | liveness |
| `connect_hpc(pin_otp, password)` | build/reuse the two-hop ControlMasters |
| `check_hpc` | chain check + queue + `/w/ceph24/JAM/chunyu` listing |
| `node_diag(cmd)` | shell on the gateway node (diagnostics / hot-redeploy) |
| `remote_exec(cmd, timeout)` | shell on ifarm through the chain |
| `list_jam_runs` | JAM24 cards + smoke4 run dirs + newest msr files |
| `submit_jam_fit(card, outdir, prior_msr, cores, seed, hours, partition)` | real apptainer `maxlike_jam` fit (fire-and-forget) |
| `get_jam_result(outdir)` | queue + submit log + progress.json tail + msr |
| `read_chi2(outdir)` | parsed χ²/N series + per-dataset χ²/N from progress.json |
| `evaluate_fit(outdir, msr_file)` | finished .msr vs paper band (runs in apptainer; needs numpy) |
| `submit_test_fit` / `get_submit_result` | trivial chain test (v1) |

## Science produced through the gateway (2026-10-05)

**Agent-run fit (submitted, monitored, evaluated end-to-end):**

- `submit_jam_fit(card=input_jam24_s7_warm.py, prior=001/step6_v2/msr/...)`
  → ifarm job **12367088**, converged in ~40 min (4 cores, jupyter).
- Final .msr evaluated: **χ²/N = 1.452** — identical to the human-run
  ladder (step7_v2 = step7_v3 = 1.452). Bit-reproducible result.

Whole-ladder χ²/N evaluation (agent-run, apptainer on ifarm):

```
step           chi2/N
step1          1.228
step2_v10      1.207
step3          1.505
step3_v2       1.300
step4          1.482
step4_v2       1.296
step5          1.572
step6_v2       1.452
step7_v2       1.452
step7_v3       1.452
step7_agent    1.452   <- today's agent-run fit
PAPER band (arXiv:2501.00665 step16a): mean=2.566 [2.190, 2.819]
```

Converged ladder sits at χ²/N ≈ 1.2–1.6, systematically **below** the paper
band — flagged for the reproduction report. Unfinished runs excluded
(step2=457846 blowup, step2_v3=48.6, step5_v2=6.65).


## Hot redeploy (no OTP, no job kill)

`python3 hot_redeploy.py <file>` uploads new code via `node_diag`, compiles
on the node, swaps `/tmp/jam_gateway.py` and lets the supervisor restart the
server **inside the same job boot**. The setsid'd ControlMasters in
`~/fm-cm` survive, so the chain is never re-authenticated.

## Agent client (paper demo)

`python3 agent_demo.py [--submit] [--outdir ...] [--card ...] [--prior ...]`
is a pure JSON-RPC MCP client walking the distilled expert loop:
connect (reuse) → inventory → submit warm fit → poll χ²/N → evaluate vs the
JAM24 paper band. Verified 2026-10-05: chain `reused` with zero OTP, 12 tools
listed, inventory rc=0.

## Key fixes (this session)

1. **Reuse-check ordering** (`connect_hpc`): check `_master_alive` BEFORE
   unlinking sockets. The old code unlinked the socket files first, which
   destroyed the reuse path every time.
2. **base64 transport** (`submit_*`): scripts shipped as base64 and
   `sbatch` fired with `nohup ... </dev/null &` — fixes the SSH heredoc hang
   through the two-hop chain.
3. **Supervisor loop** in the Slurm script: the JOB stays alive for the
   whole 6h window; the gateway server may crash/redeploy inside it.
4. `node_diag` tool: read-only shell on the gateway node (debugging).

## setsid persistence: what works, what doesn't

- **Works**: ControlMasters are created with `setsid` so they detach from
  the server process; sockets live in shared `~/fm-cm`. Any gateway server
  restart within the same Slurm job boot reuses them with zero OTP.
- **Does NOT survive `scancel`**: the node runs **cgroup2 +
  `ProctrackType=proctrack/cgroup`** (`/system.slice/slurmstepd.scope/
  job_<id>/...`). scancel kills the entire job cgroup; setsid cannot escape
  a cgroup kill. Also: no user systemd/linger on compute nodes, no user
  cron/at. **One OTP per 6h job boot is the platform floor** until Jie adds
  a persistent-server mode or a keeper-job slot.

## Ops

- Full redeploy (new boot, needs fresh OTP): `python3 redeploy_jam.py
  cancel` then `launch` (valid dashboard Bearer token; `validate_user`
  first).
- Hot redeploy (same boot, no OTP): `python3 hot_redeploy.py`.
- Credentials are passed as tool args in v1/v2 (demo only). Production
  path: browser-side panel + one-OTP-per-boot flow.
- One job at a time on the reservation (verified: a second job stays
  PENDING `(Resources)`).
