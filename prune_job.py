"""Scheduled prune: apply retention policies to every discovered repo, prune, report.

See PRUNE_SPEC.md for the design. Run inside the container:
    python /app/prune_job.py [--dry-run] [--no-cap] [--machine NAME]
"""
from __future__ import annotations

import argparse
import calendar
import fnmatch
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import restic_wrapper as restic

CONFIG_PATH = "/mnt/restic_tui/config.yaml"
INCLUDE_MAX_BYTES = 200 * 1024

DEFAULT_PRUNE = {
    "report_dir": "/mnt/restic_tui/prune_reports",
    "last_report": "/mnt/restic_tui/last_prune.txt",
    "keep_last": 3,
    "max_forget_percent": 20,
    "default_fresh_within": "26h",
    "group_by": "path",
    "retired_machines": [],
    "retired_paths": [],
    "include_retention_days": 30,
    "policies": [
        {
            "name": "files",
            "match": ["/mnt", "/home", "/root"],
            "keep_all_within": "6M",
            "tiers": [
                {"older_than": "6M", "keep": "monthly"},
                {"older_than": "2y", "keep": "halfyearly"},
            ],
            "fresh_within": "26h",
        },
        {
            "name": "db_daily",
            "match": ["*/db-backup/daily"],
            "keep_all_within": "6M",
            "tiers": [{"older_than": "6M", "keep": "monthly"}],
            "max_age": "2y",
            "fresh_within": "26h",
            "expect_tag": "pg-daily",
        },
        {
            "name": "db_wal",
            "match": ["*/db-backup/wal"],
            "wal_for": "daily",
            "roll_forward": "7d",
            "fresh_within": "2h",
            "expect_tag": "pg-wal",
        },
    ],
    "report_includes": [],
}

FAIL, WARN, SKIPPED, OK = "FAIL", "WARN", "SKIPPED", "OK"
_RANK = {OK: 0, SKIPPED: 1, WARN: 2, FAIL: 3}


# ── Time helpers ─────────────────────────────────────────────────────────────

