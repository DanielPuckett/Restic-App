"""Wrapper around restic CLI with JSON output."""
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Machine:
    name: str
    bu_user: str
    ssh_cmd: str
    repo_path: str


@dataclass
class Snapshot:
    id: str
    short_id: str
    time: datetime
    paths: list[str]
    hostname: str
    username: str


@dataclass
class Node:
    name: str
    type: str  # "dir" or "file"
    path: str
    size: int
    mtime: str
    permissions: str


def load_machines(clients_file: str) -> list[Machine]:
    """Parse the clients file into Machine objects."""
    machines = []
    with open(clients_file) as f:
        lines = [l.rstrip("\n") for l in f.readlines()]
    i = 0
    while i < len(lines):
        # Skip blank lines
        if not lines[i].strip():
            i += 1
            continue
        if i + 3 >= len(lines):
            break
        machines.append(Machine(
            name=lines[i].strip(),
            bu_user=lines[i + 1].strip(),
            ssh_cmd=lines[i + 2].strip(),
            repo_path=lines[i + 3].strip(),
        ))
        i += 4
    return machines


def _run(repo_path: str, password: str, args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a restic command against a local repo."""
    env = os.environ.copy()
    env["RESTIC_PASSWORD"] = password
    cmd = ["restic", "--repo", repo_path, "--json"] + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def list_snapshots(repo_path: str, password: str) -> list[Snapshot]:
    """List all snapshots in a repo."""
    result = _run(repo_path, password, ["snapshots"])
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    snapshots = []
    for s in data:
        try:
            t = datetime.fromisoformat(s["time"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        snapshots.append(Snapshot(
            id=s.get("id", ""),
            short_id=s.get("short_id", ""),
            time=t,
            paths=s.get("paths", []),
            hostname=s.get("hostname", ""),
            username=s.get("username", ""),
        ))
    return snapshots


def ls_snapshot(repo_path: str, password: str, snapshot_id: str) -> list[Node]:
    """List files in a snapshot."""
    result = _run(repo_path, password, ["ls", snapshot_id])
    if result.returncode != 0:
        return []
    nodes = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("struct_type") != "node":
            continue
        nodes.append(Node(
            name=obj.get("name", ""),
            type=obj.get("type", ""),
            path=obj.get("path", ""),
            size=obj.get("size", 0),
            mtime=obj.get("mtime", ""),
            permissions=obj.get("permissions", ""),
        ))
    return nodes


def forget_snapshots(repo_path: str, password: str, ids: list[str]) -> str:
    """Forget (delete) snapshots by ID."""
    results = []
    for sid in ids:
        r = _run(repo_path, password, ["forget", sid])
        if r.returncode != 0:
            results.append(f"ERROR forgetting {sid}: {r.stderr}")
        else:
            results.append(f"Forgot {sid}")
    return "\n".join(results)


def prune(repo_path: str, password: str) -> str:
    """Prune unreferenced data from the repo."""
    r = _run(repo_path, password, ["prune"], timeout=1800)
    if r.returncode != 0:
        return f"ERROR: {r.stderr}\n{r.stdout}"
    return r.stdout or "Prune completed."


def stats(repo_path: str, password: str) -> dict:
    """Get repo stats."""
    r = _run(repo_path, password, ["stats"])
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def restore(repo_path: str, password: str, snapshot_id: str, target: str, include: str | None = None) -> str:
    """Restore a snapshot or specific path."""
    args = ["restore", snapshot_id, "--target", target]
    if include:
        args.extend(["--include", include])
    r = _run(repo_path, password, args, timeout=1800)
    if r.returncode != 0:
        return f"ERROR: {r.stderr}\n{r.stdout}"
    return r.stdout or "Restore completed."


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} K"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024**2:.1f} M"
    else:
        return f"{size_bytes / 1024**3:.1f} G"
