#!/bin/bash
# Keep the Car Thing's USB tunnel to this Pi alive.
#
# The device reaches the server only through `adb reverse tcp:5005 tcp:5005`, and
# loses that mapping every time it re-enumerates on USB. We poll instead of using
# udev because the Car Thing enumerates as an RNDIS gadget (1d6b:1014), which no
# Android udev rule matches.
#
# The health check is a real HTTP request through the tunnel, not `adb reverse`'s
# exit code: once the transport dies that still exits 0 and adbd still shows a
# listener on the device, but nothing crosses.
#
# Logs: journalctl -u adb-watch -f

set -u

PORT=5005
CHECK_EVERY=5          # seconds between health checks
FAILS_BEFORE_RESET=3   # failed re-establishes before restarting the adb server
ADB="$(command -v adb || echo /usr/bin/adb)"

log() { echo "[adb-watch] $*"; }

device_present() {
  "$ADB" devices 2>/dev/null | grep -q "$(printf '\t')device"
}

tunnel_ok() {
  local code
  code="$("$ADB" shell "curl -s -m 4 -o /dev/null -w '%{http_code}' http://localhost:$PORT/presets" 2>/dev/null | tr -dc '0-9')"
  [ "$code" = "200" ]
}

establish() {
  # Clear stale mappings first: a leftover reverse shadows the new one.
  "$ADB" reverse --remove-all >/dev/null 2>&1
  "$ADB" reverse "tcp:$PORT" "tcp:$PORT" >/dev/null 2>&1
}

if [ ! -x "$ADB" ]; then
  log "ERROR: adb not found. Install it with: sudo apt install adb"
  exit 1
fi

log "watching for the Car Thing (health check every ${CHECK_EVERY}s on port $PORT)"

fails=0
state=""   # last logged state, so we log transitions rather than every poll

while true; do
  if ! device_present; then
    [ "$state" = absent ] || log "device not on USB — waiting"
    state=absent
    fails=0
  elif tunnel_ok; then
    [ "$state" = up ] || log "tunnel up (device -> Pi :$PORT)"
    state=up
    fails=0
  else
    [ "$state" = down ] || log "tunnel down — re-establishing"
    state=down
    establish
    if tunnel_ok; then
      log "tunnel restored"
      state=up
      fails=0
    else
      fails=$((fails + 1))
      if [ "$fails" -ge "$FAILS_BEFORE_RESET" ]; then
        log "still down after $fails attempts — restarting adb server"
        "$ADB" kill-server >/dev/null 2>&1
        sleep 1
        "$ADB" start-server >/dev/null 2>&1
        fails=0
      fi
    fi
  fi
  sleep "$CHECK_EVERY"
done
