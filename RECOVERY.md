# Recovery

What it takes to bring FemtoMind back, and what would not come back at all.
Every claim here was produced by running something against the live
deployment — including the restores, which were performed rather than assumed.
Audited 2026-08-24.

## The one-minute version

The irreplaceable data is **383 KB gzipped**. Everything else is either in this
repository or re-obtainable from whoever issued it. The single largest risk is
not corruption; it is that **every backup sits on the disk it protects**, so
losing the instance loses the backups in the same stroke.

```bash
# run this from anywhere that can reach the host, today and after any change
ssh -i <key> ec2-user@<host> 'cd ~/femtomind && tar -czf - \
  backups/ \
  fitpack-workflow/hpc_tasks.db fitpack-workflow/hpc_tasks.db-wal \
  femtomind-chatgpt-app/auth.db \
  env.conf mcpo.config.json' > femtomind-data-$(date -u +%Y%m%d).tar.gz
```

## Read this before touching a database

Two findings, both reproduced, that make the difference between a restore and a
silent loss.

**The `.db` file alone is not the database.** `hpc_tasks.db` runs in WAL mode.
At audit the file held 13 rows and the write-ahead log held another 11 —
`batch_summaries` 1→0 and `job_history` 10→0, meaning a plain `cp hpc_tasks.db`
loses an entire completed batch and its chi-square results without any error.
`deploy/backup_db.py` avoids this by using SQLite's online backup API
(`src.backup(dst)`) rather than copying bytes, which is why its snapshots are
whole. Verified: the newest snapshot and the live database dump to the *same*
sha256 over every table.

**Restoring on top of stale sidecars corrupts.** Dropping a snapshot into a
directory that still contains the old `-wal`/`-shm` produced
`sqlite3.DatabaseError: database disk image is malformed`. The same snapshot
restored into a clean directory opened with `integrity_check=ok`. So: stop the
writers, delete the sidecars, then put the file in place.

## Rebuild, in dependency order

The order is not cosmetic. The OAuth metadata the client reads is built from
`FEMTOMIND_PUBLIC_HOST`, not from the request, so a working name has to exist
before a sign-in can succeed. **DNS → TLS → OAuth → tunnel**, serially.

**1. Instance.** Amazon Linux 2023, 2 vCPU / 1 GB / 40 GB is enough — the app
holds ~70 MB resident and the disk sits under 3 GB. Open 22, 80 and 443 in the
cloud firewall and nothing else: the services bind `0.0.0.0`, so that rule is
the only thing keeping ports 8000 and 8010 off the internet.

**2. Code.** `git clone` this repository into `~/femtomind/`, keeping the layout
in [DEPLOYMENT.md](DEPLOYMENT.md). At audit, 52 of the 53 live files on the host
were md5-identical to the pushed commit and nothing ran on the host that was
absent from git — but see *Private repository* below.

**3. Python.** One virtualenv at `~/femtomind/.venv`, from `requirements.txt`
plus `mcpo`. There is no lockfile, so the rebuilt environment will not match
version-for-version; if something misbehaves after a rebuild, suspect this
first.

**4. DNS.** Re-point `femtomind.duckdns.org` at the new address, either from
the DuckDNS web account or with `deploy/duckdns_sync.sh`, which reads its token
from `~/.duckdns_token`. **Do this before TLS**: Caddy's ACME challenge
resolves the name, so a stale record fails certificate issuance.

**5. TLS.** Install `deploy/Caddyfile.example` with a real contact address.
Caddy obtains a fresh certificate on start. `/.well-known/*` must be forwarded
alongside `/mcp`, `/authorize` and `/token` — the client reads the RFC 9728 and
RFC 8414 documents *before* it will attempt OAuth at all.

**6. Services.** Install the three units from `deploy/`.
**`femtomind-app` must keep `KillMode=process`**: the SSH ControlMasters that
hold each collaborator's cluster session are its children, so the systemd
default tears down every live session on any restart.

**7. Data.** Stop the services, remove any `-wal`/`-shm`, restore both
databases from the newest snapshot, start the services. The schema needs no
migration — `apply_schema()` and `init_db()` were run against restored
snapshots from three different dates and each reported no missing tables, no
migrations applied and no row deltas.

**8. Backups.** Re-enable `femtomind-backup.timer` with `deploy/backup_db.py`,
and this time **send a copy off the box**.

