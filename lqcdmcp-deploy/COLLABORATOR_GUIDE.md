# Running JAM fits with FemtoMind — a step-by-step guide

This lets you run JAM global fits on the JLab farm (ifarm) without logging in
to the cluster yourself. You either click through a web page (the **panel**)
or describe what you want to an AI assistant (**Claude Code** or **Codex**),
and the fits are submitted to Slurm under your own JLab account.

How it fits together:

```
your browser (panel)  ─┐
Claude Code / Codex  ──┼──>  your gateway  ──>  ifarm: your workspace, Slurm jobs
                       │     (a small server you start on the JLab LQCD cluster,
                       │      through Jie Chen's LQCDMCP platform)
```

- The **gateway** is yours alone. You start it at the beginning of the day and
  stop it when you are done.
- The **dashboard token** is your key. It comes from the LQCDMCP dashboard
  login, it starts your gateway, and it is what the panel and the AI assistant
  use to talk to it. Nobody else's token opens your gateway.
- Your **cluster sign-in** (PIN + one-time code, ifarm password) is typed only
  on the panel. Never type it into a chat.

Below, replace `<you>` with your JLab username.

---

## Part 1 — One-time setup

### 1.1 What you need

- A JLab account that can use the LQCDMCP dashboard
  (<https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/>). If the dashboard
  does not let you start MCP servers, ask Jie Chen to enable your account.
- A computer with `git` and Python 3 (macOS or Linux; on Windows use WSL).
- Read access to the GitHub repositories `Chunyu-Hugh/femtomind-chatgpt-app`
  (this code) and `QCDHUB/fitpack_legacy` (the JAM code). Ask Chunyu if you
  cannot open them.

### 1.2 Get the code

```bash
git clone https://github.com/Chunyu-Hugh/femtomind-chatgpt-app.git
cd femtomind-chatgpt-app/lqcdmcp-deploy
```

All commands below are run from this `lqcdmcp-deploy` folder.

### 1.3 Get your dashboard token

1. Open <https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/> and sign in
   with your JLab account (Globus).
2. Copy the access token the dashboard gives you after sign-in.

Keep it private (like a password). It expires after a while; when commands
start saying the token is not accepted, sign in again and copy the new one.

### 1.4 Tell your terminal who you are

Each time you open a new terminal for this, run:

```bash
export LQCDMCP_TOKEN='paste-your-dashboard-token-here'
export JAM_GW_USER=<you>
```

(You can put these two lines in `~/.bashrc` or `~/.zshrc` so you never have to
retype them; update the token there when it expires.)

---

## Part 2 — Every day you use it

The platform runs gateways in a daily window, roughly **10:00–16:00 US
Eastern**. Your files and results on ifarm stay between days.

> **One gateway at a time.** The platform currently runs one gateway job at a
> time. If a colleague's gateway is running, yours waits in the queue until
> theirs stops — agree on turns, and always do step 2.4 when you finish.

### 2.1 Start your gateway

```bash
python3 deploy_and_verify.py
```

This takes 1–10 minutes. It starts the gateway, waits for it, and checks it.
It succeeds when the last line is `DONE` and you see:

```
2. reachable through the proxy: yes ...
   ... jam-gateway-<you> IS in the list -> the gateway accepts this token
   no token     -> refused (good)
   your token   -> accepted (good)
```

