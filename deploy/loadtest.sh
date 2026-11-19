#!/bin/bash
# P4 load test and failure drills, on fake models only.
#   deploy/loadtest.sh ramp [--levels 16,32,64,128] [--rounds 6]
#   deploy/loadtest.sh drill [NAME ...]  failure drills (deploy/loadtest.py DRILLS); default: all of them
#   deploy/loadtest.sh cli ARGS...       any taskforge command in the load-test environment
# Uses the running stack (Postgres, S3, sandboxd) but its own database schema `loadtest`, and starts its own
# worker processes. No API key is ever in this environment, so nothing here can call a paid model.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
set -a; . deploy/.env; set +a
for v in $(env | grep -o '^[A-Z_]*_API_KEY'); do unset "$v"; done
export TASKFORGE_PG_DSN="postgresql://taskforge:${TASKFORGE_PG_PASSWORD}@127.0.0.1:54329/taskforge"
export TASKFORGE_PG_SCHEMA=loadtest
export TASKFORGE_BLOBS="s3://taskforge@127.0.0.1:59000"
export TASKFORGE_S3_KEY=taskforge
export TASKFORGE_SANDBOX="${TASKFORGE_SANDBOX:-http://127.0.0.1:58700}"
export TASKFORGE_OTLP="http://127.0.0.1:54318"
export TASKFORGE_PROFILE=loadtest
export TASKFORGE_FAULTS="${TASKFORGE_FAULTS:-$ROOT/runs/loadtest/faults.json}"
mkdir -p runs/loadtest
case "${1:-}" in
  ramp|drill) exec "$ROOT/.venv/bin/python" deploy/loadtest.py "$@";;
  cli)  shift; exec "$ROOT/.venv/bin/python" -m taskforge.cli "$@";;
  *) echo "usage: $0 ramp [--levels ...] [--rounds N] | drill [NAME ...] | cli ARGS..."; exit 2;;
esac
