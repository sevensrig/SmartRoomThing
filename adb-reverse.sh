#!/bin/bash
# Establish the USB tunnel so the Car Thing can reach the server on this Pi.
#
# The Car Thing has no network of its own — it reaches the Pi only over USB.
# `adb reverse tcp:5005 tcp:5005` maps the device's localhost:5005 to the
# server here. This script is run as a systemd oneshot, triggered by a udev
# rule when the Car Thing is plugged in (see udev/99-carthing-adb.rules).
#
# Output goes to journald: journalctl -u adb-reverse -f

set -u
PORT=5005
ADB="$(command -v adb || echo /usr/bin/adb)"

log() { echo "[adb-reverse] $*"; }

if [ ! -x "$ADB" ]; then
  log "ERROR: adb not found. Install it with: sudo apt install adb"
  exit 1
fi

# adb is not ready the instant the device enumerates on USB: the adb daemon
# still has to see the device and (re)authorize it. Retry for a while before
# giving up so a freshly-plugged Car Thing reliably gets its tunnel.
for attempt in $(seq 1 15); do
  "$ADB" start-server >/dev/null 2>&1
  if "$ADB" reverse tcp:"$PORT" tcp:"$PORT" >/dev/null 2>&1; then
    log "adb reverse tcp:$PORT established (Car Thing -> Pi server)."
    exit 0
  fi
  log "adb not ready yet (attempt $attempt/15) — waiting 2s..."
  sleep 2
done

log "ERROR: could not establish adb reverse after 15 attempts (device not authorized / not detected?)."
exit 1
