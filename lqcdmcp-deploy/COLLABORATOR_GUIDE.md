# Running JAM fits with FemtoMind — a step-by-step guide

This lets you run JAM global fits on the JLab farm (ifarm) without logging in
to the cluster yourself. You either click through a web page (the **panel**)
or describe what you want to an AI assistant (**Claude Code** or **Codex**).
The fits run as Slurm jobs under your own JLab account.

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
  login. It starts your gateway, and the panel and the AI assistant use it to
  talk to the gateway. Nobody else's token opens your gateway.
- Your **cluster sign-in** (PIN + one-time code, ifarm password) and your
  **GitHub token** are typed only on the panel. Never type them into a chat.
- Your **fits** are ordinary Slurm jobs on ifarm. They keep running when your
  gateway stops, and your files stay. You can look at the results another day.

Below, replace `<you>` with your JLab username.

---

## Part 1 — One-time setup

### 1.1 What you need

- A JLab account with:
  - JLab two-factor sign-in for `scilogin.jlab.org`: a PIN and a 6-digit
    one-time code;
  - membership in the `jam` Unix group. Your files go into
    `/w/ceph24/JAM/<you>`, and only members of `jam` can create that folder.
    (Not needed if the folder already exists. On ifarm, `id -nG` lists your
    groups.)
- Your account enabled on LQCDMCP by Jie Chen (step 1.2).
- A computer with `git` and Python 3 (macOS or Linux; on Windows, use WSL).
- Read access to two private GitHub repositories:
  `Chunyu-Hugh/femtomind-chatgpt-app` (this code) and `QCDHUB/fitpack_legacy`
  (the JAM code). Ask Chunyu if you cannot open them.
- A GitHub token that can read `QCDHUB/fitpack_legacy` (on GitHub: Settings →
  Developer settings → Personal access tokens). You paste it once, when you
  create your workspace (step 2.3).

### 1.2 Ask Jie Chen to enable your account (before your first deploy)

Jie Chen runs the LQCDMCP platform. Before you start a gateway for the first
time, ask him to enable your JLab account for:

- MCP servers, and
- the gateway reservation `chen_mcp_daily` (partition `24s`).

Without this, step 2.1 stops with "the LQCDMCP platform could not start your
gateway". Trying again, or from another computer, gives the same result. Only
Jie can fix it.

### 1.3 Get the code

```bash
git clone https://github.com/Chunyu-Hugh/femtomind-chatgpt-app.git
cd femtomind-chatgpt-app/lqcdmcp-deploy
```

The repository is private. If git asks for a password, paste a GitHub token,
not your GitHub password.

All commands below are run from this `lqcdmcp-deploy` folder. To get updates
later, run `git pull` here before you start your gateway.

### 1.4 Get your dashboard token

1. Open <https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/> and sign in
   with your JLab account (Globus).
2. The page shows your access token. Press **Copy**.

Keep it private, like a password. You can paste it with or without a leading
`Bearer `. It expires after a while. Then sign in again and copy the new one.

### 1.5 Tell your terminal who you are

In each new terminal you use for this, run:

```bash
export LQCDMCP_TOKEN='paste-your-dashboard-token-here'
export JAM_GW_USER=<you>
```

You can put the `JAM_GW_USER` line in `~/.bashrc` (or `~/.zshrc` on macOS) so
it is always set. Export the token again in each new terminal: it changes each
time you sign in to the dashboard.

---

## Part 2 — Every day you use it

The platform runs gateways in a daily window, about **10:00–16:00 US
Eastern**. Your files and results on ifarm stay between days, and fits keep
running after the window closes.

> **One gateway at a time.** So far the platform's reservation has run only
> one gateway job at a time. If a colleague's gateway is running, yours waits
> in the queue (PENDING) until theirs is stopped; `deploy_and_verify.py` tells
> you when this happens. Agree on turns, and always do step 2.4 when you
> finish.

### 2.1 Start your gateway

```bash
python3 deploy_and_verify.py
```

It starts your gateway, waits for it (up to 10 minutes) and checks it.
Running it again is safe: while your gateway job is queued or running it
starts no second one, and if your gateway is up it only checks it.

It succeeds when the last line is `DONE` and you see:

