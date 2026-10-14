#!/bin/bash
# Deploy a commit to this Pi and the Car Thing plugged into it, verify it came up
# healthy, and roll everything back if it didn't.
#
#   ./deploy.sh              deploy origin/main
#   ./deploy.sh <commit>     deploy a specific commit (also how to roll back by hand)
#
# Runs ON the Pi, from the repo checkout. Only the parts that changed are
# touched: the server restarts only if server.py or its dependencies changed, and
# a file is pushed to the Car Thing only if the copy on the device differs.
#
# Healthy means real requests succeed, not that commands exited 0 — `adb reverse`
# and a listening socket have both lied here before:
#   - GET /status on the Pi returns 200
#   - every pushed file's checksum on the device matches the repo
#   - GET /ping from ON the Car Thing, through the USB tunnel, returns 204
#
# Every run is appended to .deploy-history.

set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
STATUS_URL=http://localhost:5005/status
HISTORY="$REPO/.deploy-history"
LOCK=/tmp/smartroomthing-deploy.lock
HEALTH_TIMEOUT=60   # seconds; Cast connections at startup take ~10s per speaker

# Files deployed to the Car Thing: repo path, device path, supervisord program
# to restart after a change.
DEVICE_FILES=(
  "car-thing-webapp/index.html|/usr/share/qt-superbird-app/webapp/index.html|chromium"
  "car-thing-webapp/backlight_watch.sh|/usr/share/qt-superbird-app/backlight_watch.sh|dockbacklight"
)

log()  { echo "[deploy] $*"; }
warn() { echo "[deploy] WARNING: $*" >&2; }
die()  { echo "[deploy] ERROR: $*" >&2; exit 1; }

record() {
  printf '%s  %s -> %s  %s  %ss\n' "$(date -Is)" "${PREV:0:7}" "${TARGET:0:7}" "$1" "$SECONDS" >>"$HISTORY"
}

# --- the Car Thing -----------------------------------------------------------

device_md5() {
  adb shell md5sum "$1" 2>/dev/null | tr -d '\r' | awk '{print $1}'
}

# Any rootfs write needs the remount wrapper, and the sync: without it an
# unclean reboot silently reverts the write to the stock image.
device_push() {
  local src=$1 dest=$2 rc=0
  adb shell mount -o remount,rw / || return 1
  adb push "$src" "$dest" >/dev/null || rc=1
  [ "$dest" = "${dest%.sh}" ] || adb shell chmod +x "$dest" || rc=1
  adb shell 'sync; sync; mount -o remount,ro /' || rc=1
  return $rc
}

device_restart() {
  adb shell supervisorctl restart "$1" >/dev/null
}

# The device's own view of the server, through the tunnel.
device_ping() {
  local code
  code="$(adb shell "curl -s -m 4 -o /dev/null -w '%{http_code}' http://localhost:5005/ping" 2>/dev/null | tr -dc '0-9')"
  [ "$code" = "204" ]
}

# --- the Pi ------------------------------------------------------------------

server_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while [ $SECONDS -lt $deadline ]; do
    [ "$(curl -s -m 4 -o /dev/null -w '%{http_code}' "$STATUS_URL")" = "200" ] && return 0
    sleep 2
  done
  return 1
}

tunnel_healthy() {
  # adb-watch re-establishes a dropped tunnel within ~5s; give it a few rounds.
  local _
  for _ in 1 2 3 4 5 6; do
    device_ping && return 0
    sleep 5
  done
  return 1
}

changed() {
  # True if any of the given paths differ between PREV and TARGET.
  ! git diff --quiet "$PREV" "$TARGET" -- "$@"
}

# --- deploy and roll back ----------------------------------------------------

# Every step checks its own result: `set -e` is ignored inside a function called
# from an `if`, so it can't be relied on here.
apply() {
  if [ "$PREV" != "$TARGET" ]; then
    log "checking out ${TARGET:0:7}"
    git reset -q --hard "$TARGET" || return 1
  fi

  if changed requirements.txt; then
    log "installing Python dependencies"
    venv/bin/pip install -q --disable-pip-version-check -r requirements.txt || return 1
  fi
  if changed server.py requirements.txt; then
    log "restarting volumepresets"
    sudo systemctl restart volumepresets || return 1
  fi
  if changed adb-watch.sh; then
    log "restarting adb-watch"
    sudo systemctl restart adb-watch || return 1
  fi
  if changed volumepresets.service adb-watch.service; then
    warn "a systemd unit file changed; install it by hand (see README, Step 7)"
  fi

  local entry src dest prog
  for entry in "${DEVICE_FILES[@]}"; do
    IFS='|' read -r src dest prog <<<"$entry"
    [ "$(device_md5 "$dest")" = "$(md5sum "$src" | awk '{print $1}')" ] && continue
    log "pushing $src to the Car Thing"
    device_push "$src" "$dest" || return 1
    PUSHED+=("$entry")
    device_restart "$prog" || return 1
  done
  return 0
}

