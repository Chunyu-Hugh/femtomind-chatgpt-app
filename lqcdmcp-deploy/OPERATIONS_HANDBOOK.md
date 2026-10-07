# FemtoMind JAM Gateway — Operations Handbook

Complete handover document for whoever owns the pipeline. Covers
architecture, the full run flow, daily operations, troubleshooting, security
and the code inventory. Everything below is what was built and verified
2026-10-05/06.

---

## 1. Big picture

One MCP gateway on the JLab LQCD cluster lets an agent (or a person, or a
web page) drive **real JAM QCD fits on ifarm** through a two-hop SSH chain.

```
client ──HTTPS──▶ LQCDMCP proxy ──▶ gateway node ──SSH①──▶ scilogin ──SSH②──▶ ifarm
Claude/Codex/     lqcdmcp.jlab.org   qcd24s100       (PIN+OTP)    (password)  fitpack,
console/curl      :8000 (Jie's)      Slurm job                   via fwd port sbatch,
                                                              2222+hash      /w/ceph24
```

### Machines

| host | role | notes |
|---|---|---|
| `qcd24s100` (qcd24s###) | gateway node; Slurm cluster **knl**, partition `24s` (CPU) | fixed reservation node (`chen_mcp_daily`); job runs the MCP server |
| `qcd21g###` | GPU nodes, partition `21g` | no access for our account (sbatch rejected) |
| `scilogin.jlab.org` | JLab gateway/login | reachable from the node on :22; auth = **PIN+OTP** |
| `ifarm.jlab.org` (`ifarm2401/2402`) | the farm where JAM runs | NOT reachable from the node directly; reached via scilogin forward; partitions `production/jupyter/ifarm/priority` |
| `lqcdmcp.jlab.org:8000` | **Jie Chen's LQCDMCP platform** | public HTTPS; dashboard UI + `/jlab` MCP (manage jobs, Bearer token from Globus login) + `/cloud/<name>/mcp` (pass-through to registered backends) |
| `perlmutter.nersc.gov` | future NERSC backend | **CLOSED from the node**, OPEN from ifarm → NERSC would ride the ifarm hop |

### Network facts (verified)

- node → ifarm:22 ❌ closed (this is why the two-hop chain exists)
- node → scilogin:22 ✅ open
- node → perlmutter.nersc.gov:22 ❌ closed
- ifarm → perlmutter.nersc.gov:22 ✅ open
- the node kills everything in the job's cgroup on `scancel`
  (**cgroup2 + `ProctrackType=proctrack/cgroup`**) — `setsid` survives
  *server* restarts inside a boot but NOT job cancellation.

---

## 2. The gateway (what runs on qcd24s100)

A zero-dependency Python MCP server (`jam_gateway_v3.py`, stdlib only —
compute nodes have no pip/net), launched as a **Slurm job** through the
LQCDMCP dashboard with a **supervisor loop** so the 6 h job outlives server
crashes/redeploys:

```bash
#SBATCH --partition=24s --time=06:00:00 --reservation=chen_mcp_daily
while true; do python3 jam_gateway_v3.py; sleep 3; done
```

- Listens on `:8080`, self-registers with the proxy:
  `POST /server?mcp_name=jam-gateway&server_url=http://<node>:8080/mcp&owner=chunyu&job_id=...`
- Exposes the route `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp`
- Speaks plain JSON-RPC MCP; GET returns:
  - `Accept: text/event-stream` → SSE stream (strict clients like Claude Desktop require this)
  - `Accept: text/html` → the built-in **web console** (browser UI)
  - otherwise → plain text
- `Mcp-Session-Id` header per session; **per-session user memory** (no
  global default user — every client must connect as someone).

### The HPC chain (per user)

`connect_hpc(username, pin_otp?, password?)`:

1. `setsid ssh -M -S ~/fm-cm/gateway-<user> -fN <user>@scilogin.jlab.org`
   (auth = PIN+OTP, via SSH_ASKPASS helper in `$HOME` — /tmp is noexec)
2. `ssh -S <gw-sock> -O forward -L 127.0.0.1:<2222+crc32(user)%400>:ifarm.jlab.org:22`
3. `setsid ssh -M -S ~/fm-cm/ifarm-<user> -p <port> -fN <user>@localhost`
   (auth = ifarm password)

- Sockets live in shared `~/fm-cm/`; `ControlPersist=8h`.
- Server restarts inside the boot **reuse** the chain (`reused`, no OTP).
- Job cancellation kills the chain (cgroup) → next boot needs a fresh OTP.
- **One folder = everything**: the workspace is the folder the user cloned
  (default `/w/ceph24/JAM/<user>/fitpack_legacy`, recorded in
  `.jam_workspace`; `setup_workspace` takes a target_dir). Cards live in
  `<ws>/inputs/jam24`, runs INSIDE the clone at `<ws>/runs`. chunyu's
  ladder was migrated there (`runs/smoke4/001/...`).

### Tools (23)

| tool | what it does |
|---|---|
| `ping` | liveness |
| `get_console` | returns the web console URL (agents hand this to users) |
| `connect_hpc` | build/reuse the per-user two-hop chain (username-first, credentials only when needed) |
| `check_hpc` | chain + queue + `/w/ceph24/JAM/<user>` listing |
| `submit_test_fit` / `get_submit_result` | trivial chain test job |
| `node_diag` | shell ON THE GATEWAY NODE (hot-redeploy + diagnostics) |
| `remote_exec` | shell on ifarm through the chain |
| `list_jam_runs` | cards + run dirs + newest msr files |
| `submit_jam_fit` | real apptainer `maxlike_jam` fit (card, outdir, prior, cores, hours=10 default, partition) |
| `get_jam_result` | queue + submit log + progress.json tail + msr |
| `read_chi2` | parsed χ²/N series + per-dataset χ²/N |
| `evaluate_fit` | finished .msr vs paper band (runs inside apptainer, needs numpy) |
| `fs_list` / `fs_read` / `fs_write` | filesystem tools on ifarm (write restricted to the user's own JAM area) |
| `setup_workspace` | prepare /w/ceph24/JAM/<user> (copy or git-clone fitpack, run tree, lhapdf symlink) |
| `search_datasets` | list datasets (reaction + id) in a card, query-filterable |
| `build_card` | derive a custom card by commenting out selected dataset ids |
| `cluster_status` | cluster perception (partitions/queue/run-time hints) for resource sizing |
| `chain_state` | freeze/continue/reset judgment data (msr χ²/N, worst datasets, params, ladder context) |
| `get_dataset_catalog` | composer catalog {reaction: [datasets]} for a card |
| `submit_fit_from_selection` | composer submission: keep selected ids in a derived card and submit |

---

## 3. Daily lifecycle (the flow to understand)

```
~10:00 EDT   reservation window opens; queued jobs start
  │  gateway job RUNNING (6h TimeLimit)
  │  user: connect_hpc once (username + PIN+OTP + password)  → chain live
  │  …all day: submit fits / read chi2 / evaluate ride the chain…
~16:00 EDT   window closes → Slurm kills the job → chain dead
  │           ("Backend not found" from then on; jobs submitted now = PENDING)
next 10:00   queued job auto-starts → one fresh OTP needed again
```

Key consequences:
- **One OTP per boot** (~per day). `connect_hpc` called with username only
  returns `reused` while the chain lives; it asks for credentials only when
  dead. Never pre-ask for credentials.
- The **web console** makes this trivial for humans: fill username → click
  Connect → (fields for PIN+OTP/password appear only if needed).

---

## 4. Operating it (how-to)

### 4.1 Launch / relaunch the gateway job

```bash
cd /root/femtomind/lqcdmcp-deploy
python3 redeploy_jam.py status    # state of the jam-gateway job
python3 redeploy_jam.py launch    # cancel-free submit via dashboard API
python3 redeploy_jam.py cancel    # stop by name
```

- Uses the LQCDMCP dashboard MCP (`/jlab`), Bearer token in the script's `T`
  variable. **The dashboard token expires frequently** — when calls start
  returning `{}`/auth errors, re-login to the dashboard UI and paste a fresh
  token into `redeploy_jam.py` (also inside `hot_redeploy`-adjacent flows).
- Sessions need `validate_user` first (the script does it).
- **One job at a time** on the reservation; a second submit stays PENDING.
- Outside the window the job sits PENDING and auto-starts next morning.

### 4.2 Hot-redeploy the server code (no OTP, no job kill)

```bash
python3 hot_redeploy.py jam_gateway_v3.py
```

Uploads via `node_diag`, compiles on the node, copies over both script
names, pkills the server; the supervisor restarts it inside the same boot;
ControlMasters survive. **This is the normal edit loop.**

### 4.3 Web console (browser UI)

Open (any of):
- `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp`
- `https://seniors-loc-directive-memphis.trycloudflare.com/cloud/jam-gateway/mcp` (443 tunnel)

Sections: 1·连接 (username → Connect; OTP fields appear only if chain dead)
2·提交拟合 (card dropdown ×20, outdir, prior, hours, cores, partition) 3·输出
(JSON + 查状态/读χ²/对照 band buttons).

The 443 tunnel is a **trycloudflare quick tunnel** — the URL changes every
restart. Regenerate:
```bash
cloudflared tunnel --url https://lqcdmcp.jlab.org:8000 --no-autoupdate
```
then update `CONSOLE_PUBLIC_URL` in `jam_gateway_v3.py` and hot-redeploy.

### 4.4 Clients

| client | how | status |
|---|---|---|
| Claude Code | `claude mcp add --transport http jam-gateway <url>` | ✅ verified |
| Claude Desktop | custom connector, URL = 443 tunnel, auth **No sign-in**, preflight "Couldn't reach" → Continue anyway | ✅ verified (port 8000 direct is BLOCKED by Desktop's egress) |
| Codex CLI (ChatGPT terminal) | `codex mcp add jam-gateway --url <url>` | documented, untested |
| browser console | open URL | ✅ verified |
| scripts/SDK | POST JSON-RPC to `<url>` | ✅ verified |

### 4.5 The science loop (what agents do)

Full experiment-control loop (v4, verified live 2026-10-07):
0. (new user once) `setup_workspace` — copy/git-clone fitpack + run tree
1. `connect_hpc(username)` → reused or asks for credentials
2. `fs_list` / `fs_read` — browse workspace, read cards and logs
3. `search_datasets(card, query)` — pick reactions/datasets with the user
4. `build_card(base_card, exclude_ids, out_card)` — derive a custom card
5. `submit_jam_fit(card=out_card, ...)` — FIRED
6. poll `read_chi2` / `get_jam_result`; `evaluate_fit` vs paper band

1. `connect_hpc(username)` → `reused` or asks user for credentials
2. `list_jam_runs` → inventory (cards/runs/msr)
3. `submit_jam_fit(card=input_jam24_s7_warm.py, outdir=..., prior_msr=..., hours=…)`
   → FIRED + ifarm job id
4. poll `read_chi2(outdir)` / `get_jam_result(outdir)` every few minutes
5. `evaluate_fit(outdir)` → χ²/N vs paper band

**Reference numbers (verified):**
- warm step-7 fit (card `input_jam24_s7_warm.py`, prior
  `001/step6_v2/msr/8XW0AVCIGLJ8.msr`, 4 cores, jupyter) → **χ²/N = 1.452**
  in ~40 min — identical to the human-run ladder (step7_v2 = step7_v3 = 1.452)
- converged ladder χ²/N ≈ 1.2–1.6, systematically below the paper band
  (arXiv:2501.00665 step16a: mean 2.566, band [2.190, 2.819])
- fit totals 4613 data points; cold starts need the 10 h budget (ladder
  reference ran 10 h/step on 4 cores, jupyter partition)

---

## 5. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `Backend not found` (cloud & dashboard) | job died (window closed / 6h limit / scancel) | `redeploy_jam.py launch`; if PENDING → wait for next window |
| job PENDING | reservation window closed | wait ~10:00 EDT |
| `check_hpc` → disconnected | chain dead (new boot) | `connect_hpc` with username (+ OTP once) |
| `no username known for this session` | client never connected | connect_hpc with the user's username |
| Claude Desktop "Couldn't reach" | its egress blocks port 8000 | use the 443 tunnel URL |
| dashboard calls return `{}` | token expired / no validate_user | re-login to dashboard, refresh token in `redeploy_jam.py` |
| ssh askpass "Permission denied" | /tmp noexec | helper lives in `$HOME` (already handled) |
| fit hangs in submit | heredoc quoting | base64 transport (already handled) |
| `TERM environment variable not set` in logs | harmless login-shell warning | ignore |

---

## 6. Security (current state + plan)

- **NOW**: the `/cloud` route is **unauthenticated** — anyone with the URL
  can call tools; when the chain is live they'd act as the connected user.
  `remote_exec`/`node_diag` are arbitrary shells. Do not share the URL
  beyond trusted people until the instance key lands.
- **NEXT (10 min)**: per-instance bearer key in the gateway — one key per
  instance, passed as `Authorization: Bearer <key>` (Claude Code `--header`,
  Desktop Request headers, Codex `http_headers`, console input field).
- **Proper multi-user**: the OAuth front door (`femtomind_app.py`, invite
  codes, per-user identity, panel for credentials so OTP/password never
  enter chat). Code exists; needs an always-on host.
- **Platform asks for Jie** (see `JIE_OAUTH_ASK.md`): add MCP OAuth to the
  proxy (makes ChatGPT/Desktop connect directly) + auto-relaunch/persistent
  slot (removes the daily-window fragility before the AMSC demo).
- **Immediate**: the ifarm password was pasted into a Claude chat on
  2026-10-06 — rotate it.

---

## 7. Code inventory (`/root/femtomind/lqcdmcp-deploy/`)

| file | purpose |
|---|---|
| `jam_gateway_v5.py` | **current server** (23 tools: +cluster_status, chain_state, composer catalog/selection; web composer at `?ui=composer`) |
| `console.html` | console page source (embedded into the server at build) |
| `hot_redeploy.py` | in-boot code swap |
| `redeploy_jam.py` | launch/cancel/status via dashboard API |
| `agent_demo.py` | pure MCP client walking the expert loop |
| `fit_poller.py` | background χ² poller |
| `COLLABORATOR_GUIDE.md` | one-page quickstart for users |
| `CONNECTION_DIAGRAM.md` | topology diagram + the "why two hops" explainer |
| `V2_DEMO.md` | demo narrative + result table |
| `JIE_OAUTH_ASK.md` | spec for Jie: MCP OAuth + platform asks |
| `README.md` | deployment notes + ladder table |
| `make_launch.py`, `probe_server.py`, `official_flow.py` | earlier-stage artifacts |

Secrets: `repro-jam20/secrets.env` (ifarm password). PIN is `202610`
(6 digits) + 6-digit OTP, concatenated with no space.

---

## 8. Roadmap / open items

1. **Instance key** on the gateway (before any broad sharing).
2. **v4 full experiment control** — ✅ done (fs tools, setup_workspace, search_datasets, build_card).
3. **v5 agent perception** — ✅ done (cluster_status, chain_state, composer catalog + selection submit, web composer page).
3. **NERSC as second site** — needs the `sites` registry in
   `connect_hpc`; the network path is node→scilogin→ifarm→perlmutter
   (ifarm→NERSC:22 verified open). One small refactor.
4. **Jie asks**: MCP OAuth on the proxy; auto-relaunch / persistent slot;
   443 exposure of the platform (Desktop blocks 8000).
5. **Front door deployment** (OAuth + panels + per-user identity) on an
   always-on host — the production multi-user shape.
6. **Stable public entry** — replace the ephemeral trycloudflare tunnel with
   a fixed domain (Cloudflare named tunnel) before the AMSC demo.
