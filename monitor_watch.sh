#!/bin/bash
# Watches for the LG HDR WFHD monitor. Starts the volume preset server when the
# monitor connects, stops it when it disconnects.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_PY="$SCRIPT_DIR/server.py"
VENV_PY="$SCRIPT_DIR/venv/bin/python3"
PID_FILE="/tmp/volumepresets.pid"
LOG_FILE="$HOME/Library/Logs/volumepresets.log"
MONITOR_NAME="LG HDR WFHD"
POLL_INTERVAL=5
HA_CONTAINER="homeassistant"
HA_URL="http://localhost:8123"
SERVER_PORT=5005

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

# The Car Thing has no network of its own; it reaches this Mac only over USB.
# `adb reverse` tunnels the device's localhost:PORT to the server here.
find_adb() {
  for p in "$(command -v adb 2>/dev/null)" /opt/homebrew/bin/adb \
           /usr/local/bin/adb "$HOME/Library/Android/sdk/platform-tools/adb"; do
    if [ -n "$p" ] && [ -x "$p" ]; then echo "$p"; return 0; fi
  done
  return 1
}
ADB="$(find_adb)"
REVERSE_STATE="down"

ensure_adb_reverse() {
  [ -z "$ADB" ] && return 1
  if "$ADB" reverse tcp:$SERVER_PORT tcp:$SERVER_PORT >/dev/null 2>&1; then
    if [ "$REVERSE_STATE" != "up" ]; then
      log "adb reverse tcp:$SERVER_PORT established (Car Thing -> Mac server)."
      REVERSE_STATE="up"
    fi
    return 0
  fi
  if [ "$REVERSE_STATE" != "down" ]; then
    log "adb reverse dropped (Car Thing disconnected from USB?)."
    REVERSE_STATE="down"
  fi
  return 1
}

server_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

ha_running() {
  [ -n "$(docker ps -q -f "name=^${HA_CONTAINER}$" 2>/dev/null)" ]
}

start_home_assistant() {
  if ha_running; then
    log "Home Assistant container already running."
  else
    log "Starting Home Assistant container ($HA_CONTAINER)."
    docker start "$HA_CONTAINER" >> "$LOG_FILE" 2>&1
  fi

  log "Waiting for Home Assistant API at $HA_URL/api/ ..."
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -o /dev/null --max-time 2 "$HA_URL/api/"; then
      log "Home Assistant is ready."
      return 0
    fi
    sleep 3
  done
  log "WARN: Home Assistant did not become ready within 30s; starting server anyway."
  return 1
}

stop_home_assistant() {
  log "Stopping Home Assistant container ($HA_CONTAINER)."
  docker stop "$HA_CONTAINER" >> "$LOG_FILE" 2>&1
}

start_server() {
  log "Monitor connected. Starting server."
  # Start Flask + the USB tunnel FIRST so the Car Thing can reach the server and
  # wake its screen within a second or two of docking. Home Assistant (needed for
  # speaker volume, not for the UI/screen) is brought up right after — it can take
  # a while, but the screen no longer waits on it.
  if [ -x "$VENV_PY" ]; then
    PY="$VENV_PY"
  else
    log "WARN: venv python not found at $VENV_PY, falling back to system python3"
    PY="$(/usr/bin/env which python3)"
  fi
  nohup "$PY" "$SERVER_PY" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  log "Server started with PID $(cat "$PID_FILE") using $PY."
  if [ -z "$ADB" ]; then
    log "WARN: adb not found — the Car Thing won't be able to reach the server. Install android-platform-tools."
  fi
  ensure_adb_reverse
  # Now bring up Home Assistant (speaker control). The screen is already awake.
  start_home_assistant
}

stop_server() {
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    log "Monitor disconnected. Stopping server PID $PID."
    kill "$PID" 2>/dev/null
    sleep 1
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    rm -f "$PID_FILE"
    log "Server stopped."
  fi
  [ -n "$ADB" ] && "$ADB" reverse --remove tcp:$SERVER_PORT >/dev/null 2>&1
  REVERSE_STATE="down"
  stop_home_assistant
}

trap 'stop_server; exit 0' SIGTERM SIGINT

log "monitor_watch starting (looking for: $MONITOR_NAME)"

while true; do
  if system_profiler SPDisplaysDataType 2>/dev/null | grep -q "$MONITOR_NAME"; then
    if ! server_running; then
      start_server
    else
      # Keep the USB tunnel alive across Car Thing reboots / replugs.
      ensure_adb_reverse
    fi
  else
    if server_running; then
      stop_server
    fi
  fi
  sleep "$POLL_INTERVAL"
done
