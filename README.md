# Restic TUI

A terminal UI for managing [Restic](https://restic.net/) backup snapshots across multiple machines. Built with Python/Textual, runs in Docker.

## What it does

- **List snapshots** for any machine with date range filtering
- **Browse snapshot contents** — full file tree with size, modified date
- **Restore** full snapshots or individual files/folders
- **Forget** (delete) snapshots in bulk with confirmation
- **Prune** unreferenced data from repos
- **Stats** — repo size, file count, snapshot count

## Architecture

The TUI runs on the backup server with direct filesystem access to all restic repositories. No tunnels needed for browse/list/delete/prune — those operate on local repo paths. Restores go to a local staging directory.

```
┌──────────────────────────────────────────────┐
│  restic-tui container                        │
│  Python/Textual TUI                          │
│  ├── Reads machine list from clients file    │
│  ├── Calls restic CLI directly               │
│  └── Accesses repos via mounted volumes      │
└──────────────────────────────────────────────┘
        │
   Volume mounts to repo directories
        │
   /mnt/BACKUPS/secure/bu_<machine>/repo
```

## Requirements

- Docker + Docker Compose
- Restic installed in the container (included in Dockerfile)
- Direct filesystem access to restic repositories

## File Layout

```
~/restic-tui/                  # Build files
  ├── Dockerfile
  ├── docker-compose.yaml
  ├── app.py                   # TUI application
  ├── restic_wrapper.py        # Restic CLI wrapper
  └── requirements.txt

/mnt/restic_tui/               # Runtime data (mounted rw)
  ├── config.yaml              # Config (copy from config.sample.yaml)
  ├── clients                  # Machine list
  ├── restores/                # Restore staging area
  └── last_output.log          # Last command output for debugging
```

## Configuration

Copy `config.sample.yaml` to `/mnt/restic_tui/config.yaml`:

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

Fields: friendly name, backup username, SSH connect command (for future restore push), repo path.

## Usage

```bash
cd ~/restic-tui
docker compose build
docker compose run --rm restic-tui
```

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

## Docker Mounts

Adjust `docker-compose.yaml` volumes to match your repo locations:

```yaml
volumes:
  - /mnt/restic_tui:/mnt/restic_tui
  - /mnt/BACKUPS:/mnt/BACKUPS
```

## Platform

- Raspberry Pi (aarch64), Debian
- Docker + Docker Compose
- Python 3.12, Textual 1.0.0, Restic 0.18.0
