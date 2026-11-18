"""Retention planning tests for prune_job. No restic needed.

Run: python3 tests/test_prune_plan.py
"""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Python < 3.10 can't evaluate "str | None" annotations; load the wrapper with postponed annotations.
if sys.version_info < (3, 10):
    src = open(os.path.join(ROOT, "restic_wrapper.py")).read()
    mod = types.ModuleType("restic_wrapper")
    mod.__file__ = os.path.join(ROOT, "restic_wrapper.py")
    sys.modules["restic_wrapper"] = mod
    exec(compile("from __future__ import annotations\n" + src, mod.__file__, "exec"), mod.__dict__)

import restic_wrapper as restic  # noqa: E402
import prune_job as pj  # noqa: E402

EST = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)  # 06:00 EST
CFG = pj.load_prune_config({})
_n = [0]


def snap(t, path, tags=(), host="web3"):
    _n[0] += 1
    sid = f"{_n[0]:08x}" + "0" * 56
    return restic.Snapshot(id=sid, short_id=sid[:8], time=t, paths=[path], hostname=host,
                           username="root", tags=list(tags))


def daily_series(path, start, end, tags=(), hour=2):
    out, d = [], start
    while d <= end:
        out.append(snap(d.replace(hour=hour, minute=0), path, tags))
        d += timedelta(days=1)
    return out


def plan_for(snaps, path):
    return next(p for p in pj.plan_repo(snaps, CFG, NOW) if p.path == path)


failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ── files policy: 3 years of nightly /home ───────────────────────────────────
start = datetime(2023, 11, 1, tzinfo=EST)
end = datetime(2026, 11, 18, tzinfo=EST)
home = daily_series("/home", start, end)
gp = plan_for(home, "/home")
kept = {s.id for s in gp.keep}
cut6 = pj.ago(NOW, "6M")
cut2y = pj.ago(NOW, "2y")

check(gp.policy == "files", "/home matches files policy")
check(all(s.id in kept for s in home if s.time >= cut6), "files: everything within 6 months kept")

mid = [s for s in home if cut2y <= s.time < cut6]
mid_kept = [s for s in mid if s.id in kept]
check(all(s.time.day == 1 for s in mid_kept),
      f"files 6M-2y: only first-of-month kept (got days {sorted({s.time.day for s in mid_kept})})")
months = {(s.time.year, s.time.month) for s in mid if s.time.day == 1}
check(len(mid_kept) == len(months), f"files 6M-2y: one per month ({len(mid_kept)} kept, {len(months)} months)")

# Boundary stability: running a day later must not forget anything kept today except by aging
gp2 = plan_for(home, "/home")
tomorrow = next(p for p in pj.plan_repo(home, CFG, NOW + timedelta(days=1)) if p.path == "/home")
newly_forgotten = {s.id for s in gp2.keep} - {s.id for s in tomorrow.keep}
check(len(newly_forgotten) <= 1, f"stable across days: {len(newly_forgotten)} kept snapshot(s) dropped the next day")

old = [s for s in home if s.time < cut2y]
old_kept = [s for s in old if s.id in kept]
check([(s.time.year, s.time.month, s.time.day) for s in old_kept] == [(2023, 11, 1), (2024, 1, 1), (2024, 7, 1)],
      f"files >2y: first of each half-year kept: {[pj.fmt_time(s.time) for s in old_kept]}")
check(not gp.fails, "files: fresh, no guard failures")

# ── db_daily: thinned like files, nothing past 2 years ───────────────────────
dpath = "/mnt/appdb/db-backup/daily"
wpath = "/mnt/appdb/db-backup/wal"
daily = daily_series(dpath, start, end, tags=["pg-daily"])
gpd = plan_for(daily, dpath)
dk = {s.id for s in gpd.keep}
check(gpd.policy == "db_daily", "daily path matches db_daily")
check(not any(s.time < cut2y for s in gpd.keep), "db_daily: nothing older than 2 years kept")
check(all(s.id in dk for s in daily if s.time >= cut6), "db_daily: everything within 6 months kept")
check(len([s for s in gpd.keep if cut2y <= s.time < cut6]) == len(months), "db_daily 6M-2y: one per month")

# ── db_wal: hourly at 1,4-23 for 20 days, plus one right after the daily ─────
wal = []
d = datetime(2026, 10, 29, tzinfo=EST)
while d <= datetime(2026, 11, 18, 5, tzinfo=EST):
    for h in [1, 4] + list(range(4, 24)):
        t = d.replace(hour=h, minute=0, second=2)
        if t <= datetime(2026, 11, 18, 5, tzinfo=EST):
            wal.append(snap(t, wpath, ["pg-wal"]))
    wal.append(snap(d.replace(hour=2, minute=0, second=8), wpath, ["pg-wal"]))  # after the daily
    d += timedelta(days=1)
