#!/usr/bin/env bash

set -euo pipefail

FAILOVER_SERVICE_NAME="ai-assistant-bot-failover"
INSTALL_BIN="/usr/local/bin/ai-assistant-bot-failover"
ENV_DEST="/etc/default/ai-assistant-bot-failover"
START_SERVICE=true

print_usage() {
  cat <<'EOF'
Usage: sudo ./install_failover_service.sh [options]

Installs the failover watchdog that monitors the master healthcheck endpoint
and starts/stops ai-assistant-bot.service on this standby machine.

Options:
  --no-start   Install and enable without starting the watchdog immediately
  -h, --help   Show this help message

Example:
  sudo ./install_failover_service.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-start)
      START_SERVICE=false
      shift
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      print_usage
      exit 1
      ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run with elevated privileges. Use sudo." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. This installer requires systemd." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl not found. Install curl before setting up failover watchdog." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCHDOG_SRC="${SCRIPT_DIR}/watchdog.sh"
ENV_EXAMPLE="${SCRIPT_DIR}/failover.env.example"
SERVICE_FILE="/etc/systemd/system/${FAILOVER_SERVICE_NAME}.service"

if [[ ! -f "${WATCHDOG_SRC}" ]]; then
  echo "watchdog.sh not found in ${SCRIPT_DIR}" >&2
  exit 1
fi

install -m 755 "${WATCHDOG_SRC}" "${INSTALL_BIN}"
echo "Installed ${INSTALL_BIN}"

if [[ ! -f "${ENV_DEST}" ]]; then
  if [[ ! -f "${ENV_EXAMPLE}" ]]; then
    echo "failover.env.example not found in ${SCRIPT_DIR}" >&2
    exit 1
  fi
  install -m 644 "${ENV_EXAMPLE}" "${ENV_DEST}"
  echo "Created ${ENV_DEST} from example (edit if needed)"
else
  echo "Keeping existing ${ENV_DEST}"
fi

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=AI Assistant Bot failover watchdog (standby)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${INSTALL_BIN}
EnvironmentFile=-${ENV_DEST}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable "${FAILOVER_SERVICE_NAME}"

if "${START_SERVICE}"; then
  systemctl restart "${FAILOVER_SERVICE_NAME}"
  systemctl status "${FAILOVER_SERVICE_NAME}" --no-pager
else
  echo "Service ${FAILOVER_SERVICE_NAME} installed and enabled."
  echo "Start manually: sudo systemctl start ${FAILOVER_SERVICE_NAME}"
fi

echo ""
echo "Tip: disable bot autostart on standby so only the watchdog controls it:"
echo "  sudo systemctl disable ai-assistant-bot"
echo "Logs: journalctl -u ${FAILOVER_SERVICE_NAME} -f"
