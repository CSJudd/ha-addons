#!/usr/bin/with-contenv bashio
set -euo pipefail

# ============================================================================
# ESPHome Selective Updates - Startup Script
# ============================================================================

log_info()  { echo "[INFO] $*"; }
log_warn()  { echo "[WARN] $*"; }
log_fatal() { echo "[FATAL] $*"; exit 1; }

log_info "======================================================================"
log_info "ESPHome Selective Updates - Starting"
log_info "======================================================================"

# Get configuration
ESPHOME_CONTAINER="$(bashio::config 'esphome_container')"
DRY_RUN="$(bashio::config 'dry_run')"

log_info "Configuration:"
log_info "  ESPHome container: ${ESPHOME_CONTAINER}"
log_info "  Dry run mode: ${DRY_RUN}"

# ============================================================================
# Docker Socket Detection
# ============================================================================

log_info ""
log_info "Checking Docker socket availability..."

SOCKET=""
for s in /run/docker.sock /var/run/docker.sock; do
  if [ -S "$s" ]; then
    SOCKET="$s"
    break
  fi
done

if [ -z "$SOCKET" ]; then
  log_fatal "Docker socket not found!"
  log_fatal ""
  log_fatal "This usually means Protection Mode is ON."
  log_fatal ""
  log_fatal "To fix this:"
  log_fatal "1. Go to add-on Info tab"
  log_fatal "2. Find 'Protection mode' toggle"
  log_fatal "3. Turn it OFF"
  log_fatal "4. Restart the add-on"
  log_fatal ""
  log_fatal "Why this is needed:"
  log_fatal "This add-on extends ESPHome's functionality and needs"
  log_fatal "the same Docker access that ESPHome uses for compilation."
  log_fatal "It only accesses the ESPHome container, not your host system."
  log_fatal ""
  exit 1
fi

export DOCKER_HOST="unix://${SOCKET}"
log_info "✓ Docker socket found: ${SOCKET}"

# ============================================================================
# Docker CLI Verification
# ============================================================================

log_info ""
log_info "Verifying Docker CLI..."

if ! command -v docker >/dev/null 2>&1; then
  log_fatal "Docker CLI not found in container image"
  log_fatal "This is a build error. Please report this issue."
  exit 1
fi

DOCKER_VERSION=$(docker --version 2>/dev/null || echo "unknown")
log_info "✓ Docker CLI available: ${DOCKER_VERSION}"

# ============================================================================
# Docker Daemon Connection
# ============================================================================

log_info ""
log_info "Testing Docker daemon connection..."

if ! docker ps >/dev/null 2>&1; then
  log_fatal "Cannot communicate with Docker daemon via ${DOCKER_HOST}"
  log_fatal ""
  log_fatal "Possible causes:"
  log_fatal "1. Docker service not running (unlikely on Home Assistant OS)"
  log_fatal "2. Permission issue with socket"
  log_fatal "3. Protection Mode is ON (most common)"
  log_fatal ""
  log_fatal "Fix: Ensure Protection Mode is OFF in add-on settings"
  exit 1
fi

log_info "✓ Docker daemon connection OK"

# ============================================================================
# ESPHome Container Verification
# ============================================================================

log_info ""
log_info "Verifying ESPHome container '${ESPHOME_CONTAINER}'..."

if ! docker inspect "${ESPHOME_CONTAINER}" >/dev/null 2>&1; then
  log_fatal "ESPHome container '${ESPHOME_CONTAINER}' not found!"
  log_fatal ""
  log_fatal "Possible causes:"
  log_fatal "1. ESPHome add-on not installed"
  log_fatal "2. ESPHome add-on not running"
  log_fatal "3. Container name incorrect in options"
  log_fatal ""
  log_fatal "To fix:"
  log_fatal "1. Ensure ESPHome add-on is installed and running"
  log_fatal "2. Check Supervisor logs for actual container name"
  log_fatal "3. Update 'esphome_container' option if needed"
  log_fatal ""
  log_fatal "Common container names:"
  log_fatal "  • addon_15ef4d2f_esphome (official)"
  log_fatal "  • addon_a0d7b954_esphome"
  log_fatal "  • addon_5c53de3b_esphome"
  exit 1
fi

log_info "✓ ESPHome container found and accessible"

# ============================================================================
# ESPHome Version Check
# ============================================================================

log_info ""
log_info "Detecting ESPHome version..."

ESPHOME_VERSION=$(docker exec "${ESPHOME_CONTAINER}" esphome version 2>/dev/null | grep -o '\(ESPHome|Version:\) [0-9][0-9.]*' | cut -d' ' -f2 || echo "unknown")
log_info "✓ ESPHome version: ${ESPHOME_VERSION}"

# ============================================================================
# Final Pre-flight Summary
# ============================================================================

log_info ""
log_info "======================================================================"
log_info "Pre-flight Checks Complete"
log_info "======================================================================"
log_info "✓ Docker socket: ${SOCKET}"
log_info "✓ Docker daemon: Connected"
log_info "✓ ESPHome container: ${ESPHOME_CONTAINER}"
log_info "✓ ESPHome version: ${ESPHOME_VERSION}"
log_info ""
log_info "Safety boundaries:"
log_info "  • Will only access: ${ESPHOME_CONTAINER}"
log_info "  • Will only modify: /config/esphome/builds/"
log_info "  • No access to: host system, other containers"
log_info ""
log_info "Starting Python updater script..."
log_info "======================================================================"
log_info ""

# ============================================================================
# Execute Main Script
# ============================================================================

exec python3 /app/esphome_smart_updater.py