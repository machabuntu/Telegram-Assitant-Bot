#!/usr/bin/env bash
# Failover watchdog: starts local ai-assistant-bot when master healthcheck fails.

set -euo pipefail

ENV_FILE="/etc/default/ai-assistant-bot-failover"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

MASTER_HOST="${MASTER_HOST:-212.69.85.168}"
MASTER_PORT="${MASTER_PORT:-18473}"
HEALTH_PATH="${HEALTH_PATH:-/healthz}"
CHECK_INTERVAL="${CHECK_INTERVAL:-10}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-15}"
BOT_SERVICE="${BOT_SERVICE:-ai-assistant-bot}"

LOG_TAG="ai-assistant-bot-failover"
HEALTH_URL="http://${MASTER_HOST}:${MASTER_PORT}${HEALTH_PATH}"

RUNNING=true

log() {
  logger -t "$LOG_TAG" "$*"
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

bot_is_active() {
  systemctl is-active --quiet "$BOT_SERVICE"
}

master_is_alive() {
  local body
  if ! body="$(curl -fsS --max-time "$REQUEST_TIMEOUT" "$HEALTH_URL" 2>/dev/null)"; then
    return 1
  fi
  grep -q '"status"[[:space:]]*:[[:space:]]*"alive"' <<<"$body"
}

stop_handler() {
  RUNNING=false
  log "Received stop signal, exiting"
}

trap stop_handler SIGTERM SIGINT

log "Starting failover watchdog (master=${HEALTH_URL}, bot=${BOT_SERVICE}, interval=${CHECK_INTERVAL}s, timeout=${REQUEST_TIMEOUT}s)"

while $RUNNING; do
  if master_is_alive; then
    if bot_is_active; then
      log "Master is alive — stopping local ${BOT_SERVICE}"
      systemctl stop "$BOT_SERVICE" || log "WARNING: failed to stop ${BOT_SERVICE}"
    fi
  else
    if ! bot_is_active; then
      log "Master is down — starting local ${BOT_SERVICE}"
      systemctl start "$BOT_SERVICE" || log "WARNING: failed to start ${BOT_SERVICE}"
    fi
  fi

  sleep "$CHECK_INTERVAL"
done

log "Watchdog stopped"
