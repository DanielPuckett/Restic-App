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


def _run(repo_path: str, password: str, args: list[str], timeout: int = 600, json_output: bool = False) -> subprocess.CompletedProcess:
    """Run a restic command against a local repo."""
    env = os.environ.copy()
    env["RESTIC_PASSWORD"] = password
    cmd = ["restic", "--repo", repo_path]
    if json_output:
        cmd.append("--json")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def list_snapshots(repo_path: str, password: str) -> list[Snapshot]:
    """List all snapshots in a repo."""
    result = _run(repo_path, password, ["snapshots"], json_output=True)
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
    result = _run(repo_path, password, ["ls", snapshot_id], json_output=True)
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
    r = _run(repo_path, password, ["stats"], json_output=True)
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def restore(repo_path: str, password: str, snapshot_id: str, target: str, include: str | None = None, extra_args: list[str] | None = None) -> str:
    """Restore a snapshot or specific path."""
    args = ["restore", snapshot_id, "--target", target]
    if include:
        args.extend(["--include", include])
    if extra_args:
        args.extend(extra_args)
    r = _run(repo_path, password, args, timeout=1800)
    cmd_info = f"\nCOMMAND: restic --repo {repo_path} {' '.join(args)}"
    if r.returncode != 0:
        return f"ERROR: {r.stderr}\n{r.stdout}{cmd_info}"
    return (r.stdout or "Restore completed.") + cmd_info
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


REMOTE_MOUNT_BASE = "/mnt/restic_tui/restore.remote"


def check_sshfs_installed() -> bool:
    """Check if sshfs is available."""
    r = subprocess.run(["which", "sshfs"], capture_output=True)
    return r.returncode == 0


def parse_ssh_string(ssh_cmd: str) -> tuple[str, str | None]:
    """Extract user@host and identity file from SSH connect string.
    Strips -R reverse tunnel args. Returns (user@host, identity_file)."""
    parts = ssh_cmd.split()
    user_host = None
    identity_file = None
    skip_next = False
    i = 0
    while i < len(parts):
        p = parts[i]
        if skip_next:
            skip_next = False
            i += 1
            continue
        if p == "ssh":
            i += 1
            continue
        if p == "-R":
            skip_next = True  # skip the tunnel spec
            i += 1
            continue
        if p == "-p":
            skip_next = True  # skip port (gateway port, not needed for direct sshfs)
            i += 1
            continue
        if p == "-i":
            if i + 1 < len(parts):
                identity_file = parts[i + 1]
                skip_next = True
            i += 1
            continue
        # Remaining non-flag arg is user@host
        if not p.startswith("-"):
            user_host = p
        i += 1
    return user_host, identity_file


def sshfs_mount(machine: Machine, remote_path: str) -> tuple[bool, str]:
    """Mount a remote path via SSHFS. Returns (success, message)."""
    user_host, identity_file = parse_ssh_string(machine.ssh_cmd)
    if not user_host:
        return False, "Could not parse user@host from SSH string"

    local_mount = f"{REMOTE_MOUNT_BASE}/{machine.name}"
    os.makedirs(local_mount, exist_ok=True)

    # Check if already mounted
    r = subprocess.run(["mountpoint", "-q", local_mount])
    if r.returncode == 0:
        return True, f"Already mounted at {local_mount}"

    # Create remote directory via SSH
    ssh_args = ["ssh"]
    if identity_file:
        ssh_args.extend(["-i", identity_file])
    ssh_args.extend(["-o", "StrictHostKeyChecking=no", user_host, f"mkdir -p {remote_path}"])
    r = subprocess.run(ssh_args, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, f"Failed to create remote dir: {r.stderr}"

    # SSHFS mount
    sshfs_args = ["sshfs"]
    if identity_file:
        sshfs_args.extend(["-o", f"IdentityFile={identity_file}"])
    sshfs_args.extend(["-o", "StrictHostKeyChecking=no", f"{user_host}:{remote_path}", local_mount])
    r = subprocess.run(sshfs_args, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return False, f"SSHFS mount failed: {r.stderr}"

    # Verify
    r = subprocess.run(["mountpoint", "-q", local_mount])
    if r.returncode != 0:
        return False, "Mount verification failed"

    return True, local_mount


def sshfs_unmount(machine_name: str) -> str:
    """Unmount SSHFS for a machine."""
    local_mount = f"{REMOTE_MOUNT_BASE}/{machine_name}"
    r = subprocess.run(["mountpoint", "-q", local_mount])
    if r.returncode != 0:
        return "Not mounted"
    r = subprocess.run(["fusermount", "-u", local_mount], capture_output=True, text=True)
    if r.returncode != 0:
        return f"Unmount failed: {r.stderr}"
    try:
        os.rmdir(local_mount)
    except OSError:
        pass
    return "Unmounted"


def cleanup_stale_mounts() -> list[str]:
    """Clean up any stale SSHFS mounts from previous runs."""
    results = []
    if not os.path.exists(REMOTE_MOUNT_BASE):
        return results
    for name in os.listdir(REMOTE_MOUNT_BASE):
        mount_path = f"{REMOTE_MOUNT_BASE}/{name}"
        if not os.path.isdir(mount_path):
            continue
        r = subprocess.run(["mountpoint", "-q", mount_path])
        if r.returncode == 0:
            subprocess.run(["fusermount", "-u", mount_path], capture_output=True)
            results.append(f"Unmounted stale: {mount_path}")
        try:
            os.rmdir(mount_path)
            results.append(f"Removed: {mount_path}")
        except OSError:
            pass
    return results


def check_remote_contents(local_mount: str) -> bool:
    """Check if the remote mount point has any existing files."""
    try:
        entries = os.listdir(local_mount)
        return len(entries) > 0
    except OSError:
        return False
