# Scheduled Prune: Spec

A daily cron job that applies retention policies to every discovered repo, prunes, and writes a report. It also serves as an early warning that backups are failing.

Status: in production. First live run on 2026-11-18 cleared the backlog on all repos.

## Vocabulary

- **Group**: all snapshots in a repo with the same path. Every rule and check works per group. With one repo per machine, the repo already identifies the machine, so a hostname change or rebuild continues the same history (the report lists every hostname seen in the group). `group_by: host_path` splits groups by hostname too.
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

"First" means the earliest snapshot in the whole calendar bucket, not just the part of the bucket inside the tier. This keeps the month that straddles a tier boundary from keeping a different snapshot each day. Restic's own `--keep-monthly` keeps the newest and has no half-year bucket, so the job computes the keep set itself and calls `restic forget <ids>`.

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
- Every path the repo has ever had is checked, so a path that silently stops being backed up goes stale and fails.
- **Retirement** skips the freshness check, but retention, the floor and the deletion cap still apply. A retired machine ages down to one snapshot per half-year plus the newest 3.
  - `retired_machines: [WEB, WEB2]`: whole machines, by clients-file name, case-insensitive.
  - `retired_paths: [WEB3:/root]`: one path on one machine. A bare `/root` retires that path on **all** machines.
  - A retired group with a snapshot newer than its freshness limit is WARN, because it is probably still being backed up.
- **Deletion cap**: a run may delete at most 20% of a repo's snapshots. `--no-cap` turns this off (for the initial cleanup run).
- **Repo state**: if the repo is locked, the repo is **SKIPPED**, not FAIL (most likely a backup is running). If `restic check` (without `--read-data`) fails, the repo is **FAIL**.

### Timing notes

- Daily backups run at 2 AM for all machines.
- The WAL crontab runs hourly at 1 and 4–23. 2 and 3 AM are skipped for the daily, and one extra WAL run follows the completed daily, so there is a WAL snapshot before 4 AM as well as the 4 AM one.
- At the 6 AM prune, a healthy WAL group is under 2h old, so a 2h freshness failure means a real problem, not normal timing.
- A manual run between about 2 and 4 AM may report false WAL freshness failures.

## Running

- Script: `prune_job.py`, reusing `restic_wrapper` (repo discovery, forget by ID, prune, ownership fix).
- Cron (6 AM daily), on the backup server. `-T` is required because the compose file sets `tty: true` and cron has no terminal. Cron's PATH is minimal, so use full paths:
  `0 6 * * * cd /home/user/restic-tui && /usr/bin/docker compose run --rm -T --entrypoint python restic-tui /app/prune_job.py >> /mnt/restic_tui/prune_cron.log 2>&1`
  `prune_cron.log` catches failures that happen before a report can be written (for example Docker not starting).
- Flags:
  - `--dry-run`: report what would be deleted and change nothing.
  - `--no-cap`: turn off the deletion cap (one-time initial run).
  - `--machine NAME`: limit the run to one machine (debugging).
- Per-repo order: lock check → list snapshots → compute keep set → guards → `restic check` → forget by ID (batched, `delete_batch_size`) → fix ownership → prune.
- `prune` runs only when something was forgotten. Space left behind by manual TUI forgets is reclaimed the next time the job forgets something, or by pruning from the TUI.
- A path that matches no policy keeps everything and reports WARN, so new paths are noticed.
- `--no-email`: write the report but don't send it.

### Rollout

1. `--dry-run`, then review the report.
2. `--no-cap` once, to clear the backlog.
3. Daily cron with the cap on.

## Report

- Written to `/mnt/restic_tui/prune_reports/YYYY-MM-DD_HHMMSS[-dry-run].txt` and `/mnt/restic_tui/last_prune.txt`. The timestamp keeps a dry run and a real run on the same day from overwriting each other.
- The full list of forgotten IDs goes to `<same name>.forgets.txt`. The report lists at most 20 per path.
- Email: the report is the body and the header line is the subject. SMTP settings come from `/mnt/restic_tui/.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `SMTP_TO`, comma-separated for several recipients). Port 465 uses implicit TLS; any other port uses STARTTLS. If email isn't configured, the report says so and the run doesn't fail. If sending fails, the job exits 1.
- Exit code: 1 if anything is FAIL or the email failed, otherwise 0.
- Header line, which becomes the email subject:
  `[restic] FAIL 1 / WARN 0 / SKIPPED 0 / OK 11 (dry-run)`
- A **Summary** comes first, under the header: "Needs attention" lists every FAIL, WARN and SKIPPED reason as `LEVEL  MACHINE / repo: path: reason` (or "Everything OK."), followed by a one-line tally for each report include. The details follow.
- Details, for each repo and each path: the policy matched, how many snapshots were kept and deleted, the oldest and newest kept snapshot with dates, and any failed guard with the reason.

## Report includes

Files whose new contents are added to the next report. External jobs such as the DB restore drill append to them, and each prune report includes whatever has built up since the last run. The drill itself stays outside this tool: it runs on the client, in Docker, against the live DB.

```yaml
report_includes:
  - title: DB restore drills
    path: /mnt/restic_tui/drills/db_tests.log
    truncate: true
    fail_pattern: "^RESULT FAIL"
    expect_pattern: "^RESULT "
    expect_count: 4
    count_patterns:
      PASS: "^RESULT PASS"
      SKIP: "^RESULT SKIP"
      FAIL: "^RESULT FAIL"
```

- **truncate**: after the include is read, the next run starts with an empty file. This is done by renaming the file to `<name>.YYYYMMDD-HHMMSS` before reading it, not by truncating in place, so lines appended during the prune run are not lost. A new `>>` creates a fresh file. Renamed copies are deleted after 30 days.
- **fail_pattern**: any matching line counts as FAIL in the report header.
- **expect_pattern**: marks one result line per run. The matching lines are listed under the include's summary. If the file has content but no line matches, it counts as WARN.
- **expect_count**: the number of runs expected per report. Fewer result lines count as **FAIL**, because a run that died before finishing (SSH, tunnel or crash) prints no `RESULT` line and its DB was not verified. Counting only `RESULT FAIL` would miss that. More lines than expected (an extra manual run) is a note, not a failure.
- **count_patterns**: label → pattern, tallied in the summary, for example `PASS 3 / SKIP 1 / FAIL 0 / results 4 of 4`.
- A missing or empty file is reported as "nothing new", not as a failure (for example, weekly drills).
- `--dry-run` reads includes but does not rotate them.
- Each include is capped at about 200 KB in the report. If a file is larger, the last part is kept.
- Writers should capture stderr as well. The weekly drill (all DBs in one script, run as the backup-server user that holds the client SSH keys):
  `0 23 * * 0 /home/user/dbtest.sh >> /mnt/restic_tui/drills/db_tests.log 2>&1`
- A drill cron that never runs is silent: the file is simply empty ("nothing new").

**Scheduling**: `prune` takes an exclusive repo lock and the drill's `restic restore` takes a shared one. Do not schedule drills to overlap the 6 AM prune.

## Deployment

- Add `prune_job.py` to the `COPY` line in `Dockerfile`.
- Add `prune_job.py` to the scp list in `deploy_from_nas.sh`.
- Copy changed files to `user@nas:/home/user/restic-tui/`, then run the deploy on the backup server.