**9. The ChatGPT connector.** Not automatable. There is no dynamic client
registration and no clients table; the binding between the connector and this
server exists only inside the ChatGPT account's settings. The connector must be
re-added by hand, and every collaborator re-links theirs.

**10. Cluster sessions.** Nobody is signed in after a rebuild, and nobody can
be signed in *for* them. `CONTROL_PERSIST` is 8 hours, so this is not a
one-time rebuild step: each person re-enters a PIN, a one-time code and an
account password after every idle session, forever.

## What does not come back

**Invite codes and tokens.** `auth.db` stores invite codes as plaintext and
bearer tokens as sha256 hashes. Lose the database and the codes cannot be
re-derived — you mint new ones and every collaborator re-links. This is
recoverable in the sense that access can be restored; it is not recoverable in
the sense that the same codes come back.

**The DuckDNS and OpenAI accounts behind the tokens.** `~/.duckdns_token` and
`~/.config/tunnel-client/key` are bearer credentials. Whoever can sign in to
those accounts can reissue them; the account logins themselves appear nowhere
on the host or in this repository.

**The tunnel binary.** A 29 MB pre-release build, `0.0.12-dev+17570548ca19`,
with no vendored source and no public package. If the tunnel matters to you,
keep a copy of the binary as well as the key.

**Caddy's ACME state** under `/root/.local/share/caddy` — the Let's Encrypt
account and the live key and certificate. Not a loss in practice: a fresh
instance registers again and gets a new certificate, provided DNS resolves.

**The exact package set.** No lockfile exists. Consider `pip freeze` into the
repository the next time the environment is known good.

## The parts nobody here owns

The compute half of this system depends on facility assets that are not in this
repository, not on the application host, and not under this project's control:

| | |
|---|---|
| `theory-notebook.sif` | 6.2 GB apptainer image, owned `wmoore:scicomp`, hardcoded in `slurm_tools.py` |
| `/w/ceph24/JAM/share/fitpack_grids` | 166 GB, owned `nsato:jam`, symlinked into every workspace |
| JLab account, `jam` group, `SAS-MobilePass` | per collaborator; nothing here can provision them |

A rebuild of this repository restores the interface. It does not restore the
ability to run a fit if those disappear.

**A path to check before trusting a rebuild:** `env.conf` sets
`FITPACK_REMOTE_TEMPLATE=/w/ceph24/JAM/{username}/fitpack_legacy`, while the
live `user_hpc_config.remote_base` points at `.../fitpack_demo`. Several trees
exist side by side on the cluster. Follow env.conf blindly and the rebuilt
system addresses the wrong one.

## Loss windows

**Corruption or mistake:** up to 24 hours. `femtomind-backup.timer` fires daily
at 00:00 UTC.

**Instance loss:** everything. `stat -c %d` returns the same device for
`backups/`, both databases and `/`; there is one 40 GB volume and no second
one; no `rclone`, `restic`, `borg`, `s3cmd` or `~/.aws` is installed. Whether a
platform-level snapshot exists cannot be answered from inside the host — the
instance profile is not authorised to query Lightsail, so **confirm that
separately from the AWS console** and treat it as absent until you have.

**A live countdown.** `KEEP=7` in `backup_db.py` deletes the oldest snapshot
each day. `backups/hpc_tasks-20260821-000006.db` — 1,026 rows, the last
surviving copy of the 1,020-task campaign history from before the demo reset —
is removed at the **2026-08-28 00:00 UTC** run. A local copy was taken on
2026-08-24; without it that history ends there.

## Verifying a rescue bundle

A backup nobody has opened is a belief, not a backup. This opens every file and
reports rather than assuming:

```python
import sqlite3, glob, os
for f in sorted(glob.glob("backups/*.db")) + ["fitpack-workflow/hpc_tasks.db",
                                              "femtomind-chatgpt-app/auth.db"]:
    c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    n = sum(c.execute(f"SELECT COUNT(*) FROM {r[0]}").fetchone()[0]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    print(f"{os.path.basename(f):36} {ok:4} {n:5} rows")
    c.close()
```

Run against the bundle taken on 2026-08-24 this returned `ok` for all fourteen
snapshots and both live databases, with the live `hpc_tasks.db` showing
`job_history=10` and `batch_summaries=1` — the rows that live in the WAL and
that a file copy would have dropped.

## Private repository

Every step above that says "clone this repository" depends on a GitHub account
credential that appears in none of the recovery material. The repository is
private; unauthenticated access returns 404. Whoever must perform a recovery
needs that access, and arranging it is not something the recovery itself can do.
