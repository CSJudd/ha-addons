#!/bin/bash
# ESPHome Version Monitor
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
DOCKER_COMPOSE_PATH="${DOCKER_COMPOSE_PATH:-/opt/data-services}"
AUTO_UPDATE="${AUTO_UPDATE:-false}"
NOTIFY_ON_NO_UPDATE="${NOTIFY_ON_NO_UPDATE:-false}"

send_telegram() {
    [[ -n "$TELEGRAM_BOT_TOKEN" ]] && [[ -n "$TELEGRAM_CHAT_ID" ]] && \
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" -d text="$1" -d parse_mode="HTML" > /dev/null 2>&1
}

get_latest_version() {
    curl -s https://api.github.com/repos/esphome/esphome/releases/latest | grep '"tag_name"' | cut -d'"' -f4
}

get_current_version() {
    grep "image: ghcr.io/esphome/esphome:" "${DOCKER_COMPOSE_PATH}/docker-compose.yml" | head -1 | awk -F: '{print $3}'
}

update_esphome() {
    cp "${DOCKER_COMPOSE_PATH}/docker-compose.yml" "${DOCKER_COMPOSE_PATH}/docker-compose.yml.backup-$(date +%Y%m%d-%H%M%S)"
    sed -i "s|ghcr.io/esphome/esphome:.*|ghcr.io/esphome/esphome:$1|g" "${DOCKER_COMPOSE_PATH}/docker-compose.yml"
    cd "${DOCKER_COMPOSE_PATH}" && docker compose pull esphome-production esphome-lab && docker compose up -d esphome-production esphome-lab
    sleep 10
    [[ "$(docker inspect codex-esphome-production --format='{{.State.Status}}' 2>/dev/null)" == "running" ]] && \
    [[ "$(docker inspect codex-esphome-lab --format='{{.State.Status}}' 2>/dev/null)" == "running" ]]
}

CURRENT=$(get_current_version)
LATEST=$(get_latest_version)
[[ -z "$CURRENT" ]] || [[ -z "$LATEST" ]] && send_telegram "⚠️ ESPHome check failed" && exit 1

if [[ "${CURRENT#v}" != "${LATEST#v}" ]]; then
    MSG="🔔 New ESPHome: ${LATEST}
Current: ${CURRENT}
https://github.com/esphome/esphome/releases"
    if [[ "$AUTO_UPDATE" == "true" ]]; then
        send_telegram "$MSG - Auto-updating..."
        if update_esphome "$LATEST"; then
            send_telegram "✅ Updated to ${LATEST}"
        else
            send_telegram "❌ Update failed"
        fi
    else
        send_telegram "$MSG
Run: sudo /opt/data-services/esphome-update.sh"
    fi
elif [[ "$NOTIFY_ON_NO_UPDATE" == "true" ]]; then
    send_telegram "✅ ESPHome ${CURRENT} (current)"
fi