```
2. reachable through the proxy: yes ...
   jam-gateway-<you> (owner <you>) IS in the list -> the gateway accepts this token ...
   no token     -> refused (good)
   your token   -> accepted (good)
```

Before you sign in (step 2.2), the report says `cluster chain: not open yet`.
That is expected.

If it stops early, see [Troubleshooting](#troubleshooting).

### 2.2 Open the panel and sign in to the cluster

1. Open your gateway's address in your browser:
   **`https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp`**
   (the last lines of the report show it too).
2. It asks for a token. Paste your dashboard token and press **Use this
   token**. The panel keeps it in this browser tab only and forgets it when
   you close the tab; a new tab asks again.
3. On the **Connect** tab, fill in:
   - **1 · scilogin.jlab.org** — your PIN immediately followed by the 6-digit
     code, no space (PIN `1234`, code `567890` → `1234567890`);
   - **2 · ifarm.jlab.org** — your JLab password;
   - press **Connect**.
4. The badge at the top right turns green: **HPC connected**. The panel moves
   to the **Project** tab.

The sign-in lasts as long as your gateway runs (at most until the daily
window closes), for the panel and the AI assistant alike. If scilogin is
still open from an earlier try (for example after a wrong password), the
panel hides the first field and asks only for the ifarm password.

### 2.3 The first time only: create your workspace

On the **Project** tab:

1. Press **Set up a new workspace…**.
2. **Folder name** — the folder created in `/w/ceph24/JAM/<you>/` (letters,
   digits, `.`, `_` and `-`). The field starts with `fitpack_legacy`. A
   folder that already exists is **not** cloned again: it becomes your
   workspace as it is. So pick a new name, such as `fitpack_ai`, unless that
   folder is already a checkout of the `ai` branch.
3. **Branch** — leave it empty (the `ai` branch, which runs campaigns).
4. **GitHub token** — paste your GitHub token (step 1.1). It is used for this
   clone only and is not stored.
5. Press **Create and clone**. Cloning can take a few minutes.

It clones the JAM code, links the shared interpolation grids, and checks
everything. The Project tab then lists these items, each marked ✓ or ✕:
JAM code (fitpack_legacy), fitlib, qcdlib, obslib, experimental data,
interpolation grids, campaign template (examples/input02.py), campaign runner
(fitlib/ai.py, FitPack ai branch).

A ✕ comes with a hint:

- ✕ on the campaign template or the campaign runner: the checkout is not on
  the `ai` branch. Press **Switch to the ai branch**, or create a new
  workspace under a new folder name. (Switching changes the branch of that
  folder: if you use it for other work, create a new workspace instead. If
  the switch is refused, for example because the folder has uncommitted
  changes, the panel says why; then create a new workspace.)
- ✕ on the interpolation grids: press **Use selected** (your workspace is
  already selected) to link the shared grids again.

Already have a checkout of the `ai` branch in `/w/ceph24/JAM/<you>/`? The
Project tab lists the checkouts it finds there: select yours and press
**Use selected** instead of creating a new one.

### 2.4 When you are done

```bash
python3 redeploy_jam.py cancel
```

This stops your gateway and frees the slot for the next person. Your
workspace, your results and any fits still running are not affected.

---

## Part 3 — The panel

The panel is your gateway's address opened in a browser:
`https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp`. It has five
tabs: Connect, Project, Campaign, Collections, Analysis. Add `?tab=campaign`
(or `connect`, `project`, `collections`, `analysis`) to the address to open
a tab directly. Old links ending in `?ui=panel` still work.

### 3.1 Campaign: build and submit a fit

The **Campaign** tab walks through five steps, like the FemtoMind JAM app.
Reactions and datasets are never chosen for you. Use **Next** and **Back**,
or click a step.

1. **Non-perturbative object** — tick *Unpolarized PDF* (the others are
   marked BLUEPRINT: not available yet). The parameter table is read from
   your workspace's `examples/input02.py` (the library; 39 rows on the
   current `ai` branch): block, parameter, FREE or FIXED, value, min, max.
   - Click a status to switch it between FREE and FIXED. You can edit each
     row's value, and the min and max of FREE rows.
   - Only the rows you change are written into the input card. Every other
     row stays exactly as the library has it.
   - A parameter you switch to FREE needs a min and a max. Where the library
     gives none, its row says "set a min and max" until you fill in both.
   - **Reset to library values** undoes all your changes.
