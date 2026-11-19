#!/bin/bash
# Copy the repo to the VM (never the VM's secrets or run data).  Usage: TASKFORGE_VM=user@host deploy/sync.sh
set -euo pipefail
VM="${TASKFORGE_VM:?set TASKFORGE_VM=user@host}"
DEST="${TASKFORGE_VM_DIR:-/root/work/taskforge}"
cd "$(dirname "$0")/.."
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude '*.egg-info' --exclude .git \
  --exclude runs --exclude deploy/.env --exclude deploy/s3.json ./ "$VM:$DEST/"