wal.sort(key=lambda s: s.time)
recent_daily = daily_series(dpath, datetime(2026, 10, 29, tzinfo=EST), end, tags=["pg-daily"])
plans = pj.plan_repo(recent_daily + wal, CFG, NOW)
gpw = next(p for p in plans if p.path == wpath)
anchor_cut = pj.ago(NOW, "7d")
anchor = max((s for s in recent_daily if s.time <= anchor_cut), key=lambda s: s.time)
check(gpw.policy == "db_wal", "wal path matches db_wal")
check(all(s.time >= anchor.time for s in gpw.keep), "db_wal: nothing older than the anchor daily kept")
check(all(s.id in {k.id for k in gpw.keep} for s in wal if s.time >= anchor.time),
      f"db_wal: everything from anchor {pj.fmt_time(anchor.time)} onward kept")
check(min(s.time for s in gpw.keep) - anchor.time < timedelta(minutes=1),
      "db_wal: oldest kept WAL is the one right after the anchor daily")
check(not gpw.fails, "db_wal: fresh at 06:00 (newest WAL 05:00)")

# WAL with no daily older than 7 days: keep all
young_daily = daily_series(dpath, datetime(2026, 11, 14, tzinfo=EST), end, tags=["pg-daily"])
young_wal = [s for s in wal if s.time >= datetime(2026, 11, 14, tzinfo=EST)]
gpy = next(p for p in pj.plan_repo(young_daily + young_wal, CFG, NOW) if p.path == wpath)
check(not gpy.forget, "db_wal: no anchor yet -> nothing forgotten")

# WAL with no daily at all: keep all, warn
gpn = next(p for p in pj.plan_repo(wal, CFG, NOW) if p.path == wpath)
check(not gpn.forget and gpn.warns, "db_wal: no daily group -> keep all and WARN")

# ── guards ───────────────────────────────────────────────────────────────────
stale = daily_series("/root", start, datetime(2026, 11, 15, tzinfo=EST))
gps = plan_for(stale, "/root")
check(bool(gps.fails) and "stale" in gps.fails[0], f"freshness: 3-day-old /root fails ({gps.fails})")
check(len(gps.keep) >= 3 and all(s in gps.keep for s in stale[-3:]), "floor: newest 3 kept even when stale")

stale_wal = [s for s in wal if s.time <= datetime(2026, 11, 18, 2, 30, tzinfo=EST)]
gpsw = next(p for p in pj.plan_repo(recent_daily + stale_wal, CFG, NOW) if p.path == wpath)
check(bool(gpsw.fails), "freshness: WAL 3.5h old at 06:00 fails the 2h limit")

cfg_ret = dict(CFG, retired_paths=["/root"])
gpr = next(p for p in pj.plan_repo(stale, cfg_ret, NOW) if p.path == "/root")
check(not gpr.fails, "retired path (bare): no freshness failure")

# Machine-scoped retirement
cfg_m = dict(CFG, retired_machines=["web"])
gpm = next(p for p in pj.plan_repo(stale, cfg_m, NOW, "WEB") if p.path == "/root")
check(not gpm.fails and any("retired machine" in n for n in gpm.notes), "retired_machines: case-insensitive, no freshness failure")
gpm2 = next(p for p in pj.plan_repo(stale, cfg_m, NOW, "WEB3") if p.path == "/root")
check(bool(gpm2.fails), "retired_machines: other machines still checked")
check(len(gpm.forget) == len(gpm2.forget) > 0, "retired machine: retention still applies")

cfg_mp = dict(CFG, retired_paths=["WEB3:/root"])
gpp = next(p for p in pj.plan_repo(stale, cfg_mp, NOW, "WEB3") if p.path == "/root")
gpp2 = next(p for p in pj.plan_repo(stale, cfg_mp, NOW, "WEB4") if p.path == "/root")
check(not gpp.fails and bool(gpp2.fails), "retired_paths MACHINE:path only affects that machine")

gpw_ret = next(p for p in pj.plan_repo(home, cfg_m, NOW, "WEB") if p.path == "/home")
check(not gpw_ret.fails and any("recent snapshot" in w for w in gpw_ret.warns), "retired machine with a fresh snapshot: WARN")


badtag = [snap(datetime(2026, 11, 18, 2, tzinfo=EST), dpath, ["pg-wal"])]
gpt = plan_for(badtag, dpath)
check(any("missing tag" in w for w in gpt.warns), "tag cross-check: pg-wal on daily path warns")

