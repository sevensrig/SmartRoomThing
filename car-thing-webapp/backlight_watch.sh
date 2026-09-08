#!/bin/sh
# Runs ON the Car Thing. Turns the screen backlight on/off based on whether the
# server is reachable through the USB (adb reverse) tunnel at localhost:5005.
#
# The Pi runs the Flask server and adb-watch keeps `adb reverse tcp:5005` alive,
# so localhost:5005 answers -> screen ON. If the Pi is off or the tunnel is down
# (the dock may still be powering the Car Thing), it stops answering -> screen
# OFF.
#
# Polls /ping, not /status: this fires every 3s and only the exit code matters,
# so there is no reason to make the server refresh volumes and build JSON.
#
# We toggle ONLY bl_power (panel power: 0 = on, 1 = off). The stock firmware runs
# an auto-brightness service that continuously rewrites `brightness` from the
# ambient-light sensor, so we must not fight it on `brightness` — but it leaves
# bl_power alone. We re-assert bl_power only when it has drifted, so there's no
# flicker, and any stray wake (e.g. a touch) is corrected within a few seconds.
#
# It runs under the device's supervisord (autostart + autorestart), so it's
# always running, survives undocks, and starts on boot.

BL=/sys/class/backlight/aml-bl
SERVER=http://localhost:5005/ping
LOG=/var/cache/blwatch.log   # /var is the persistent partition; /tmp is wiped

prev=""
while true; do
  curl -s --max-time 2 -o /dev/null "$SERVER"; rc=$?
  if [ "$rc" = 0 ]; then
    want=0   # reachable -> screen on
  else
    want=1   # unreachable -> screen off
  fi
  cur=$(cat "$BL/bl_power" 2>/dev/null)
  if [ "$cur" != "$want" ]; then
    echo "$want" > "$BL/bl_power" 2>/dev/null
  fi
  # Log only on dock/undock transitions, to avoid writing flash every 3s.
  if [ "$want" != "$prev" ]; then
    if [ "$want" = 0 ]; then msg="server UP   -> screen ON"; else msg="server DOWN -> screen OFF"; fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') $msg (rc=$rc)" >> "$LOG"
    tail -n 60 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
    prev="$want"
  fi
  sleep 3
done
