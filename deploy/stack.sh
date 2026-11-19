#!/bin/bash
# Start / stop the TaskForge stack on one machine.
#   deploy/stack.sh up [N]     backing services (Docker Compose) + sandboxd + api + N worker processes (default 2)
#   deploy/stack.sh down       stop the host processes (backing services keep running; add --all to stop those too)
#   deploy/stack.sh status
#   deploy/stack.sh env        print the environment the host processes use (source it to run the CLI by hand)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd); RUN="$ROOT/runs/stack"; mkdir -p "$RUN"
PY="$ROOT/.venv/bin/python"

if [ ! -f deploy/.env ]; then
  umask 077
  { echo "TASKFORGE_PG_PASSWORD=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
    echo "TASKFORGE_S3_SECRET=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"; } > deploy/.env
fi
set -a; . deploy/.env; set +a
if [ ! -f deploy/s3.json ]; then
  umask 077
  printf '{"identities":[{"name":"taskforge","credentials":[{"accessKey":"taskforge","secretKey":"%s"}],"actions":["Admin","Read","Write","List","Tagging"]}]}\n' "$TASKFORGE_S3_SECRET" > deploy/s3.json
  chmod 644 deploy/s3.json     # the container's user must be able to read it; the port is localhost-only
fi
export TASKFORGE_PG_DSN="postgresql://taskforge:${TASKFORGE_PG_PASSWORD}@127.0.0.1:54329/taskforge"
export TASKFORGE_BLOBS="s3://taskforge@127.0.0.1:59000"
export TASKFORGE_S3_KEY=taskforge
export TASKFORGE_SANDBOX="http://127.0.0.1:58700"
export TASKFORGE_OTLP="http://127.0.0.1:54318"
export TASKFORGE_PROFILE="${TASKFORGE_PROFILE:-demo}"
if [ "$TASKFORGE_PROFILE" = "pilot" ]; then          # paid models: keys from the machine's secrets file
  set -a; . "${TASKFORGE_SECRETS:-$HOME/.secrets.env}"; set +a
  export TASKFORGE_CAPS="${TASKFORGE_CAPS:?set TASKFORGE_CAPS, e.g. anthropic=30,openrouter=15}"
fi

start() {  # name, command...
  local name=$1; shift
  if [ -f "$RUN/$name.pid" ] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then echo "$name already running"; return; fi
  nohup "$@" > "$RUN/$name.log" 2>&1 &
  echo $! > "$RUN/$name.pid"; echo "started $name (pid $!)"
}
stop_all() {
  for f in "$RUN"/*.pid; do [ -e "$f" ] || continue
    pid=$(cat "$f"); kill "$pid" 2>/dev/null && echo "stopped $(basename "$f" .pid)"; rm -f "$f"; done
}

case "${1:-status}" in
  up)
    docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --wait postgres
    docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
    "$PY" -m taskforge.cli db init
    start sandboxd "$PY" -m taskforge.cli sandboxd
    start api "$PY" -m taskforge.cli api
    for i in $(seq 1 "${2:-2}"); do start "worker$i" "$PY" -m taskforge.cli worker; done
    ;;
  down)
    stop_all
    if [ "${2:-}" = "--all" ]; then docker compose -f deploy/docker-compose.yml --env-file deploy/.env down; fi
    ;;
  status)
    for f in "$RUN"/*.pid; do [ -e "$f" ] || continue
      n=$(basename "$f" .pid); kill -0 "$(cat "$f")" 2>/dev/null && echo "$n running" || echo "$n DEAD"; done
    docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps --format '{{.Service}} {{.State}}' 2>/dev/null || true
    ;;
  env)
    env | grep '^TASKFORGE_' | grep -v -e PASSWORD -e SECRET -e _KEY | sed 's#\(postgresql://taskforge:\)[^@]*@#\1***@#'
    ;;
  *) echo "usage: $0 up [N] | down [--all] | status | env"; exit 2;;
esac
