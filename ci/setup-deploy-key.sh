#!/bin/bash
# One-time setup (and key rotation) for automatic deploys. Run on your Mac, with
# `ssh birdpi` and `gh` both working:
#
#   ci/setup-deploy-key.sh
#
# 1. Creates the `production` GitHub environment, limited to deployments from main.
# 2. Generates a fresh SSH key for CI and installs its public half on the Pi,
#    locked to ci/deploy-from-ci.sh: no shell, no forwarding, no other commands.
#    Any previous CI key is removed, so re-running this rotates the key.
# 3. Stores the private half as the environment's DEPLOY_SSH_KEY secret and
#    deletes the local copy. It never touches disk anywhere else.

set -euo pipefail

REPO_SLUG=sevensrig/SmartRoomThing
PI=birdpi
FORCED_COMMAND=/home/srigsrini/SmartRoomThing/ci/deploy-from-ci.sh
COMMENT=ci-deploy@github

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "Creating the production environment (deploys from main only)..."
gh api -X PUT "repos/$REPO_SLUG/environments/production" --input - >/dev/null <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
gh api "repos/$REPO_SLUG/environments/production/deployment-branch-policies" --jq '.branch_policies[].name' \
  | grep -qx main \
  || gh api -X POST "repos/$REPO_SLUG/environments/production/deployment-branch-policies" -f name=main >/dev/null

echo "Generating a new deploy key..."
ssh-keygen -q -t ed25519 -N "" -C "$COMMENT" -f "$tmp/key"

echo "Installing it on the Pi (replacing any previous CI key)..."
entry="command=\"$FORCED_COMMAND\",restrict $(cat "$tmp/key.pub")"
# shellcheck disable=SC2029  # $entry is meant to expand here, on the Mac
ssh "$PI" "sed -i '/ $COMMENT\$/d' ~/.ssh/authorized_keys && echo '$entry' >> ~/.ssh/authorized_keys"

echo "Storing the private key as a GitHub secret..."
gh secret set DEPLOY_SSH_KEY --env production --repo "$REPO_SLUG" <"$tmp/key"

echo "Done. The local copy of the key has been deleted."
