#!/usr/bin/env bash
# Floorball services watchdog and auto-recovery script.
# Ensures all floorball services and tunnels stay active upon network availability and after crashes.

set -euo pipefail

LOG_FILE="/home/moses/tg_bot_floorball_site/var/log/watchdog.log"
mkdir -p "$(dirname "${LOG_FILE}")"

log() {
  echo "$(date --iso-8601=seconds) [floorball-watchdog] $*" | tee -a "${LOG_FILE}"
}

# 1. Check internet connectivity
check_internet() {
  if curl -s -m 5 --head "https://api.telegram.org" >/dev/null 2>&1; then
    return 0
  fi
  if ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if ! check_internet; then
  # Network is temporarily down; do not flap services while offline.
  exit 0
fi

# 2. Check and revive floorball user services
SERVICES=(
  "floorball-content-bot.service"
  "floorball-content-worker.service"
  "floorball-content-miniapp.service"
  "floorball-content-tunnel.service"
)

for svc in "${SERVICES[@]}"; do
  state=$(systemctl --user is-active "${svc}" 2>/dev/null || true)
  if [[ "${state}" != "active" ]]; then
    log "Service ${svc} is in state '${state}'. Restarting..."
    systemctl --user restart "${svc}" 2>&1 | tee -a "${LOG_FILE}" || true
  fi
done

# 3. Ensure timers are active
TIMERS=(
  "floorball-content-health.timer"
  "floorball-content-backup.timer"
  "floorball-content-watchdog.timer"
)

for tmr in "${TIMERS[@]}"; do
  state=$(systemctl --user is-active "${tmr}" 2>/dev/null || true)
  if [[ "${state}" != "active" ]]; then
    log "Timer ${tmr} is in state '${state}'. Starting..."
    systemctl --user start "${tmr}" 2>&1 | tee -a "${LOG_FILE}" || true
  fi
done