If it stops early, see [Troubleshooting](#troubleshooting).

### 2.2 Open the panel and sign in to the cluster

1. Open **`https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp?ui=panel`**
   in your browser.
2. The first time, it asks for a token: paste your dashboard token and press
   **Use this token** (the browser remembers it).
3. On the **Connect** tab, fill in:
   - **1 · scilogin.jlab.org** — your PIN immediately followed by the 6-digit
     code from your authenticator, no space (PIN `1234`, code `567890` →
     `1234567890`);
   - **2 · ifarm.jlab.org** — your JLab password;
   - press **Connect**.
4. The badge at the top right turns green: **HPC connected**. This sign-in
   lasts for the rest of the window, for the panel and the AI alike.

### 2.3 The first time only: create your workspace

On the **Project** tab, press **Set up a new workspace…**, choose a folder
name (e.g. `fitpack_legacy`), paste a GitHub token with read access to
`QCDHUB/fitpack_legacy` if it asks for one, and press **Create and clone**. It
clones the JAM code (the `ai` branch), links the shared interpolation grids,
and checks everything. Afterwards the Project tab shows a list of ✓ items.

(The GitHub token is typed on the panel only, used for this clone, and not
stored.)

### 2.4 When you are done

```bash
python3 redeploy_jam.py cancel
```

This frees the slot for the next person. Your workspace and results stay.

---

## Part 3 — Run a fit from the panel

The **Campaign** tab walks through five steps, like the FemtoMind JAM app.
Settings start at the values in the JAM library; reactions and datasets are
never chosen for you.

1. **Non-perturbative object** — tick *Unpolarized PDF* (the others are not
   available yet). The table shows every parameter: FREE or FIXED, starting
   value, min and max. Click a status to switch it; edit values and ranges.
2. **Reactions** — tick *DIS* and/or *Drell-Yan*, then tick the datasets
   (grouped by experiment, with search). The **Cuts** sub-tab holds the
   kinematic cuts (DIS: Q² > 1.69, W² > 10).
3. **QCD settings** — order, Q₀², TMC, higher twist, DGLAP mode, αS mode.
4. **Job config** — campaign name, number of replicas, parameter
   initialization, inference (pseudo-data ensemble or central values),
   tolerance, max iterations, optimizer, and the Slurm resources per replica.
   The partition is chosen for you: `priority` for jobs up to 4 h,
   `production` for longer ones.
5. **Review** — press **Create Input Card** (checks everything on the
   cluster), read the summary and the Slurm script, then **Submit Jobs**.

Then:

- **Collections** lists your campaigns and how many replicas have finished.
- **Analysis**: choose the campaign, then
  - **Postprocess** once replicas have finished (collects them),
  - **Load Fit Summary** — χ² statistics, best replica, χ²/N per dataset,
  - **View PDF Plots** — pick flavors and Q²,
  - **By experiment** — χ²/N per experiment and data-vs-theory plots,
  - **Generate More Replicas** — add replicas; you confirm with a phrase.

---

## Part 4 — Use an AI assistant (Claude Code or Codex)

The assistant reaches the same gateway as the panel: it can do everything the
panel does, step by step in conversation. Do steps 2.1 and 2.2 first (the
cluster sign-in always happens on the panel).

### 4.1 Claude Code

1. Install Claude Code and sign in with your Claude account
   (see <https://docs.claude.com/en/docs/claude-code>).
2. Register your gateway once (in a terminal where `LQCDMCP_TOKEN` is set):
   ```bash
   claude mcp add --transport http --scope user jam-gateway \
     https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp \
     --header "Authorization: Bearer $LQCDMCP_TOKEN"
   claude mcp list          # expect: jam-gateway ... ✔ Connected
   ```
   This stores the current token. When you get a new token, run
   `claude mcp remove jam-gateway -s user` and add it again.
3. Start `claude` in any folder and talk to it (examples in 4.3).

### 4.2 Codex (OpenAI's terminal assistant, with your ChatGPT account)

1. Install Codex and sign in: `npm install -g @openai/codex`, then run
   `codex` and choose *Sign in with ChatGPT*.
2. Register your gateway once: add these lines to `~/.codex/config.toml`
   (create the file if it does not exist):
   ```toml
   [mcp_servers.jam-gateway]
   url = "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp"
   bearer_token_env_var = "LQCDMCP_TOKEN"
   ```
   Codex reads the token from `LQCDMCP_TOKEN`, so start it from a terminal
   where that is set (step 1.4); nothing to re-register when the token
   changes.
3. Check with `codex mcp list`, then run `codex` and talk to it.

### 4.3 What to say

- *"Help me run a JAM fit."* — it asks, one step at a time, for the same
  choices as the Campaign tab, shows you the input-card summary and the Slurm
  script, and submits only after you say yes.
- *"Fit DIS with SLAC and BCDMS proton F2, Q² > 2, 10 replicas, otherwise the
  library values."* — you can also give everything at once.
- *"How is my campaign doing?"* · *"Postprocess it and show the fit
  summary."* · *"Plot the u and d PDFs at Q² = 10."* · *"Which experiment
  fits worst? Show data vs theory for NMC."*
- *"Write a Slurm job that …"* — it drafts the script with you, checks it
  with `sbatch --test-only`, and submits only after you confirm.

If it asks for your PIN, password or GitHub token in chat, do not type them:
open the panel instead.

### 4.4 ChatGPT in the browser

Not available yet: the ChatGPT website needs a sign-in flow the LQCDMCP
platform does not offer so far. Use Codex (4.2) with your ChatGPT account
instead.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `deploy_and_verify.py` waits, then says "the gateway is not up yet" | Another gateway probably holds the slot. Check with `python3 redeploy_jam.py status`; run `python3 deploy_and_verify.py --check` again later, or ask whoever is running theirs. Outside 10:00–16:00 ET nothing starts. |
| "the gateway does not accept this token", or `unauthorized` | The token expired or belongs to another account. Sign in to the dashboard again, `export LQCDMCP_TOKEN=...` with the new token, and update Claude Code (4.1 step 2). |
| `jam-gateway-<you> is NOT in the list` | Your gateway is not running: do step 2.1. |
| Connect fails at scilogin | PIN and code typed together, no space; the code must be fresh (it changes every 30 s). |
| Connect fails at ifarm | Check your JLab password. |
| Project tab: an item marked ✕ | Follow the hint under it; a new workspace (2.3) fixes most. |
| The AI says "not connected" | Sign in on the panel's Connect tab (2.2), then ask again. |
| Claude Code: `jam-gateway ✘ Failed to connect` | The gateway is not running, or the stored token expired (4.1 step 2). |

---

## Reference

- Your gateway: `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp`;
  the panel is the same address with `?ui=panel`.
- Your files on ifarm: `/w/ceph24/JAM/<you>/`. A campaign's results are in
  `<workspace>/campaigns/<name>_<date>/`.
- `python3 redeploy_jam.py status` — is your gateway running?
- `python3 redeploy_jam.py script` — writes the gateway's job script, if you
  prefer to start it from the dashboard page (start an MCP server named
  `jam-gateway-<you>` with that file).
- VS Code (Copilot agent mode), `.vscode/mcp.json`:
  ```json
  {"inputs": [{"type": "promptString", "id": "jam-token", "description": "LQCDMCP dashboard token", "password": true}],
   "servers": {"jam-gateway": {"type": "http",
     "url": "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp",
     "headers": {"Authorization": "Bearer ${input:jam-token}"}}}}
  ```
- Claude Desktop, or any client that cannot send a header: use
  `npx -y mcp-remote <gateway URL> --header "Authorization:Bearer <token>"`.
- How it works inside, for maintainers: [README.md](README.md).