2. **Reactions** — tick *DIS* and/or *Drell-Yan* (the others are BLUEPRINT).
   Each reaction has two sub-tabs:
   - **Datasets**: grouped by experiment, with a search box, **Select all**
     and **Clear all**. DIS datasets without a data file in your workspace
     are greyed out (NO DATA FILE). For Drell-Yan, only E866 NuSea pp
     (10001) can be selected for now; the others are marked BLUEPRINT.
   - **Cuts**: the kinematic cuts written into the card. Edit, add or remove
     them; at least one must stay enabled.
3. **QCD settings** — order, Q₀², TMC, DGLAP mode, αS mode, higher twist.
4. **Job config** — campaign name (required), number of replicas, parameter
   initialization, inference mode, tolerance, max iterations, optimizer, and
   the Slurm resources **per replica**: wall clock, memory, CPUs and
   partition. Partition **Auto** picks `priority` when the wall clock is up
   to 4 h and `production` when it is longer; you can also pick one.
5. **Review** — press **Create Input Card**. It checks everything on the
   cluster and shows the summary (including which parameters you changed)
   and the Slurm job array script (click **Slurm job array script** to open
   it). Read them, then press **Submit Jobs**. Problems are listed under the
   buttons, for example `Parameter g1 N is FREE: give it a min and a max.`
   If you change anything afterwards, press **Create Input Card** again.

After submitting, the tab shows the campaign's collection ID and Slurm job
number, with an **Open Collections** link. **New campaign** starts a fresh
one.

Settings start at these values; keep or change each one:

| Setting | Starts at | Other choices |
|---|---|---|
| DIS cuts | Q² > 1.69, W² > 10 | edit, add, remove |
| Drell-Yan cut | Q² > 0 | edit, add, remove |
| Order | NLO | LO, NNLO |
| Q₀² | 1.6129 GeV² | any positive number |
| TMC | AOT | off |
| Higher twist | off | on |
| DGLAP mode | truncated | iterated |
| αS mode | backward | forward |
| Parameter initialization | the table's starting values | random within each FREE parameter's min and max (seed: auto or yours; a different seed per replica unless you untick it) |
| Inference mode | ensemble (pseudo-data replicas) | the original central values of the data |
| Replicas | 10 | 1 to 2000 |
| Max iterations | 500 | any positive whole number |
| Tolerance | 1e-6 | any positive number |
| Optimizer | trf | dogbox |
| Slurm, per replica | 00:30:00, 4G, 1 CPU, partition Auto | wall clock as HH:MM:SS, memory like 4G or 500M, 1 to 64 CPUs, a partition from the list |

### 3.2 Collections: follow, stop or delete

- The table lists your campaigns, newest first: name, creation time (US
  Eastern), replicas done (and failed), status (submitted, running,
  completed, postprocessed or cancelled) and the Slurm job numbers.
  **Refresh** reloads it.
- **Analyze** opens the campaign on the Analysis tab.
- **Stop** appears while the campaign has tasks queued or running. Confirm
  with **Stop jobs**. Replicas that already finished keep their results.
- **Delete** removes the campaign from Collections and deletes its folder
  with all its results. Type the confirmation phrase shown and press
  **Delete**. It is refused while tasks are still in the queue: stop the
  campaign first.
- The **Slurm queue** card lists all your jobs on ifarm: campaign replicas
  and any other job you submitted.

### 3.3 Analysis: look at the results

Choose the campaign at the top, then:

- **Postprocess** — collects the finished replicas into the campaign's fit
  summary. Press it again when more replicas have finished; Load Fit Summary
  tells you when the summary is out of date.
- **Load Fit Summary** — replicas completed / failed / missing, χ²
  statistics and the best replica, then a table of χ²/N per dataset, worst
  first. It is each replica's χ² for the data it was fitted to (the
  pseudo-data, in the ensemble mode), averaged over the replicas and divided
  by the number of points. **By experiment** (below) compares the best
  replica with the central data values instead.
- **View PDF Plots** — pick flavors (u, d, g and s are on; ub, db and sb can
  be added) and Q² (starts at 10 GeV²), then press **Draw**. You get
  x f(x, Q²) of every finished replica, with the mean and 68% band when that
  box is ticked.