etc = [snap(datetime(2026, 11, 18, 2, tzinfo=EST), "/etc")]
gpe = plan_for(etc, "/etc")
check(gpe.policy is None and not gpe.forget and gpe.warns, "unmatched path: keep all and WARN")

# Hostname change (machine rebuilt): old host Apr-Jun, renamed host Jun-Nov 17, old name again Nov 18
dvr = ([snap(s.time, "/home", host="oldhostname") for s in daily_series("/home", datetime(2026, 4, 26, tzinfo=EST), datetime(2026, 6, 12, tzinfo=EST))]
       + [snap(s.time, "/home", host="newhostname") for s in daily_series("/home", datetime(2026, 6, 13, tzinfo=EST), datetime(2026, 11, 17, tzinfo=EST))]
       + [snap(datetime(2026, 11, 18, 2, tzinfo=EST), "/home", host="oldhostname")])
gdvr = pj.plan_repo(dvr, CFG, NOW)
check(len(gdvr) == 1 and not gdvr[0].fails, "group_by path (default): hostname change is one fresh group")
check("newhostname (" in gdvr[0].host and "oldhostname (" in gdvr[0].host, f"group_by path: report lists both hosts ({gdvr[0].host})")
check(any(s.hostname == "oldhostname" and s.time.month == 5 and s.time.day == 1 for s in gdvr[0].keep),
      "group_by path: old host's first-of-month still kept as history")
gsplit = pj.plan_repo(dvr, dict(CFG, group_by="host_path"), NOW)
check(len(gsplit) == 2 and any(p.fails for p in gsplit), "group_by host_path: split by host, abandoned host goes stale")

# ── run_repo guards with a fake restic ───────────────────────────────────────
class Fake:
    def __init__(self, snaps, locks=(), check_ok=True):
        self.snaps, self.locks, self.check_ok = snaps, list(locks), check_ok
        self.forgot, self.pruned = [], False

    def install(self):
        pj.restic.list_locks = lambda r, p: (self.locks, "")
        pj.restic.list_snapshots_checked = lambda r, p: (self.snaps, "")
        pj.restic.check = lambda r, p: (self.check_ok, "" if self.check_ok else "error: pack missing")
        pj.restic.forget_batch = lambda r, p, ids, b: (self.forgot.extend(ids) or True, "ok")
        pj.restic.prune = lambda r, p: (setattr(self, "pruned", True) or "Prune completed.")
        pj.restic._fix_repo_ownership = lambda r: ""
        return self


f = Fake(home).install()
r = pj.run_repo("WEB3 / repo", "/x", "", CFG, NOW, dry_run=False, no_cap=False, batch_size=50)
check(r.status == pj.FAIL and not f.forgot, f"deletion cap: initial cleanup blocked ({len(r.forget_ids)} of {len(home)})")

f = Fake(home).install()
r = pj.run_repo("WEB3 / repo", "/x", "", CFG, NOW, dry_run=True, no_cap=True, batch_size=50)
check(r.status == pj.OK and not f.forgot and not f.pruned, "dry-run + no-cap: OK, nothing changed")

f = Fake(home).install()
r = pj.run_repo("WEB3 / repo", "/x", "", CFG, NOW, dry_run=False, no_cap=True, batch_size=50)
check(r.status == pj.OK and len(f.forgot) == len(r.forget_ids) > 0 and f.pruned, "no-cap real run: forgets and prunes")

f = Fake(home, locks=["abcdef1234"]).install()
r = pj.run_repo("WEB3 / repo", "/x", "", CFG, NOW, dry_run=False, no_cap=True, batch_size=50)
check(r.status == pj.SKIPPED and not f.forgot, "locked repo: SKIPPED")

f = Fake(home, check_ok=False).install()
r = pj.run_repo("WEB3 / repo", "/x", "", CFG, NOW, dry_run=False, no_cap=True, batch_size=50)
check(r.status == pj.FAIL and not f.forgot, "restic check failure: FAIL, nothing forgotten")

f = Fake(home + stale).install()
r = pj.run_repo("WEB3 / repo", "/x", "", CFG, NOW, dry_run=False, no_cap=True, batch_size=50)
check(r.status == pj.FAIL and not f.forgot, "stale group blocks deletion for the whole repo")
f = Fake(stale).install()
r = pj.run_repo("WEB / repo", "/x", "", cfg_m, NOW, dry_run=True, no_cap=True, batch_size=50, machine="WEB")
check(r.status == pj.OK, f"run_repo on retired machine with --no-cap: OK ({r.status})")

