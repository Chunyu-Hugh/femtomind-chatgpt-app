# FemtoMind JAM Gateway — Collaborator Quickstart

Your own MCP server on the JLab LQCD cluster that runs real JAM fits on
ifarm through the two-hop chain `node → scilogin.jlab.org (PIN+OTP) → ifarm
(password)`. You launch it yourself on Jie Chen's LQCDMCP platform; it runs
under your account and only you use it. You drive it from a browser page, or
from an AI client of your choice — they all reach the same tools and panel.

Below, `<you>` is your JLab username and your gateway's URL is
`https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp`.

## Before you start

- **The code.** Clone this repository and work in `lqcdmcp-deploy/`.
- **Dashboard access.** Sign in at `https://lqcdmcp.jlab.org:8000/jlab/lqcd/mcp/dashboard/`
  (Globus, your JLab account) and copy the dashboard token it shows. If the
  dashboard does not let you launch MCP servers, ask Jie Chen to enable your
  account.
- **One gateway at a time.** The daily reservation runs one gateway job at a
  time: while a colleague's gateway is up, yours waits in the queue until it
  stops. Agree on turns (or ask Jie for more slots).

## Launch your gateway

```bash
cd lqcdmcp-deploy
export LQCDMCP_TOKEN='...'        # your dashboard token; keep it private
export JAM_GW_USER=<you>          # your JLab username
python3 deploy_and_verify.py      # launches, waits, and checks everything
```

It prints a report ending in `DONE`: the gateway is reachable, the dashboard
recognises your token, a call without a token is refused and yours is
accepted. Then open the panel (below), paste the same token, and sign in to
the cluster on the Connect tab; `python3 deploy_and_verify.py --check` then
also reports your workspace. Stop the gateway with
`python3 redeploy_jam.py cancel` when you are done, so the next person's
gateway can start.

Or launch it from the dashboard page: `JAM_GW_USER=<you> python3 redeploy_jam.py
script` writes `jam-gateway-<you>.slurm`; launch an MCP server named
`jam-gateway-<you>` with that file as its submission script (it holds no
credential), then run `deploy_and_verify.py --check`.

The same dashboard token opens your gateway: every client below sends it. The
gateway asks the dashboard which MCP servers the token's owner launched and
lets the call in only when that list includes this gateway, so nobody else's
token works. (The proxy in front forwards every request, so this check is what
keeps your open cluster session to yourself.) Keep the token private. When the
dashboard login expires, log in again and update it in your clients.

The job runs on the daily reservation, roughly **10:00–16:00 US Eastern**;
launch again the next day. Your workspace and runs live under
`/w/ceph24/JAM/<you>/` and survive between windows.

Your cluster credentials — PIN + one-time code and ifarm password — are typed
only on your gateway's own page. No AI client ever needs them; if one asks in
chat, open the panel instead.

## The panel (any browser)

Open `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp?ui=panel`, paste
your dashboard token once (it stays in that browser), then work left to right:

| Tab | What you do there |
|---|---|
| Connect | Sign in to the cluster: PIN + one-time code, then ifarm password. Good for the rest of the window. |
| Project | Check your workspace (code, data, grids), switch between checkouts, or clone a fresh one (FitPack `ai` branch; a private repository asks for a GitHub token here, never in chat). |
| Campaign | The fit composer: Non-perturbative object (parameter table: FREE/FIXED, value, range) → Reactions (datasets by experiment, cuts) → QCD settings → Job config (replicas, initialization, inference, tolerance, iterations, Slurm resources) → Review: Create Input Card, then Submit Jobs. |
| Collections | Your campaigns with how many replicas have finished, and single runs and the queue. |
| Analysis | Postprocess, Load Fit Summary, View PDF Plots, By experiment (χ²/N and data vs theory), Generate More Replicas (with a confirmation phrase). |

## AI clients

Each one sends your token as a header. Ask it to "open the FemtoMind panel" or
just describe the fit you want.

**Claude Code** (terminal)

