#!/usr/bin/env bash
# Invoked by lightsail.sh on the existing Lightsail Docker host.
set -euo pipefail
mode=$1
[[ $mode == dashboard || $mode == live ]]
revision=$2
if [[ $revision == registry ]]; then
  upload=$(mktemp -d /tmp/toss-deploy.XXXXXXXX)
else
  upload=$3
  [[ $revision =~ ^[a-f0-9]{40}$ && $upload =~ ^/tmp/toss-deploy\.[a-zA-Z0-9]+$ ]]
fi
trap 'rm -rf -- "$upload"' EXIT

docker() { sudo -n docker "$@"; }
fail() { echo "$*" >&2; exit 1; }
root="$HOME/toss-trading-releases"
mkdir -p "$root"
command -v flock >/dev/null || fail "flock is required on the Lightsail host."
exec 9>"$root/.deploy.lock"
flock -n 9 || fail "Another Toss Trading deployment is running."

if [[ $mode == dashboard ]]; then
  service=api
  project=toss-trading
  current=toss-trading-api-1
  env_args=(--env-file "$HOME/toss-trading/.env")
else
  service=live
  project=toss-trading-live
  current=toss-trading-live-live-1
  env_args=()
fi

test "$(docker inspect "$current" --format '{{.State.Running}}')" = true ||
  fail "The existing $current container must be running."
old_dir=$(docker inspect "$current" --format \
  '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')
old_files=$(docker inspect "$current" --format \
  '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
[[ -d $old_dir && -n $old_files ]]
IFS=, read -ra files <<< "$old_files"
old_args=()
base_args=()
for file in "${files[@]}"; do
  test -f "$file"
  old_args+=(-f "$file")
  case "$file" in
    */.toss-deploy-image.yaml|*/compose.dashboard-live.yaml|*/.rollback-*.yaml) ;;
    *) base_args+=(-f "$file") ;;
  esac
done
docker volume inspect toss-trading-live_live_data >/dev/null

old_id=$(docker inspect "$current" --format '{{.Image}}')
if [[ $revision == registry ]]; then
  registry_image="ghcr.io/yongtaii/toss-trading:$mode"
  docker pull "$registry_image"
  new_id=$(docker image inspect "$registry_image" --format '{{.Id}}')
  [[ $new_id != "$old_id" ]] || exit 0
  revision=$(docker image inspect "$new_id" --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}}')
  [[ $revision =~ ^[a-f0-9]{40}$ ]] || fail "Registry image lacks a valid Git revision."
  image="toss-trading:registry-${new_id:7}"
  docker tag "$new_id" "$image"
  release="$root/registry-$mode-${new_id:7}"
  mkdir -p "$release"
  [[ ! -f "$release/.cutover-failed" ]] ||
    fail "This image previously failed after cutover. Review logs before retrying."
  registry=true
else
  registry=false
  image="toss-trading:main-${revision:0:7}"
  release="$root/deploy-$mode-$revision"
  mkdir "$upload/source"
  tar -xzf "$upload/source.tar.gz" -C "$upload/source"
  docker build --label "org.opencontainers.image.revision=$revision" -t "$image" "$upload/source"
  if [[ ! -d $release ]]; then
    mv "$upload/source" "$release"
  fi
fi
short=${revision:0:7}
rollback_image="toss-trading:rollback-$mode-${old_id:7:12}"
docker tag "$old_id" "$rollback_image"
printf 'services:\n  %s:\n    image: %s\n' "$service" "$rollback_image" > "$upload/rollback.yaml"

printf 'services:\n  %s:\n    image: %s\n' "$service" "$image" > "$release/.toss-deploy-image.yaml"

if [[ $registry == true ]]; then
  # Image-only updates retain the installed configuration and bind mounts.
  workdir=$old_dir
  deploy_args=()
  for file in "${files[@]}"; do
    case "$file" in
      */.toss-deploy-image.yaml|*/.rollback-*.yaml) ;;
      *) deploy_args+=(-f "$file") ;;
    esac
  done
  if [[ $mode == dashboard ]]; then
    mount=$(docker inspect "$current" --format \
      '{{range .Mounts}}{{if eq .Destination "/app/live-status"}}{{.Name}}:{{.RW}}{{end}}{{end}}')
    [[ $mount == toss-trading-live_live_data:false ]] ||
      fail "The existing dashboard must have the read-only LIVE volume mounted."
  fi
elif [[ $mode == dashboard ]]; then
  workdir=$old_dir
  deploy_args=("${base_args[@]}" -f "$release/compose.dashboard-live.yaml")