# ── includes ─────────────────────────────────────────────────────────────────
import tempfile  # noqa: E402

tmp = tempfile.mkdtemp()
log = os.path.join(tmp, "db_tests.log")
inc = {"title": "DB drills", "path": log, "truncate": True,
       "fail_pattern": "^RESULT FAIL", "expect_pattern": "^RESULT "}
st, _ = pj.run_include(inc, NOW, False, 30)
check(st == pj.OK, "include: missing file -> nothing new, OK")
open(log, "w").write("== ast\nPASS rows\nRESULT PASS ast on web3\n")
st, lines = pj.run_include(inc, NOW, True, 30)
check(st == pj.OK and os.path.exists(log), "include dry-run: read, not rotated")
st, lines = pj.run_include(inc, NOW, False, 30)
check(st == pj.OK and not os.path.exists(log) and any("rotated" in l for l in lines), "include: rotated after read")
open(log, "w").write("== app\nRESULT FAIL app on web3\n")
st, _ = pj.run_include(inc, NOW + timedelta(seconds=1), False, 30)
check(st == pj.FAIL, "include: RESULT FAIL -> FAIL")
open(log, "w").write("== app\nunpacking...\n")
st, _ = pj.run_include(inc, NOW + timedelta(seconds=2), False, 30)
check(st == pj.WARN, "include: no RESULT line -> WARN")

# ── env + email config ───────────────────────────────────────────────────────
envf = os.path.join(tmp, ".env")
open(envf, "w").write("# SMTP\nSMTP_HOST=smtp.example.com\nSMTP_PORT=587\nSMTP_PASS='se=cret'\n")
env = pj.load_env(envf)
check(env == {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587", "SMTP_PASS": "se=cret"}, f"load_env parses comments, quotes, '=' in values")
err = pj.send_email("s", "b", env)
check(err.startswith("email not configured") and "SMTP_TO" in err and "se=cret" not in err, f"send_email: missing fields reported, no secret leaked ({err})")

# ── main() end to end with fake restic ───────────────────────────────────────
mroot = os.path.join(tmp, "BACKUPS", "bu_web3")
for d in ("repo", "repo-pg-ast"):
    os.makedirs(os.path.join(mroot, d, "keys"))
    open(os.path.join(mroot, d, "config"), "w").close()
clients = os.path.join(tmp, "clients")
open(clients, "w").write(f"WEB3\nbu_web3\nssh root@web3\n{mroot}\n")
cfgf = os.path.join(tmp, "config.yaml")
open(cfgf, "w").write("")  # filled below without yaml dependency

by_repo = {os.path.join(mroot, "repo"): home, os.path.join(mroot, "repo-pg-ast"): recent_daily + wal}
pj.restic.list_locks = lambda r, p: ([], "")
pj.restic.list_snapshots_checked = lambda r, p: (by_repo[r], "")
pj.restic.check = lambda r, p: (True, "")
fake_yaml = types.ModuleType("yaml")
fake_yaml.safe_load = lambda f: {
    "clients_file": clients, "restic_password": "x",
    "prune": {"report_dir": os.path.join(tmp, "reports"), "last_report": os.path.join(tmp, "last_prune.txt"),
              "env_file": envf,
              "report_includes": [inc]},
}
sys.modules["yaml"] = fake_yaml
open(log, "w").write("RESULT PASS ast on web3\n")
pj.datetime = type("D", (datetime,), {"now": staticmethod(lambda tz=None: NOW)})
rc = pj.main(["--config", cfgf, "--dry-run"])
last = open(os.path.join(tmp, "last_prune.txt")).read()
check(last.startswith("[restic] FAIL 2 / WARN 0 / SKIPPED 0 / OK 1 (dry-run)"), f"main dry-run header (both repos over cap): {last.splitlines()[0]}")
check("deletion cap" in last and "email: email not configured" in last, "main: cap failure and unconfigured email reported")
check(rc == 1, "main: exit 1 when any FAIL")
check(os.path.exists(log), "main dry-run: include not rotated")
rc = pj.main(["--config", cfgf, "--dry-run", "--no-cap", "--no-email"])
last = open(os.path.join(tmp, "last_prune.txt")).read()
check(last.startswith("[restic] FAIL 0 / WARN 0 / SKIPPED 0 / OK 3 (dry-run, no-cap)") and rc == 0, f"main no-cap: {last.splitlines()[0]}")
check(any(n.endswith(".forgets.txt") for n in os.listdir(os.path.join(tmp, "reports"))), "main: .forgets.txt written")
print("\n----- sample report (truncated) -----")
print("\n".join(last.splitlines()[:40]))

print()
print(f"{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
