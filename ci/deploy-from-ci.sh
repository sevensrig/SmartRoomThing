#!/bin/bash
# Forced command for the CI deploy key on the Pi. Whatever the CI runner asks
# for arrives in $SSH_ORIGINAL_COMMAND, and the only thing it may ask for is a
# full commit SHA that is already on origin/main. Anything else is refused, so a
# leaked key can redeploy reviewed code and nothing more: no shell, no other
# commands, no unmerged branches.
#
# Installed as (one line, in ~/.ssh/authorized_keys):
#   command="/home/srigsrini/SmartRoomThing/ci/deploy-from-ci.sh",restrict ssh-ed25519 AAAA... ci-deploy

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
sha="${SSH_ORIGINAL_COMMAND:-}"

if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[deploy-from-ci] refused: expected a 40-character commit SHA" >&2
  exit 2
fi

cd "$REPO" || exit 1
git fetch -q origin main || { echo "[deploy-from-ci] git fetch failed" >&2; exit 1; }
if ! git merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
  echo "[deploy-from-ci] refused: $sha is not on origin/main" >&2
  exit 2
fi

exec "$REPO/deploy.sh" "$sha"
