#!/usr/bin/env bash

set -euo pipefail

SERVICE_NAME="ai-assistant-bot"
SERVICE_USER=""
PYTHON_PATH=""
START_SERVICE=true

print_usage() {
  cat <<'EOF'
Usage: sudo ./install_systemd_service.sh [options]

Options:
  --service-name NAME   Override systemd service name (default: ai-assistant-bot)
  --user USER           System user that will run the bot (default: invoking user)
  --python PATH         Path to Python interpreter (default: ./venv/bin/python or python3)
  --no-start            Install and enable the service without starting it immediately
  -h, --help            Show this help message

Environment variables:
  SERVICE_NAME, SERVICE_USER, PYTHON_PATH operate as their corresponding flags.

Example:
  sudo ./install_systemd_service.sh --user botuser
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service-name)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --user)
      SERVICE_USER="$2"
      shift 2
      ;;
    --python)
      PYTHON_PATH="$2"
      shift 2
      ;;
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

SERVICE_NAME="${SERVICE_NAME:-ai-assistant-bot}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run with elevated privileges. Use sudo." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. This installer requires systemd." >&2
  exit 1
fi

if [[ -z "${SERVICE_USER}" ]]; then
  if [[ -n "${SUDO_USER:-}" ]]; then
    SERVICE_USER="${SUDO_USER}"
  else
    SERVICE_USER="$(id -un)"
  fi
fi

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  echo "User ${SERVICE_USER} does not exist. Please create it before installing the service." >&2
  exit 1
fi

SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="${SCRIPT_DIR}"

if [[ -z "${PYTHON_PATH}" ]]; then
  if [[ -x "${WORKDIR}/venv/bin/python" ]]; then
    PYTHON_PATH="${WORKDIR}/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_PATH="$(command -v python3)"
  else
    echo "Python interpreter not found. Set it explicitly via --python PATH." >&2
    exit 1
  fi
fi

if [[ ! -x "${PYTHON_PATH}" ]]; then
  echo "Python interpreter at ${PYTHON_PATH} is not executable or missing." >&2
  exit 1
fi

if [[ ! -f "${WORKDIR}/ai_assistant_bot.py" ]]; then
  echo "ai_assistant_bot.py not found in ${WORKDIR}. Run this script from the project root." >&2
  exit 1
fi

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=AI Assistant Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${WORKDIR}
ExecStart="${PYTHON_PATH}" "${WORKDIR}/ai_assistant_bot.py"
Restart=on-failure
RestartSec=5
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:${WORKDIR}/ai_assistant_bot.log
StandardError=append:${WORKDIR}/ai_assistant_bot.log

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

if "${START_SERVICE}"; then
  systemctl restart "${SERVICE_NAME}"
  systemctl status "${SERVICE_NAME}" --no-pager
else
  echo "Service ${SERVICE_NAME} installed and enabled. Start it manually with: sudo systemctl start ${SERVICE_NAME}"
fi