else
  [[ ${#base_args[@]} == 2 && ${base_args[1]} == "$old_dir/compose.live.yaml" ]] ||
    fail "Unexpected LIVE Compose overrides; review before replacing the worker."
  volume=$(docker inspect "$current" --format \
    '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}')
  test "$volume" = toss-trading-live_live_data || fail "Unexpected LIVE volume: $volume"
  if [[ $release != "$old_dir" ]]; then
    install -m 600 "$old_dir/.env.live" "$release/.env.live"
    install -m 600 "$old_dir/config/live.toml" "$release/config/live.toml"
    group=$(docker run --rm --entrypoint id "$image" -g)
    sudo -n chgrp "$group" "$release/config/live.toml"
    chmod 640 "$release/config/live.toml"
  fi
  workdir=$release
  deploy_args=(-f "$release/compose.live.yaml")
fi

compose() {
  docker compose -p "$project" --project-directory "$workdir" ${env_args[@]+"${env_args[@]}"} \
    "${deploy_args[@]}" -f "$release/.toss-deploy-image.yaml" "$@"
}
rollback() {
  echo "Verification failed. Restoring $rollback_image." >&2
  if [[ $registry == true ]]; then
    touch "$release/.cutover-failed"
  fi
  # Keep the rollback file at a stable path referenced by container labels.
  rollback_file="$root/.rollback-$mode.yaml"
  cp "$upload/rollback.yaml" "$rollback_file"
  docker compose -p "$project" --project-directory "$old_dir" ${env_args[@]+"${env_args[@]}"} \
    "${old_args[@]}" -f "$rollback_file" up -d --no-deps --no-build --pull never "$service"
}

check_dashboard() {
  docker exec -i "$1" /app/.venv/bin/python - <<'PY'
import base64
import json
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from toss_trading.config import Settings

settings = Settings()
password = settings.dashboard_password.get_secret_value()
auth = base64.b64encode(f"{settings.dashboard_username}:{password}".encode()).decode()

def read(path):
    request = Request(
        "http://127.0.0.1:8000" + path, headers={"Authorization": "Basic " + auth}
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)

for attempt in range(10):
    try:
        if read("/health")["status"] == "ok":
            break
    except (URLError, OSError):
        pass
    time.sleep(2)
else:
    raise SystemExit("API health check failed")
data = read("/dashboard/data")
status = read("/dashboard/live-status")
if data["mode"] != "LIVE" or status["worker"] != "running":
    raise SystemExit(f"Unexpected dashboard state: {data['mode']}, {status['worker']}")
print(f"LIVE dashboard HTTP 200; worker={status['worker']}; control={status['control']}")
PY
}

check_live() {
  python3 -c '
import json, sys
state = json.load(sys.stdin)
if not state["enabled"] or state["mode"] != "LIVE":
    raise SystemExit("LIVE config is disabled or invalid")
if state["pending_orders"]:
    raise SystemExit("Pending LIVE orders; deployment stopped")
print("LIVE ledger OK; control={}; target={}".format(
    state["control"], state["weekly_target_allocation"]))
'
}

compose config --quiet
if [[ $mode == dashboard ]]; then
  candidate="toss-trading-api-check-$short-$$"
  compose run -d --rm --no-deps --pull never --name "$candidate" api
  if ! check_dashboard "$candidate"; then
    docker logs --tail 20 "$candidate" || true
    docker stop "$candidate" || true
    exit 1
  fi
  docker stop "$candidate" >/dev/null
else
  # Do not mint a broker token while the current worker is running.
  compose run --rm --no-deps --pull never -T live /app/.venv/bin/python -c '
from datetime import datetime
from toss_trading.marketdata import NY, offline_session
now = datetime.now(NY)
session = offline_session(now.date())
if session and session.contains(now):
    raise SystemExit("Deploy the LIVE worker outside US regular market hours.")
'
  compose run --rm --no-deps --pull never -T live /app/.venv/bin/python \
    -m toss_trading.live.runner --config config/live.toml --status | check_live
fi

if ! compose up -d --no-deps --no-build --pull never "$service"; then
  rollback
  exit 1
fi
if [[ $mode == dashboard ]]; then
  if ! check_dashboard "$current" || ! curl -fsS --max-time 5 http://127.0.0.1:8000/health; then
    rollback
    exit 1
  fi
else
  sleep 5
  if ! docker exec "$current" /app/.venv/bin/python -m toss_trading.live.runner \
    --config config/live.toml --status | check_live; then
    rollback
    exit 1
  fi
fi

if [[ $(docker inspect "$current" --format '{{.Config.Image}}') != "$image" ]]; then
  rollback
  exit 1
fi
echo "Deployed $mode at $revision ($image)"
