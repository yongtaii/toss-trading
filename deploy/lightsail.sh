#!/usr/bin/env bash
# Run from the Mac. The published Git tree is sent over SSH without a server PAT.
set -euo pipefail

if [[ $# != 1 || ( $1 != dashboard && $1 != live ) ]]; then
  echo "Usage: $0 dashboard|live" >&2
  exit 2
fi
mode=$1
host=${TOSS_LIGHTSAIL_HOST:-toss-lightsail}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
cd "$repo"
test "$(git remote get-url origin)" = https://github.com/yongtaii/toss-trading.git
git fetch --no-tags origin refs/heads/main
revision=$(git rev-parse --verify 'FETCH_HEAD^{commit}')
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git archive --format=tar.gz "$revision" > "$tmp/source.tar.gz"

remote_tmp=$(ssh -o BatchMode=yes "$host" 'mktemp -d /tmp/toss-deploy.XXXXXXXX')
[[ $remote_tmp =~ ^/tmp/toss-deploy\.[a-zA-Z0-9]+$ ]]
scp -q -o BatchMode=yes "$tmp/source.tar.gz" "$host:$remote_tmp/source.tar.gz"
ssh -o BatchMode=yes "$host" bash -s -- "$mode" "$revision" "$remote_tmp" \
  < deploy/lightsail-remote.sh