```bash
claude mcp add --transport http --scope user jam-gateway \
  https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp \
  --header "Authorization: Bearer $LQCDMCP_TOKEN"
claude mcp list        # expect: jam-gateway ✔ Connected
```

Claude Code cannot show the panel inside the terminal; it hands you the link.

**VS Code** (Copilot agent mode) — `.vscode/mcp.json`. VS Code asks for the
token once and keeps it in its secret store. The panel opens inline.

```json
{
  "inputs": [{"type": "promptString", "id": "jam-gateway-token",
              "description": "LQCDMCP dashboard token", "password": true}],
  "servers": {
    "jam-gateway": {
      "type": "http",
      "url": "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp",
      "headers": {"Authorization": "Bearer ${input:jam-gateway-token}"}
    }
  }
}
```

**Claude Desktop, Google Antigravity, or any client that cannot add a header**
— run the gateway through `mcp-remote` (needs Node.js). In Claude Desktop this
goes in `claude_desktop_config.json`; in Antigravity in
`~/.gemini/antigravity/mcp_config.json`:

```json
{
  "mcpServers": {
    "jam-gateway": {
      "command": "npx",
      "args": ["-y", "mcp-remote",
               "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway-<you>/mcp",
               "--header", "Authorization:${JAM_GW_AUTH}"],
      "env": {"JAM_GW_AUTH": "Bearer <your dashboard token>"}
    }
  }
}
```

**ChatGPT** cannot connect yet: it has no way to send a fixed token header, so it
needs an OAuth sign-in on the LQCDMCP platform, which is not available so far.
The panel is already built to render inside ChatGPT once it is.

## In chat

Ask the AI to run a JAM fit. It walks the same steps as the Campaign tab, one
question at a time: HPC (sign-in happens on the panel), workspace, parameters,
reactions, datasets and cuts, QCD settings, job and Slurm resources. Every
setting shows the library value; reactions and datasets are never preselected.
It validates the card, shows you the summary and the Slurm script, and submits
only after you agree. Afterwards ask for the fit summary, PDF plots (it shows
the images), the χ² by experiment, or more replicas (it gives you a phrase to
confirm). It can also write any other Slurm job with you: it checks the script
with `sbatch --test-only` and submits it after you confirm.

## Tools

Campaigns: `campaign_options`, `campaign_datasets`, `validate_campaign`,
`submit_campaign`, `list_campaigns`, `campaign_status`, `postprocess_campaign`,
`campaign_summary`, `campaign_pdf_plots`, `analyze_by_experiment`,
`append_replicas`, `delete_campaign`, `use_ai_branch`. Slurm: `slurm_partitions`,
`draft_slurm_job`, `submit_slurm_job`. Connection and workspace:
`check_hpc_connection`, `connect_hpc`, `check_project`, `use_workspace`,
`setup_workspace`, `cluster_status`. Single fits: `submit_fit_from_selection`,
`submit_jam_fit`, `list_runs`, `read_chi2`, `evaluate_fit`, `chain_state`.
Files in your own area: `fs_list`, `fs_read`, `fs_write`. Panel: `open_panel`,
`get_console`.

- A campaign generates its card from the repository's `examples/input02.py`
  plus your choices, and runs one Slurm array task per replica
  (`fitlib/ai.py`). Results are under `<workspace>/campaigns/<name>_<time>/`.
- The partition is picked for you unless you choose one: `priority` for jobs
  up to 4 h, `production` for longer ones.
- `evaluate_fit` compares finished single-fit `.msr` files with the JAM24
  paper band (arXiv:2501.00665, χ²/N mean 2.566, band [2.190, 2.819]).

## Notes

- One PIN + one-time code lasts one gateway window. After a restart you sign
  in once and everything rides the same chain.
- The gateway's code updates without a restart through `hot_redeploy.py`
  (same `LQCDMCP_TOKEN` and `JAM_GW_USER` set), or with the next launch.
- A new workspace (Project tab, or ask the AI) clones the FitPack `ai` branch,
  links the shared grids and is ready for campaigns. The repository is private:
  its GitHub token is typed on the Project tab only.
