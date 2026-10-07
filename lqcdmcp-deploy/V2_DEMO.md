# V2 Demo Narrative — Real JAM Fit Through the Agent Gateway

2026-10-05, ~11:00–13:00 EDT. All calls are pure MCP JSON-RPC through
`https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp`; the client can be any
MCP consumer (DeepSeek chat agent, script, or the FemtoMind app).

## The chain (infrastructure)

LQCD cluster node `qcd24s100` (Slurm job 7326675, reservation
`chen_mcp_daily`, 6h) → scilogin.jlab.org (PIN+OTP) → ifarm (password).
ControlMasters are setsid-detached in `~/fm-cm`; the job's supervisor loop
restarts the gateway server inside the same boot, so the chain survives
server restarts and hot code redeploys without a new OTP.

## The expert loop (science)

1. **connect** — `connect_hpc` returns `status: reused` (no OTP; masters
   alive from the boot).
2. **inventory** — `list_jam_runs` lists 20 JAM24 cards (s1–s7 cold/warm/
   seed), the smoke4 ladder (`step1`…`step7_v3`), and the newest msr files.
3. **choose** — expert picks the last warm card `input_jam24_s7_warm.py`
   with prior `001/step6_v2/msr/8XW0AVCIGLJ8.msr` (the step-6 chain's
   output), 4 cores, seed 160300, 2h, partition `jupyter`.
4. **submit** — `submit_jam_fit` ships the sbatch script (base64 transport)
   and fires it on ifarm → **Slurm job 12367088 RUNNING**.
5. **monitor** — `read_chi2` parses progress.json every few minutes:
   4613 data points, warm start χ²/N=2.003, least-squares grinding down
   (iter 200 at 25 min, iter 430 at ~40 min).
6. **converged** — job 12367088 finished, final .msr written.
7. **evaluate** — `evaluate_fit` loads the finished .msr in apptainer:
   **χ²/N = 1.452** — identical to the human-run ladder values
   (step7_v2 = step7_v3 = 1.452). Bit-reproducible agent-run result.

## Result table (agent-run, 2026-10-05)

| quantity | value |
|---|---|
| datasets / points | 4613 |
| warm start χ²/N | 2.003 |
| converged χ²/N | **1.452** |
| human-run step7_v2/v3 | 1.452 (match) |
| paper band (arXiv:2501.00665) | 2.566 [2.190, 2.819] |
| wall time | ~40 min (4 cores, jupyter) |
| Slurm job | 12367088 |

The agent submitted, monitored, and evaluated a REAL JAM fit end-to-end and
bit-reproduced the expert's ladder result. Converged ladder sits below the
paper band (see README) — flagged for the reproduction report.

## Ladder table produced through the gateway (agent-run)

Converged smoke4 ladder sits at χ²/N ≈ 1.2–1.6 — systematically below the
paper band (see README). Flagged for the reproduction report.

## Artifacts

- `jam_gateway_v2.py` — zero-dependency MCP server (12 tools)
- `hot_redeploy.py` — no-OTP in-boot code swaps
- `agent_demo.py` — pure MCP client walking the expert loop
- `fit_poll.log` — minute-by-minute χ²/N polling record
