#!/bin/bash
# Deploy restic-tui from NAS to backup server
# Run as: sh deploy_from_nas.sh

set -e

NAS="user@nas"
SRC="/home/user/restic-tui"
DEST="$HOME/restic-tui"
CONF="/mnt/restic_tui"

echo "Creating directories..."
mkdir -p "$DEST"
sudo mkdir -p "$CONF/restores"
sudo chown -R $(whoami):$(whoami) "$CONF"

echo "Pulling build files from NAS..."
scp "$NAS:$SRC/Dockerfile" "$DEST/"
scp "$NAS:$SRC/docker-compose.yaml" "$DEST/"
scp "$NAS:$SRC/requirements.txt" "$DEST/"
scp "$NAS:$SRC/restic_wrapper.py" "$DEST/"
scp "$NAS:$SRC/app.py" "$DEST/"

echo "Pulling config and clients..."
[ -f "$CONF/config.yaml" ] && echo "  config.yaml exists, skipping." || scp "$NAS:$CONF/config.yaml" "$CONF/"
[ -f "$CONF/clients" ] && echo "  clients exists, skipping." || scp "$NAS:$CONF/clients" "$CONF/"

echo "Updating docker-compose mounts for backup server..."
cat > "$DEST/docker-compose.yaml" << 'EOF'
services:
  restic-tui:
    build: .
    container_name: restic-tui
    stdin_open: true
    tty: true
    privileged: true
    volumes:
      - /mnt/restic_tui:/mnt/restic_tui
      - /mnt/BACKUPS:/mnt/BACKUPS
      - /home/user/.ssh:/home/user/.ssh:ro
    environment:
      - TERM=xterm-256color
EOF

echo "Building Docker image..."
cd "$DEST"
docker compose build

echo ""
echo "Done. Edit /mnt/restic_tui/config.yaml with the correct restic password."
echo "Edit /mnt/restic_tui/clients with your machine list."
echo "Run with: cd ~/restic-tui && docker compose run --rm restic-tui"
