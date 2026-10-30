# E4 — the real-use window

The paper's deployment claim is not "the gateway runs" but "people other than
its author used it, and here is what happened". This file is the procedure for
opening, running and closing that window, and for turning its audit log into
the paper's table. It needs the owner's dashboard token and the users' consent;
nothing here is automated beyond the two reports.

## What the window is

A dated interval (start, end) during which a shared gateway runs on the LQCD
node with `JAM_GW_SHARED=1`, and each invited collaborator holds their own
token. The gateway acts as the person named in each call; the audit log
(`~/.jam_gateway/audit.jsonl`) records every call, job and effect with the
acting user, so the window's evidence is produced by the system, not by
after-the-fact recollection.

## Before opening (owner)

1. Launch with shared mode on; confirm the panel and `deploy_and_verify.py --check` pass.
2. `python3 redeploy_jam.py user-token list` — who already has a token.
3. For each collaborator: `python3 redeploy_jam.py user-token add <name>`; send them
   the token out of band (never in the audit log, never in chat with an AI client).
4. Record the opening date and the invited list in `consent.json` as each person agrees
   to be reported individually:

```json
{"users": ["alice", "bob"], "window": {"start": "2026-11-02", "end": "2026-11-30"}}
```

Anyone who agrees to use the gateway but not to be named is counted in the
totals only: `usage_report.py` never names a user outside `consent.users`.

## What each user is told (send verbatim)

> You have your own token for the JAM gateway. Open the panel link, paste the
> token, and sign in to the cluster yourself (PIN, one-time code, ifarm
> password): the gateway never sees or stores them. Every tool call acts as
> you, on your own Slurm account and workspace. What is recorded: the name of
> each tool, who called it, how long it took, whether it refused and with which
> code, and a hash of the arguments — never a card, a password or a token. If a
> fit is refused, the refusal code says which layer refused it; tell us if you
> think a refusal is wrong.

## During the window

- Weekly: `python3 -m evaluation.audit.failure_table --log ~/.jam_gateway/audit.jsonl --md`
  — tool × outcome × refusal code × stage. A refusal code that never fires, or
  one that fires on a correct request, is a finding either way.
- Keep the audit log: copy it off the node before the job ends (it lives in the
  gateway's home, not in the repository).

## Closing the window (owner)

1. `python3 redeploy_jam.py user-token revoke <name>` for each user; stop the gateway.
2. Copy `audit.jsonl` to `~/bench_eval/audit/window_<start>_<end>.jsonl`.
3. `python3 -m evaluation.audit.usage_report --log <copy> --consent consent.json --since <start> --until <end>`
4. Fill the paper's table from the two reports:

| Window | Users | Sessions | Active days | Calls | Submissions | Refused (code) | Median latency |
|---|---|---|---|---|---|---|---|
| <start>–<end> |  |  |  |  |  |  |  |

## What this does and does not prove

It proves real use by named people, with the refusals and failures that
actually happened, on a production cluster. It does not prove the gateway is
*necessary*: that is E1/E2/E3's job. Keep the two apart in the paper — the
window is the deployment claim, the fault table is the mechanism claim.

## Open items for the owner

- Who to invite (3–5 JAM members was the target), and for how long.
- Whether each person consents to being named (else totals only).
- The gateway's job length: a 6 h job ends the window early; relaunching needs
  a new panel sign-in, so schedule the window around the job length or relaunch
  daily.
