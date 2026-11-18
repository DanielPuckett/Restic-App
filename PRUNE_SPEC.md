# Scheduled Prune: Spec

A daily cron job that applies retention policies to every discovered repo, prunes, and writes a report. It also serves as an early warning that backups are failing.

Status: agreed design, not yet built.

## Vocabulary

- **Group**: all snapshots in a repo with the same host + path. Every rule and check works per group.
- **Policy**: the retention rules for a group, chosen by matching the snapshot path against a pattern.
- **Guard**: a precondition. If one fails, nothing is deleted in that repo and the report says FAIL.
- **Floor**: a rule that holds no matter what. It only blocks deletion and never raises an alert.

Each backup path is its own snapshot (`/mnt`, `/home`, `/root`, `.../db-backup/daily`, `.../db-backup/wal` are separate snapshots), so a group is a single backup path.

## Policies

Policies attach to snapshot paths, not repos, so they apply to every machine and every DB repo found by repo discovery with no per-repo config.

| Policy | Matches | Keep all within | 6 months – 2 years | Older than 2 years |
|---|---|---|---|---|
| `files` | `/mnt`, `/home`, `/root` | 6 months | first snapshot of each calendar month | first snapshot of each half-year (Jan–Jun, Jul–Dec), kept indefinitely |
| `db_daily` | `*/db-backup/daily` | 6 months | first snapshot of each calendar month | **deleted** (privacy policy: max 2-year retention) |
| `db_wal` | `*/db-backup/wal` | see WAL cutoff | n/a | n/a |

DB snapshots are also tagged `pg-daily` and `pg-wal`. Policies match on path. If a snapshot's tag doesn't fit its path policy (for example, `pg-wal` on a `daily` path), the report shows a WARN.

"First" means the earliest snapshot in the bucket. Restic's own `--keep-monthly` keeps the newest and has no half-year bucket, so the job computes the keep set itself and calls `restic forget <ids>`.

The 2-year cap applies to DB paths only. `/home` holds code, compose files and `.env`s, with no personal data. DB files, dumps, tars and WAL under `/mnt` are excluded from `/mnt` snapshots by `_NO_BACKUP_` tag files.

### WAL cutoff

WAL exists only to roll a daily forward, so WAL retention is tied to the oldest daily we want to be able to roll forward from (a 7-day point-in-time window).

- **Anchor**: the newest `daily` snapshot at or before *now − 7 days*. The daily is found as the sibling path: `…/db-backup/wal` pairs with `…/db-backup/daily`.
- Keep every WAL snapshot from the anchor's time onward. Delete WAL older than the anchor.
- No anchor (for example a DB with less than 7 days of history): delete no WAL.

## Floors

- Always keep the newest 3 snapshots in every group, regardless of age.
- Never delete a WAL snapshot newer than the newest daily.

## Guards

Checked per repo before any deletion. If a guard fails, the repo is not changed.

- **Freshness**: the newest snapshot in each group must be within:
  - 26h for `files` and `db_daily` (daily backups at 2 AM)
  - 2h for `db_wal`
- Every path the repo has ever had is checked, so a path that silently stops being backed up goes stale and fails. Paths retired on purpose go in `retired_paths` and are skipped.
- **Deletion cap**: a run may delete at most 20% of a repo's snapshots. `--no-cap` turns this off (for the initial cleanup run).
- **Repo state**: if the repo is locked, the repo is **SKIPPED**, not FAIL (most likely a backup is running). If `restic check` (without `--read-data`) fails, the repo is **FAIL**.

### Timing notes

- Daily backups run at 2 AM for all machines.
- The WAL crontab runs hourly at 1 and 4–23. 2 and 3 AM are skipped for the daily, and one extra WAL run follows the completed daily, so there is a WAL snapshot before 4 AM as well as the 4 AM one.
- At the 6 AM prune, a healthy WAL group is under 2h old, so a 2h freshness failure means a real problem, not normal timing.
- A manual run between about 2 and 4 AM may report false WAL freshness failures.

## Running

- Script: `prune_job.py`, reusing `restic_wrapper` (repo discovery, forget by ID, prune, ownership fix).
- Cron (6 AM daily):
  `0 6 * * * cd ~/restic-tui && docker compose run --rm --entrypoint python restic-tui /app/prune_job.py`
- Flags:
  - `--dry-run`: report what would be deleted and change nothing.
  - `--no-cap`: turn off the deletion cap (one-time initial run).
  - `--machine NAME`: limit the run to one machine (debugging).
- Per-repo order: discover groups → check guards → compute keep set → forget by ID → prune → fix ownership.

### Rollout

1. `--dry-run`, then review the report.
2. `--no-cap` once, to clear the backlog.
3. Daily cron with the cap on.

## Report

- Written to `/mnt/restic_tui/prune_reports/YYYY-MM-DD.txt` and `/mnt/restic_tui/last_prune.txt`.
- Email comes later, with SMTP settings in a `.env`. The report file is the email body.
- Header line, which becomes the email subject:
  `[restic] FAIL 1 / WARN 0 / SKIPPED 0 / OK 11 (dry-run)`
- For each repo and each path: the policy matched, how many snapshots were kept and deleted, the oldest and newest kept snapshot with dates, and any failed guard with the reason.

## Report includes

Files whose new contents are added to the next report. External jobs such as the DB restore drill append to them, and each prune report includes whatever has built up since the last run. The drill itself stays outside this tool: it runs on the client, in Docker, against the live DB.

```yaml
report_includes:
  - title: DB restore drills
    path: /mnt/restic_tui/drills/db_tests.log
    truncate: true
    fail_pattern: "^RESULT FAIL"
    expect_pattern: "^RESULT "
```

- **truncate**: after the include is read, the next run starts with an empty file. This is done by renaming the file to `<name>.YYYY-MM-DD` before reading it, not by truncating in place, so lines appended during the prune run are not lost. A new `>>` creates a fresh file. Renamed copies are deleted after 30 days.
- **fail_pattern**: any matching line counts as FAIL in the report header.
- **expect_pattern**: if the file has content but no line matches, it counts as WARN (for example, a drill that crashed before printing `RESULT`).
- A missing or empty file is reported as "nothing new", not as a failure (for example, weekly drills).
- `--dry-run` reads includes but does not rotate them.
- Each include is capped at about 200 KB in the report. If a file is larger, the last part is kept.
- Writers should capture stderr as well:
  `pg-restore.restic.sh ast >> /mnt/restic_tui/drills/db_tests.log 2>&1`

**Scheduling**: `prune` takes an exclusive repo lock and the drill's `restic restore` takes a shared one. Do not schedule drills to overlap the 6 AM prune.

## Deployment

- Add `prune_job.py` to the `COPY` line in `Dockerfile`.
- Add `prune_job.py` to the scp list in `deploy_from_nas.sh`.
- Copy changed files to `daniel@nas:/home/daniel/restic-tui/`, then run the deploy on the backup server.