verify() {
  log "checking the server"
  server_healthy || { warn "$STATUS_URL did not return 200 within ${HEALTH_TIMEOUT}s"; return 1; }

  local entry src dest prog
  for entry in "${DEVICE_FILES[@]}"; do
    IFS='|' read -r src dest prog <<<"$entry"
    [ "$(device_md5 "$dest")" = "$(md5sum "$src" | awk '{print $1}')" ] \
      || { warn "$dest on the Car Thing doesn't match the repo"; return 1; }
  done

  log "checking the tunnel from the Car Thing"
  tunnel_healthy || { warn "the Car Thing can't reach the server"; return 1; }
  return 0
}

rollback() {
  warn "rolling back to ${PREV:0:7}"
  local ok=0 entry src dest prog
  git reset -q --hard "$PREV" || ok=1
  if changed requirements.txt; then
    venv/bin/pip install -q --disable-pip-version-check -r requirements.txt || ok=1
  fi
  # Restart unconditionally: whatever state the failed deploy left it in, the
  # server must end up running the previous code.
  sudo systemctl restart volumepresets || ok=1
  changed adb-watch.sh && { sudo systemctl restart adb-watch || ok=1; }

  for entry in "${PUSHED[@]}"; do
    IFS='|' read -r src dest prog <<<"$entry"
    local backup
    backup="$BACKUP_DIR/$(basename "$dest")"
    log "restoring $dest on the Car Thing"
    { device_push "$backup" "$dest" && device_restart "$prog"; } || ok=1
  done

  if [ $ok -eq 0 ] && server_healthy && tunnel_healthy; then
    log "rolled back; ${PREV:0:7} is running and healthy"
    record "rolled-back"
  else
    warn "ROLLBACK DID NOT COME UP HEALTHY. Check: journalctl -u volumepresets -n 50"
    record "rollback-failed"
  fi
}

main() {
  cd "$REPO" || die "can't cd to $REPO"

  exec 9>"$LOCK"
  flock -n 9 || die "another deploy is already running"

  # Offline is fine for deploying a commit that is already here (a rollback).
  git fetch -q origin || warn "git fetch failed; using what is already on the Pi"
  TARGET="$(git rev-parse --verify -q "${1:-origin/main}^{commit}")" || die "unknown commit: ${1:-origin/main}"
  PREV="$(git rev-parse HEAD)"
  PUSHED=()

  # Preflight: nothing below has changed anything yet.
  if ! git diff --quiet HEAD; then
    die "uncommitted changes to tracked files on the Pi; commit or discard them first:
$(git status --short --untracked-files=no)"
  fi
  [ "$(adb get-state 2>/dev/null)" = "device" ] || die "the Car Thing isn't connected over USB"

  # Back up what's on the device, not what the previous commit says should be
  # there; the two have drifted before.
  BACKUP_DIR="$(mktemp -d /tmp/deploy-backup.XXXXXX)"
  local entry src dest prog
  for entry in "${DEVICE_FILES[@]}"; do
    IFS='|' read -r src dest prog <<<"$entry"
    adb pull "$dest" "$BACKUP_DIR/" >/dev/null || die "couldn't back up $dest from the Car Thing"
  done

  if [ "$PREV" = "$TARGET" ]; then
    log "already at ${TARGET:0:7} ($(git log -1 --format=%s "$TARGET"))"
  else
    log "deploying ${PREV:0:7} -> ${TARGET:0:7} ($(git log -1 --format=%s "$TARGET"))"
  fi

  if apply && verify; then
    log "done in ${SECONDS}s; ${TARGET:0:7} is running and healthy"
    record "ok"
    rm -rf "$BACKUP_DIR"
    return 0
  fi

  rollback
  log "device backups kept in $BACKUP_DIR"
  return 1
}

# All the code is in functions and main runs on the same line as `exit`. Bash
# reads a script as it executes it, and `git reset` rewrites this file
# mid-deploy; this way bash has already parsed everything it will run.
main "$@"; exit $?
