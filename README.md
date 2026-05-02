# Restic TUI

A terminal UI for managing [Restic](https://restic.net/) backup snapshots across multiple machines. Built with Python/Textual, runs in Docker.

## What it does

- **List snapshots** for any machine with date range filtering
- **Browse snapshot contents** — full file tree with size, modified date, sortable
- **Restore locally** — to a staging directory on the backup server
- **Push restore to client** — SSHFS mount to remote machine, restore directly, unmount
- **Forget** (delete) snapshots in bulk with confirmation
- **Prune** unreferenced data from repos
- **Stats** — repo size, file count, snapshot count

## Architecture

The TUI runs on the backup server with direct filesystem access to all restic repositories. No tunnels needed for browse/list/delete/prune. For push-to-client restores, it uses SSHFS to mount the target machine's filesystem.

```
┌──────────────────────────────────────────────┐
│  restic-tui container (privileged)           │
│  Python/Textual TUI                          │
│  ├── Reads machine list from clients file    │
│  ├── Calls restic CLI directly               │
│  ├── Accesses repos via mounted volumes      │
│  └── SSHFS to clients for push restores      │
└──────────────────────────────────────────────┘
        │               │              │
   Repo volumes    SSH keys (ro)   SSHFS to clients
        │                              │
   /mnt/BACKUPS/secure/           root@<machine>:/mnt/restic.restore
```

## Requirements

- Docker + Docker Compose
- Restic (included in container image)
- SSHFS (included in container image)
- SSH key access to client machines (for push restores)
- Direct filesystem access to restic repositories

## Quick Start

On the backup server:

```bash
# Pull deploy script from NAS and run it
scp user@nas:/home/user/restic-tui/deploy_from_nas.sh .
sh deploy_from_nas.sh

# Edit config and clients
vi /mnt/restic_tui/config.yaml
vi /mnt/restic_tui/clients

# Run
cd ~/restic-tui
docker compose run --rm restic-tui
```

## File Layout

```
~/restic-tui/                  # Build files
  ├── Dockerfile
  ├── docker-compose.yaml
  ├── app.py                   # TUI application
  ├── restic_wrapper.py        # Restic CLI wrapper
  ├── requirements.txt
  └── deploy_from_nas.sh       # Deployment script

/mnt/restic_tui/               # Runtime data (mounted rw)
  ├── config.yaml              # Config
  ├── clients                  # Machine list
  ├── restores/                # Local restore staging
  ├── restore.remote/          # SSHFS mount points (auto-managed)
  └── last_output.log          # Last command output
```

## Configuration

`/mnt/restic_tui/config.yaml`:

```yaml
clients_file: /mnt/restic_tui/clients
restic_password: ""
restore_dir: /mnt/restic_tui/restores
delete_batch_size: 50
```

## Clients File

Four lines per machine, blank line separator:

```
WEB
bu_webserver1
ssh -R 8000:127.0.0.1:22 -p 8022 -i /path/to/key root@gateway
/mnt/BACKUPS/secure/bu_webserver1/repo

NAS
bu_nas
ssh -R 8000:127.0.0.1:22 -i /path/to/key root@nas
/mnt/BACKUPS/secure/bu_nas/repo
```

Fields: friendly name, backup username, SSH connect command, repo path.

The SSH connect command is used for push-to-client restores (reverse tunnel flags are automatically stripped for SSHFS).

## Docker Mounts

| Host | Container | Mode | Purpose |
|------|-----------|------|---------|
| `/mnt/restic_tui` | `/mnt/restic_tui` | rw | Config, restores, mount points |
| `/mnt/BACKUPS` | `/mnt/BACKUPS` | rw | Restic repositories |
| `/home/user/.ssh` | `/home/user/.ssh` | ro | SSH keys for push restores |

Container runs as root (required for accessing repos owned by different `bu_*` users) and privileged (required for FUSE/SSHFS).

## Keybindings

### Main screen

| Key | Action |
|-----|--------|
| `l` | Load snapshots |
| `s` | Toggle sort (newest/oldest first) |
| `m` | Mark/unmark snapshot (advances cursor) |
| `a` | Mark all visible |
| `u` | Unmark all |
| `d` | Forget marked snapshots (with confirmation) |
| `p` | Prune repo (with confirmation) |
| `b` | Browse snapshot contents |
| `i` | Show repo stats |
| `q` | Quit |

### Browse screen

| Key | Action |
|-----|--------|
| `r` | Restore selected file/folder |
| `1` | Sort by name |
| `2` | Sort by size |
| `3` | Sort by modified date |
| `Esc` | Back to snapshot list |

### Restore options

- **Local Restore** — restore to staging directory on backup server
- **Push to Client** — SSHFS mount remote machine, restore directly, unmount
- **Overwrite policy** — always (default), if-changed, if-newer, never
- **Dry run** — preview what would be restored without writing

## Push-to-Client Restore Flow

1. Mounts remote machine via SSHFS at `/mnt/restic.restore`
2. Checks for existing files in target directory
3. If files exist: shows warning, waits for user confirmation
4. Verifies mount is still active before restoring
5. Runs restic restore with selected overwrite policy
6. Unmounts remote filesystem
7. Writes full log to `last_output.log`

Stale SSHFS mounts from previous runs are automatically cleaned up on app startup.

## Disaster Recovery Workflow

1. Fresh Linux install on replacement machine
2. Paste SSH public key into `/root/.ssh/authorized_keys`
3. From backup server TUI: select machine, browse latest snapshot
4. Push restore `/root`, `/home`, `/mnt` to the new machine
5. SSH in, follow the restored build sheet to reconfigure services

## Platform

- Raspberry Pi (aarch64), Debian
- Docker + Docker Compose
- Python 3.12, Textual 1.0.0, Restic 0.18.0