- **By experiment** — list the experiments to plot (for example
  `SLAC, BCDMS`; empty = all) and press **Analyze**. You get a table of χ²/N
  for every experiment, for the best replica against the central data
  values, and data-vs-theory plots for the experiments you listed (up to 12
  datasets in one image).
- **Generate More Replicas** — enter how many, press **Plan**, read the
  plan, type the confirmation phrase and press **Launch**. The new replicas
  use the same input card.

Plots and the analysis by experiment run on the cluster and take a while.

---

## Part 4 — Use an AI assistant (Claude Code or Codex)

The assistant talks to the same gateway as the panel and can do everything
the panel does, one step at a time in conversation. It asks the same
questions as the Campaign tab, shows you the input-card summary and the Slurm
script, and submits only after you say yes. Submitting, adding replicas,
stopping and deleting each need your explicit confirmation. The assistant
itself may also ask you to approve each tool call.

Do steps 2.1 and 2.2 first: the cluster sign-in always happens on the panel.
If the assistant asks for your PIN, password or GitHub token in chat, do not
type them: open the panel instead.

### 4.1 Claude Code

1. Install Claude Code. You need a Claude Pro, Max, Team or Enterprise plan,
   or a Claude Console account
   (<https://code.claude.com/docs/en/quickstart>). On macOS or Linux:
   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ```
   Then run `claude` once to sign in, and quit it.
2. Register your gateway once, in a terminal where step 1.5 is done:
   ```bash
   claude mcp add --transport http --scope user jam-gateway "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-$JAM_GW_USER/mcp" --header 'Authorization: Bearer ${LQCDMCP_TOKEN}'
   ```
   Copy it exactly. The double quotes fill in your username now (check with
   `claude mcp list` that the address ends in `jam-gateway-<you>/mcp`). The
   single quotes keep `${LQCDMCP_TOKEN}` as written, so Claude Code reads
   your current token each time it starts.
   If you registered `jam-gateway` with an earlier version of this guide,
   remove that entry first: `claude mcp remove jam-gateway -s user`.
3. Each time, start Claude Code from a terminal where `LQCDMCP_TOKEN` is
   exported (step 1.5):
   ```bash
   export MCP_TOOL_TIMEOUT=900000
   export CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=900000
   claude
   ```
   The two timeout lines matter. By default, Claude Code waits only 60
   seconds for an answer from a server like this one, and cloning, PDF plots
   and the analysis by experiment can take up to 15 minutes. These lines
   raise its limits to 15 minutes (the values are in milliseconds). You can
   put them in `~/.bashrc` or `~/.zshrc`.
4. After a new token: export it again and restart `claude`. Nothing needs to
   be registered again.

To check: `claude mcp list` shows `jam-gateway`, but it says `✔ Connected`
even when the token is wrong or expired. The real check is to ask Claude:
*"Check my cluster connection."* If the token is not exported,
`claude mcp list` prints:

```
[Warning] [jam-gateway] mcpServers.jam-gateway: Missing environment variables: LQCDMCP_TOKEN
```

### 4.2 Codex (OpenAI's terminal assistant, with your ChatGPT account)

1. Install Codex (no Node.js needed):
   ```bash
   curl -fsSL https://chatgpt.com/codex/install.sh | sh
   ```
   (or `npm install -g @openai/codex`, or `brew install --cask codex`).
   Then run `codex` and choose **Sign in with ChatGPT**.
2. Register your gateway once: add these lines to `~/.codex/config.toml`
   (create the file if it does not exist), with your username in place of
   `<you>`:
   ```toml
   [mcp_servers.jam-gateway]
   url = "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp"
   bearer_token_env_var = "LQCDMCP_TOKEN"
   tool_timeout_sec = 900
   ```
   `tool_timeout_sec = 900` lets a step run for up to 15 minutes. Codex's
   default is 60 seconds, and cloning, plots and the analysis take longer.
   If your file already has a `jam-gateway` entry from an earlier version of
   this guide, add the `tool_timeout_sec` line to it.
3. Start `codex` from a terminal where `LQCDMCP_TOKEN` is exported
   (step 1.5). Codex reads the token once, when it starts. After a new token:
   export it, quit Codex and start it again; `codex resume --last` continues
   the conversation.

To check: `codex mcp list` only reads the configuration file; it shows the
entry even without a token. The real check is to ask: *"Check my cluster
connection."* If the token is not exported, Codex reports
`Environment variable LQCDMCP_TOKEN for MCP server 'jam-gateway' is not set`.

### 4.3 What to say

- *"Check my cluster connection."* — ask this first: it proves the assistant
  reaches your gateway with a valid token.
- *"Help me run a JAM fit."* — it asks, one step at a time, for the same
  choices as the Campaign tab, shows you the input-card summary and the
  Slurm script, and submits only after you say yes.
- *"Fit DIS with SLAC and BCDMS proton F2, Q² > 2, 10 replicas, otherwise
  the library values."* — you can also give everything at once.
- *"How is my campaign doing?"* · *"What is in my Slurm queue?"* ·
  *"Postprocess it and show the fit summary."* · *"Plot the u and d PDFs at
  Q² = 10."* · *"Which experiment fits worst? Show data vs theory for NMC."*
- *"Stop my campaign."* · *"Delete the test campaign."* — it tells you what
  will happen and asks you to confirm.
- *"Show me the log of a failed replica."* — it reads the log on the cluster.
- *"Write a Slurm job that …"* — it drafts the script with you, checks it
  with `sbatch --test-only`, and submits only after you confirm.

### 4.4 ChatGPT in the browser

Not available: the ChatGPT website needs a sign-in flow that the LQCDMCP
platform does not offer. Use Codex (4.2) with your ChatGPT account instead.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `deploy_and_verify.py` stops with "the LQCDMCP platform could not start your gateway", after lines such as `validate: {}` and `Slurm command failed: sudo sbatch --uid <you> ... (rc=1)` | Your account is not enabled on LQCDMCP yet (step 1.2). Send the report to Jie Chen. Retrying, or using another computer, does not help. |
| "the dashboard refused this token", or a Python error ending in `HTTP Error 401` or `HTTP Error 403` | The dashboard token expired or was copied incompletely. Sign in to the dashboard again, copy the token, export it (1.5) and run the command again. Ctrl-C stops a script that is still waiting. |
| "your gateway job is waiting in the queue" | Another gateway holds the reservation, or the daily window has not opened yet. Yours starts by itself: run `python3 deploy_and_verify.py --check` later, or ask your colleagues whether one of them can stop theirs. |
| "the gateway is not up yet: its job runs but the gateway does not answer" | Wait a few minutes, then run `python3 deploy_and_verify.py --check`. If it still does not answer: `python3 redeploy_jam.py cancel`, then `python3 deploy_and_verify.py`. |
| "the gateway is not up yet, and the job launched above is no longer queued or running" | Gateway jobs end when the daily window closes (about 16:00 US Eastern): start again the next day. Earlier in the day, send the report to Chunyu. |
| "the gateway is not up yet: you have no gateway job queued or running" | Run `python3 deploy_and_verify.py` without `--check`. |
| `jam-gateway-<you> (owner <you>) is NOT in the list` | Your gateway is not running (do step 2.1), or the token comes from another account's dashboard login. |
| "the dashboard does not confirm this token as <you>'s" | The end of the line says why: the token is another account's or `JAM_GW_USER` is not your JLab username; MCP servers are not enabled for you (step 1.2); or the dashboard answered with an error (sign in again and copy a new token). |
| "the gateway does not accept this token", `unauthorized`, or the panel says "The gateway did not accept this token." | The token expired or belongs to another account. Sign in to the dashboard again and copy the token. Then `export LQCDMCP_TOKEN='...'` and restart Claude Code or Codex. On the panel, paste it again (the **Token** button at the top right). |
| "the running gateway is an older version than the code in this folder" | You updated this folder (for example with `git pull`) after starting your gateway. Run `python3 hot_redeploy.py` (keeps your cluster sign-in), then `python3 deploy_and_verify.py --check`; or start over: `python3 redeploy_jam.py cancel`, then `python3 deploy_and_verify.py`. |
| "the gateway answers without a token" | Stop it at once with `python3 redeploy_jam.py cancel` and send the report to Chunyu. |
| Connect fails at scilogin | Type the PIN and the code together, no space. Use a fresh code. |
| Connect fails at ifarm | Check your JLab password. scilogin stays open, so the next try asks only for the password. |
| Creating the workspace says the repository needs a GitHub token | Paste a GitHub token that can read `QCDHUB/fitpack_legacy` and press **Create and clone** again. |
| Creating the workspace fails with `mkdir: cannot create directory` … `Permission denied` | Your account is not in the `jam` group, so `/w/ceph24/JAM/<you>` cannot be created (1.1). |
| Project tab: ✕ on the campaign template or the campaign runner | Press **Switch to the ai branch**, or create a new workspace under a new folder name (2.3). |
| Project tab: ✕ on the interpolation grids | Press **Use selected** with your workspace selected. |
| Campaign tab: "Sign in on the Connect tab and choose a workspace first" | Do steps 2.2 and 2.3 (the line below it says what is missing), then open the Campaign tab again. |
| Delete is refused: "This campaign still has … task(s) in the Slurm queue" | Press **Stop** first, then **Delete**. |
| "Could not read the Slurm queue" | Slurm did not answer. Press **Refresh** in a minute; Stop and Delete work again once the queue can be read. |
| The assistant says "not connected" or "no live chain" | Sign in on the panel's Connect tab (2.2), then ask again. |
| Claude Code: `Missing environment variables: LQCDMCP_TOKEN` | Export the token (1.5) and restart `claude`. |
| Codex: `Environment variable LQCDMCP_TOKEN for MCP server 'jam-gateway' is not set` | Export the token (1.5) and restart `codex`. |
| A step times out in Claude Code or Codex | Claude Code: export the two timeout lines of 4.1 before starting `claude`. Codex: `tool_timeout_sec = 900` in `~/.codex/config.toml` (4.2). |

---

## Reference

- Your gateway: `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp`.
  In a browser, the same address is the panel.
- The dashboard: <https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/>.
- Your files on ifarm: `/w/ceph24/JAM/<you>/`. Your workspace is the folder
  you created there.
- A campaign is in `<workspace>/campaigns/<name>_<YYYYMMDD_HHMMSS>/` (the time
  in the folder name is UTC; Collections shows US Eastern):
  - the input card: `stages/pdf_stage/input.py` (your workspace's
    `examples/input02.py`, with your choices appended at the end);
  - each replica's log: `stages/pdf_stage/logs/stage_<job>_<replica>.out`;
  - each replica's results: `stages/pdf_stage/current/replica_<n>/`;
  - the fit summary: `stages/pdf_stage/current/fit_summary.json`;
  - plots: `analysis/pdf_plots/` and `analysis/by_experiment/`.
- Other Slurm jobs written with the assistant: `<workspace>/slurm/`.
- `python3 redeploy_jam.py status` — is your gateway running?
- `python3 check_dashboard_token.py` — what the dashboard says about your
  token and your gateway.
- `python3 redeploy_jam.py script` — writes `jam-gateway-<you>.slurm`, if you
  prefer to start the gateway from the dashboard page: there, start an MCP
  server named `jam-gateway-<you>` with that file as its submission script.
- VS Code (GitHub Copilot, agent mode) — **untested**, taken from the VS Code
  documentation. Run **MCP: Open User Configuration** from the Command
  Palette and add (merge it with anything already in the file):
  ```json
  {"inputs": [{"type": "promptString", "id": "lqcdmcp-token", "description": "LQCDMCP dashboard token", "password": true}],
   "servers": {"jamGateway": {"type": "http",
     "url": "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp",
     "headers": {"Authorization": "Bearer ${input:lqcdmcp-token}"}}}}
  ```
- Claude Desktop — **untested**. It works only through the `mcp-remote`
  bridge, which needs Node.js 18 or newer, with the token pasted into
  `claude_desktop_config.json`:
  ```json
  {"mcpServers": {"jam-gateway": {"command": "npx",
    "args": ["-y", "mcp-remote", "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp", "--header", "Authorization:${AUTH_HEADER}"],
    "env": {"AUTH_HEADER": "Bearer <paste-your-token>"}}}}
  ```
  Quit Claude Desktop completely and reopen it after each change, including
  each new token.
- How it works inside, for maintainers: [README.md](README.md).
