#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
if ! grep -Eq '^(TELEGRAM_BOT_TOKEN|TG_API_KEY)=.+$' "${project_root}/.env"; then
  echo "Telegram bot token is empty in ${project_root}/.env" >&2
  exit 1
fi

unit_root="${HOME}/.config/systemd/user"
install -d -m 700 "${unit_root}"

install -m 600 "${project_root}/deploy/systemd/user/floorball-content-bot.service" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-worker.service" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-miniapp.service" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-backup.service" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-backup.timer" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-health.service" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-health.timer" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-watchdog.service" "${unit_root}/"
install -m 600 "${project_root}/deploy/systemd/user/floorball-content-watchdog.timer" "${unit_root}/"

units=(
  floorball-content-bot.service
  floorball-content-worker.service
  floorball-content-miniapp.service
  floorball-content-backup.timer
  floorball-content-health.timer
  floorball-content-watchdog.timer
)

if [[ -x "${HOME}/.local/bin/cloudflared" ]] \
  || command -v cloudflared >/dev/null 2>&1 \
  || command -v tailscale >/dev/null 2>&1; then
  install -m 600 "${project_root}/deploy/systemd/user/floorball-content-tunnel.service" "${unit_root}/"
  units+=(floorball-content-tunnel.service)
fi

# Ensure user lingering is enabled so user services start at system boot without interactive login
if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "${USER}" || true
fi

systemctl --user daemon-reload
systemctl --user enable --now "${units[@]}"

echo "Floorball user services installed and enabled successfully:"
systemctl --user list-units "floorball*" --all --no-pager
