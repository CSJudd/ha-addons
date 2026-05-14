#!/bin/bash
# ESPHome Manual Update Script
# Usage: esphome-update.sh [version]
#   version  - specific version tag (e.g. 2026.4.3). Defaults to latest GitHub release.
#
# Environment variables:
#   COMPOSE_FILE   - path to docker-compose.yml  (default: /opt/data-services/docker-compose.yml)
#   COMPOSE_SERVICES - space-separated services to update (default: esphome-production esphome-lab)

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/data-services/docker-compose.yml}"
COMPOSE_SERVICES="${COMPOSE_SERVICES:-esphome-production esphome-lab}"

if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "ERROR: docker-compose.yml not found at $COMPOSE_FILE" >&2
    echo "Set COMPOSE_FILE env var to override." >&2
    exit 1
fi

# Resolve target version
if [[ -n "${1:-}" ]]; then
    TARGET="$1"
else
    TARGET=$(curl -sf https://api.github.com/repos/esphome/esphome/releases/latest \
        | grep '"tag_name"' | cut -d'"' -f4)
    if [[ -z "$TARGET" ]]; then
        echo "ERROR: Could not fetch latest version from GitHub API" >&2
        exit 1
    fi
fi

CURRENT=$(grep "image: ghcr.io/esphome/esphome:" "$COMPOSE_FILE" | head -1 | awk -F: '{print $3}')

echo "Current: ${CURRENT:-unknown}  →  Target: $TARGET"

if [[ "${CURRENT#v}" == "${TARGET#v}" ]]; then
    echo "Already current!"
    exit 0
fi

read -rp "Update to $TARGET? (yes/no): " CONFIRM
[[ "$CONFIRM" != "yes" ]] && echo "Cancelled" && exit 0

# Backup
sudo cp "$COMPOSE_FILE" "${COMPOSE_FILE}.backup-$(date +%Y%m%d-%H%M%S)"

# Update image tag for all ESPHome services
sudo sed -i "s|ghcr.io/esphome/esphome:.*|ghcr.io/esphome/esphome:${TARGET}|g" "$COMPOSE_FILE"

# Pull and restart
cd "$(dirname "$COMPOSE_FILE")"
# shellcheck disable=SC2086
sudo docker compose pull $COMPOSE_SERVICES
# shellcheck disable=SC2086
sudo docker compose up -d $COMPOSE_SERVICES

sleep 5
echo "✓ Updated to $TARGET"
docker ps | grep esphome || true