def _sub_months(dt: datetime, n: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - n
    y, m = divmod(total, 12)
    m += 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def ago(now: datetime, spec: str) -> datetime:
    """now minus a duration like '26h', '7d', '2w', '6M', '2y' (M/y are calendar-aware)."""
    m = re.fullmatch(r"\s*(\d+)\s*([hdwMy])\s*", str(spec))
    if not m:
        raise ValueError(f"bad duration {spec!r} (use e.g. 26h, 7d, 2w, 6M, 2y)")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        return now - timedelta(hours=n)
    if unit == "d":
        return now - timedelta(days=n)
    if unit == "w":
        return now - timedelta(weeks=n)
    if unit == "M":
        return _sub_months(now, n)
    return _sub_months(now, n * 12)


def fmt_time(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


def fmt_age(now: datetime, t: datetime) -> str:
    secs = (now - t).total_seconds()
    if secs < 3600 * 48:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.0f}d ago"


def bucket_key(t: datetime, kind: str) -> tuple:
    """Calendar bucket in the snapshot's own local time."""
    if kind == "monthly":
        return (t.year, t.month)
    if kind == "halfyearly":
        return (t.year, 0 if t.month <= 6 else 1)
    if kind == "yearly":
        return (t.year,)
    raise ValueError(f"unknown tier keep {kind!r} (use monthly, halfyearly, yearly)")


# ── Planning ─────────────────────────────────────────────────────────────────

@dataclass
class GroupPlan:
    host: str
    path: str
    policy: str | None
    snaps: list  # all snapshots, oldest first
    keep: list = field(default_factory=list)
    forget: list = field(default_factory=list)
    fails: list = field(default_factory=list)
    warns: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def match_policy(path: str, policies: list[dict]) -> dict | None:
    for p in policies:
        for pat in p.get("match", []):
            if fnmatch.fnmatchcase(path, pat):
                return p
    return None


def plan_tiered(snaps: list, policy: dict, now: datetime) -> tuple[set, list[str]]:
    """Return (ids to keep, notes) for a files/daily style policy. snaps oldest first."""
    keep: set = set()
    notes: list[str] = []
    keep_all_cut = ago(now, policy["keep_all_within"]) if policy.get("keep_all_within") else None
    max_age_cut = ago(now, policy["max_age"]) if policy.get("max_age") else None
    tiers = [(ago(now, t["older_than"]), t["keep"]) for t in policy.get("tiers", [])]
    tiers.sort(key=lambda x: x[0])  # oldest cutoff (most aggressive tier) first

    seen_buckets: set = set()
    # "First of the month" means the earliest snapshot of the whole calendar bucket, not the
    # earliest inside the tier window; otherwise the month straddling a tier boundary would
    # keep a different snapshot every day.
    first_in_bucket: dict = {}
    for s in snaps:
        for _, kind in tiers:
            first_in_bucket.setdefault((kind, bucket_key(s.time, kind)), s.id)
    for s in snaps:
        if max_age_cut and s.time < max_age_cut:
            continue  # beyond max retention
        if keep_all_cut is None or s.time >= keep_all_cut:
            keep.add(s.id)
            continue
        tier = next(((i, kind) for i, (cut, kind) in enumerate(tiers) if s.time < cut), None)
        if tier is None:
            keep.add(s.id)  # older than keep_all but no tier applies: keep, to be safe
            continue
        if first_in_bucket[(tier[1], bucket_key(s.time, tier[1]))] == s.id:
            keep.add(s.id)
    if max_age_cut:
        notes.append(f"max age {policy['max_age']}: nothing kept before {fmt_time(max_age_cut)}")
    return keep, notes


def plan_wal(snaps: list, policy: dict, now: datetime, daily: list | None) -> tuple[set, list[str], list[str]]:
    """Return (ids to keep, notes, warns) for WAL. Keep everything from the anchor daily onward."""
    if not daily:
        return {s.id for s in snaps}, [], ["no matching daily snapshots found; keeping all WAL"]
    cutoff = ago(now, policy.get("roll_forward", "7d"))
    anchors = [d for d in daily if d.time <= cutoff]
    if not anchors:
        return {s.id for s in snaps}, [f"no daily older than {policy.get('roll_forward', '7d')} yet; keeping all WAL"], []
    anchor = anchors[-1]
    keep = {s.id for s in snaps if s.time >= anchor.time}
    note = f"roll-forward anchor: daily {anchor.short_id} {fmt_time(anchor.time)}; WAL before it is not needed"
    return keep, [note], []


def is_retired(machine: str, path: str, cfg: dict) -> str:
    """Return why a group is retired ('' if not). Machine names match case-insensitively.

    retired_machines: [WEB, WEB2]          whole machine
    retired_paths: [WEB3:/root, /old]      'MACHINE:path' for one machine, bare path for all machines
    """
    m = machine.lower()
    if m and m in {str(x).lower() for x in cfg.get("retired_machines") or []}:
        return "retired machine"
    for entry in cfg.get("retired_paths") or []:
        entry = str(entry)
        if not entry.startswith("/") and ":" in entry:
            em, ep = entry.split(":", 1)
            if em.lower() == m and ep == path:
                return f"retired path ({entry})"
        elif entry == path:
            return f"retired path ({entry}, all machines)"
    return ""


def plan_repo(snaps: list, cfg: dict, now: datetime, machine: str = "") -> list[GroupPlan]:
    policies = cfg["policies"]
    keep_last = int(cfg.get("keep_last", 3))
    group_by = cfg.get("group_by", "path")
    if group_by not in ("path", "host_path"):
        raise ValueError(f"group_by must be 'path' or 'host_path', not {group_by!r}")

    # group_by 'path': the repo (one per machine) identifies the machine, so a hostname
    # change or rebuild continues the same history. 'host_path' splits by hostname too.
    groups: dict[tuple, list] = {}
    for s in snaps:
        path = ",".join(sorted(s.paths))
        key = (s.hostname if group_by == "host_path" else "", path)
        groups.setdefault(key, []).append(s)
    for g in groups.values():
        g.sort(key=lambda s: s.time)

    plans: list[GroupPlan] = []
    for (host, path), gs in sorted(groups.items()):
        pol = match_policy(path, policies)
        hosts: dict[str, int] = {}
        for s in gs:
            hosts[s.hostname] = hosts.get(s.hostname, 0) + 1
        if len(hosts) == 1:
            host_label = f"host {next(iter(hosts))}"
        else:
            host_label = "hosts " + ", ".join(f"{h} ({n})" for h, n in sorted(hosts.items()))
        gp = GroupPlan(host=host_label, path=path, policy=pol["name"] if pol else None, snaps=gs)

        # Freshness guard (inverted for retired groups: a fresh snapshot is suspicious)
        fresh = (pol or {}).get("fresh_within") or cfg.get("default_fresh_within", "26h")
        newest = gs[-1]
        is_fresh = newest.time >= ago(now, fresh)
        why = is_retired(machine, path, cfg)
        if why:
            gp.notes.append(f"{why}: freshness not checked")
            if is_fresh:
                gp.warns.append(f"marked retired but has a recent snapshot {newest.short_id} ({fmt_age(now, newest.time)}); is it still being backed up?")
        elif not is_fresh:
            gp.fails.append(f"stale: newest snapshot {newest.short_id} is {fmt_age(now, newest.time)} (limit {fresh})")

        # Tag cross-check
        if pol and pol.get("expect_tag"):
            bad = [s for s in gs if pol["expect_tag"] not in s.tags]
            if bad:
                gp.warns.append(f"{len(bad)} snapshot(s) missing tag {pol['expect_tag']!r} (e.g. {bad[-1].short_id} tags={bad[-1].tags})")

        # Retention
        if pol is None:
            keep_ids = {s.id for s in gs}
            gp.warns.append("no policy matches this path; keeping everything")
        elif pol.get("wal_for"):
            sibling = os.path.join(os.path.dirname(path), pol["wal_for"])
            daily = sorted(groups.get((host, sibling), []), key=lambda s: s.time)
            keep_ids, notes, warns = plan_wal(gs, pol, now, daily)
            gp.notes += notes
            gp.warns += warns
            # Floor: never forget WAL newer than the newest daily
            if daily:
                keep_ids |= {s.id for s in gs if s.time >= daily[-1].time}
        else:
            keep_ids, notes = plan_tiered(gs, pol, now)
            gp.notes += notes

        # Floor: always keep the newest N
        keep_ids |= {s.id for s in gs[-keep_last:]} if keep_last > 0 else set()

        gp.keep = [s for s in gs if s.id in keep_ids]
        gp.forget = [s for s in gs if s.id not in keep_ids]
        plans.append(gp)
    return plans


# ── Repo run ─────────────────────────────────────────────────────────────────

@dataclass
class RepoResult:
    name: str
    path: str
    status: str = OK
    lines: list = field(default_factory=list)
    forget_ids: list = field(default_factory=list)


def _worst(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def run_repo(name: str, repo_path: str, password: str, cfg: dict, now: datetime,
             dry_run: bool, no_cap: bool, batch_size: int, machine: str = "") -> RepoResult:
    res = RepoResult(name=name, path=repo_path)
    L = res.lines

    locks, err = restic.list_locks(repo_path, password)
    if locks is None:
        res.status = FAIL
        L.append(f"  FAIL cannot read repo: {err}")
        return res
    if locks:
        res.status = SKIPPED
        L.append(f"  SKIPPED repo is locked ({len(locks)} lock(s): {', '.join(l[:8] for l in locks)}); a backup may be running")
        return res

    snaps, err = restic.list_snapshots_checked(repo_path, password)
    if snaps is None:
        res.status = FAIL
        L.append(f"  FAIL cannot list snapshots: {err}")
        return res
    if not snaps:
        res.status = FAIL
        L.append("  FAIL repo has no snapshots")
        return res

    plans = plan_repo(snaps, cfg, now, machine)
    total = len(snaps)
    forget = [s for gp in plans for s in gp.forget]
    res.forget_ids = [s.id for s in forget]

    for gp in plans:
        st = FAIL if gp.fails else WARN if gp.warns else OK
        res.status = _worst(res.status, st)
        L.append(f"  {gp.path}  [{gp.policy or 'no policy'}]  {gp.host}")
        L.append(f"    snapshots {len(gp.snaps)}, keep {len(gp.keep)}, forget {len(gp.forget)}")
        if gp.keep:
            o, n = gp.keep[0], gp.keep[-1]
            L.append(f"    oldest kept {o.short_id} {fmt_time(o.time)} ({fmt_age(now, o.time)})")
            L.append(f"    newest kept {n.short_id} {fmt_time(n.time)} ({fmt_age(now, n.time)})")
        for m in gp.notes:
            L.append(f"    note: {m}")
        for m in gp.warns:
            L.append(f"    WARN {m}")
        for m in gp.fails:
            L.append(f"    FAIL {m}")
        shown = gp.forget[:20]
        for s in shown:
            L.append(f"    - forget {s.short_id} {fmt_time(s.time)}")
        if len(gp.forget) > len(shown):
            L.append(f"    - ... and {len(gp.forget) - len(shown)} more (full list in the .forgets.txt file)")

    # Deletion cap
    pct = 100.0 * len(forget) / total
    cap = float(cfg.get("max_forget_percent", 20))
    if pct > cap and not no_cap:
        res.status = FAIL
        L.append(f"  FAIL deletion cap: would forget {len(forget)} of {total} ({pct:.0f}% > {cap:.0f}%); rerun with --no-cap if expected")

    # restic check
    ok, out = restic.check(repo_path, password)
    if not ok:
        res.status = FAIL
        tail = "\n".join(out.splitlines()[-10:])
        L.append(f"  FAIL restic check:\n" + "\n".join(f"    {x}" for x in tail.splitlines()))
    else:
        L.append("  check: ok")

    if res.status == FAIL:
        L.append(f"  nothing deleted (guard failed); {len(forget)} snapshot(s) would have been forgotten")
        return res
    if not forget:
        L.append("  nothing to forget")
        return res
    if dry_run:
        L.append(f"  dry-run: would forget {len(forget)} snapshot(s) of {total} ({pct:.0f}%) and prune")
        return res

    ok, out = restic.forget_batch(repo_path, password, res.forget_ids, batch_size)
    warn = restic._fix_repo_ownership(repo_path)
    if warn:
        L.append(f"  {warn}")
        res.status = _worst(res.status, WARN)
    if not ok:
        res.status = FAIL
        L.append("  FAIL forget:\n" + "\n".join(f"    {x}" for x in out.splitlines()))
        return res
    L.append(f"  forgot {len(forget)} snapshot(s)")
    out = restic.prune(repo_path, password)
    if out.startswith("ERROR"):
        res.status = FAIL
        L.append("  FAIL prune:\n" + "\n".join(f"    {x}" for x in out.splitlines()[-10:]))
    else:
        last = [x for x in out.splitlines() if x.strip()][-1:] or ["done"]
        L.append(f"  prune: {last[0].strip()}")
    return res


# ── Report includes ──────────────────────────────────────────────────────────

def run_include(inc: dict, now: datetime, dry_run: bool, retention_days: int) -> tuple[str, list[str]]:
    title = inc.get("title") or inc.get("path", "include")
    path = inc.get("path", "")
    lines = [f"== {title}  ({path})"]
    status = OK
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        lines.append("  nothing new")
        return status, lines

    read_from = path
    if inc.get("truncate") and not dry_run:
        rotated = f"{path}.{now.strftime('%Y%m%d-%H%M%S')}"
        os.rename(path, rotated)
        read_from = rotated
    with open(read_from, "rb") as f:
        size = os.path.getsize(read_from)
        if size > INCLUDE_MAX_BYTES:
            f.seek(size - INCLUDE_MAX_BYTES)
        text = f.read().decode("utf-8", errors="replace")
    if size > INCLUDE_MAX_BYTES:
        text = text.split("\n", 1)[-1]
        lines.append(f"  (showing last {INCLUDE_MAX_BYTES // 1024} KB of {size // 1024} KB)")

    fail_re = re.compile(inc["fail_pattern"], re.M) if inc.get("fail_pattern") else None
    expect_re = re.compile(inc["expect_pattern"], re.M) if inc.get("expect_pattern") else None
    expect_count = int(inc.get("expect_count") or 0)

    # Tally, e.g. "summary: PASS 3 / SKIP 1 / FAIL 0 / results 4 of 4"
    tally = [f"{label} {len(re.findall(rx, text, re.M))}" for label, rx in (inc.get("count_patterns") or {}).items()]
    results = [x.strip() for x in text.splitlines() if expect_re and expect_re.search(x)]
    if expect_re:
        tally.append(f"results {len(results)}" + (f" of {expect_count}" if expect_count else ""))
    if tally:
        lines.append("  summary: " + " / ".join(tally))
    for r in results:
        lines.append(f"    {r}")

    if fail_re and fail_re.search(text):
        status = FAIL
        n = len(fail_re.findall(text))
        lines.append(f"  FAIL {n} line(s) match {inc['fail_pattern']!r}")
    if expect_re:
        if expect_count and len(results) < expect_count:
            status = FAIL
            lines.append(f"  FAIL only {len(results)} of {expect_count} runs reported a result; "
                         f"{expect_count - len(results)} died before finishing (SSH, tunnel, crash?)")
        elif not results:
            status = _worst(status, WARN)
            lines.append(f"  WARN no line matches {inc['expect_pattern']!r}")
        elif expect_count and len(results) > expect_count:
            lines.append(f"  note: {len(results)} results, more than the expected {expect_count} (extra manual run?)")
    if read_from != path:
        lines.append(f"  rotated to {read_from}")
    elif inc.get("truncate"):
        lines.append("  dry-run: not rotated")
    lines.append("  ----")
    lines += [f"  {x}" for x in text.rstrip("\n").splitlines()]

    if inc.get("truncate") and not dry_run:
        _cleanup_rotated(path, now, retention_days)
    return status, lines


def _cleanup_rotated(path: str, now: datetime, retention_days: int) -> None:
    d, base = os.path.split(path)
    pat = re.compile(re.escape(base) + r"\.\d{8}-\d{6}$")
    cutoff = (now - timedelta(days=retention_days)).timestamp()
    for n in os.listdir(d or "."):
        if pat.fullmatch(n):
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass


# ── Email ────────────────────────────────────────────────────────────────────

def load_env(path: str) -> dict:
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes."""
    env: dict = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                env[k.strip()] = v
    except OSError:
        pass
    return env


def send_email(subject: str, body: str, env: dict) -> str:
    """Send the report. Returns '' on success or an error message. Never logs the password."""
    import smtplib
    import ssl
    from email.message import EmailMessage

    need = ["SMTP_HOST", "SMTP_PORT", "SMTP_FROM", "SMTP_TO"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        return f"email not configured (missing {', '.join(missing)})"
    to = [a.strip() for a in env["SMTP_TO"].split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env["SMTP_FROM"]
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    port = int(env["SMTP_PORT"])
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=60, context=ctx)
        else:
            server = smtplib.SMTP(env["SMTP_HOST"], port, timeout=60)
            server.starttls(context=ctx)
        with server:
            if env.get("SMTP_USER"):
                server.login(env["SMTP_USER"], env.get("SMTP_PASS", ""))
            server.send_message(msg, to_addrs=to)
    except Exception as e:
        return f"email failed: {type(e).__name__}: {e}"
    return ""


# ── Main ─────────────────────────────────────────────────────────────────────

def attention_reasons(lines: list[str]) -> list[tuple[str, str]]:
    """Pull FAIL/WARN/SKIPPED reasons out of a section, prefixed with the path they belong to."""
    out, path = [], ""
    for x in lines:
        m = re.match(r"^  (/\S*)  \[", x)
        if m:
            path = m.group(1)
            continue
        t = x.strip()
        if t.startswith(("FAIL", "WARN", "SKIPPED")):
            level, _, rest = t.split("\n", 1)[0].rstrip(":").partition(" ")
            level = level.rstrip(":")
            level = WARN if level.startswith("WARN") else level  # 'WARNING:' from the ownership fix
            indented = x.startswith("    ")
            out.append((level, f"{path}: {rest}" if indented and path else rest))
    return out


def load_prune_config(cfg: dict) -> dict:
    merged = dict(DEFAULT_PRUNE)
    merged.update(cfg.get("prune") or {})
    if "report_includes" in cfg and "report_includes" not in (cfg.get("prune") or {}):
        merged["report_includes"] = cfg["report_includes"]
    return merged


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply retention policies, prune, and write a report.")
    ap.add_argument("--dry-run", action="store_true", help="report only; change nothing")
    ap.add_argument("--no-cap", action="store_true", help="disable the deletion cap (initial cleanup)")
    ap.add_argument("--machine", help="only this machine (name from the clients file)")
    ap.add_argument("--no-email", action="store_true", help="write the report but do not email it")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args(argv)

    import yaml
    with open(args.config) as f:
        cfg = yaml.safe_load(f) or {}
    pcfg = load_prune_config(cfg)
    password = cfg.get("restic_password", "")
    batch = int(cfg.get("delete_batch_size", 50))
    now = datetime.now(timezone.utc)

    machines = restic.load_machines(cfg.get("clients_file", ""))
    if args.machine:
        machines = [m for m in machines if m.name.lower() == args.machine.lower()]
        if not machines:
            print(f"No machine named {args.machine!r} in the clients file", file=sys.stderr)
            return 2

    counts = {FAIL: 0, WARN: 0, SKIPPED: 0, OK: 0}
    body: list[str] = []
    all_forgets: list[str] = []
    attention: list[str] = []
    include_summaries: list[str] = []

    for m in machines:
        repos = restic.discover_repos(m.repo_path)
        if not repos:
            counts[FAIL] += 1
            why = f"no repos found: {restic.explain_no_repos(m.repo_path)}"
            body += [f"== {m.name}  ({m.repo_path})  FAIL", f"  {why}", ""]
            attention.append(f"FAIL     {m.name}: {why}")
            continue
        for label, rpath in repos:
            name = f"{m.name} / {label}"
            try:
                r = run_repo(name, rpath, password, pcfg, now, args.dry_run, args.no_cap, batch, m.name)
            except Exception:
                r = RepoResult(name=name, path=rpath, status=FAIL,
                               lines=["  FAIL unexpected error:"] + [f"    {x}" for x in traceback.format_exc().splitlines()])
            counts[r.status] += 1
            body += [f"== {name}  ({rpath})  {r.status}"] + r.lines + [""]
            if r.status != OK:
                attention += [f"{lvl:<8} {name}: {why}" for lvl, why in attention_reasons(r.lines)]
            all_forgets += [f"{name}\t{sid}" for sid in r.forget_ids]

    for inc in pcfg.get("report_includes") or []:
        try:
            st, lines = run_include(inc, now, args.dry_run, int(pcfg.get("include_retention_days", 30)))
        except Exception as e:
            st, lines = FAIL, [f"== {inc.get('title', inc.get('path'))}", f"  FAIL could not read include: {e}"]
        counts[st] += 1
        body += lines + [""]
        title = inc.get("title") or inc.get("path")
        summary = next((x.strip() for x in lines if x.startswith("  summary: ")), "")
        if summary:
            include_summaries.append(f"{st:<8} {title}: {summary[len('summary: '):]}")
        if st != OK:
            attention += [f"{lvl:<8} {title}: {why}" for lvl, why in attention_reasons(lines)]

    flags = [x for x, on in (("dry-run", args.dry_run), ("no-cap", args.no_cap)) if on]
    header = f"[restic] FAIL {counts[FAIL]} / WARN {counts[WARN]} / SKIPPED {counts[SKIPPED]} / OK {counts[OK]}"
    if flags:
        header += f" ({', '.join(flags)})"
    top = ["Summary", "-------"]
    top += ["Needs attention:"] + [f"  {x}" for x in attention] if attention else ["Everything OK."]
    if include_summaries:
        top += ["Report includes:"] + [f"  {x}" for x in include_summaries]
    top += ["", "Details", "-------", ""]
    report = "\n".join([header, f"run at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC", ""] + top + body)

    email_err = ""
    if args.no_email:
        report += "\nemail: skipped (--no-email)"
    else:
        env = load_env(pcfg.get("env_file", "/mnt/restic_tui/.env"))
        env.update({k: v for k, v in os.environ.items() if k.startswith("SMTP_")})
        email_err = send_email(header, report, env)
        report += f"\nemail: {email_err or 'sent to ' + env.get('SMTP_TO', '')}"

    os.makedirs(pcfg["report_dir"], exist_ok=True)
    stem = now.strftime("%Y-%m-%d_%H%M%S") + ("-dry-run" if args.dry_run else "")
    report_path = os.path.join(pcfg["report_dir"], f"{stem}.txt")
    with open(report_path, "w") as f:
        f.write(report + "\n")
    if all_forgets:
        with open(os.path.join(pcfg["report_dir"], f"{stem}.forgets.txt"), "w") as f:
            f.write("\n".join(all_forgets) + "\n")
    with open(pcfg["last_report"], "w") as f:
        f.write(report + "\n")

    print(report)
    print(f"\nReport written to {report_path}")
    if email_err:
        print(email_err, file=sys.stderr)
    return 1 if counts[FAIL] or (email_err and not email_err.startswith("email not configured")) else 0


if __name__ == "__main__":
    sys.exit(main())
