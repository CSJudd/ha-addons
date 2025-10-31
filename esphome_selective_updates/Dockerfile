# ============================================================================
# ESPHome Selective Updates - Dockerfile
# ============================================================================
# This add-on uses Docker exec to compile firmware inside the official
# ESPHome add-on container, avoiding toolchain compatibility issues.
# ============================================================================

ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM ${BUILD_FROM}

# ============================================================================
# Install Dependencies
# ============================================================================

RUN apk add --no-cache \
    python3 \
    py3-requests \
    docker-cli \
    bash \
    iputils

# ============================================================================
# Copy Add-on Files
# ============================================================================

COPY run.sh /app/run.sh
COPY esphome_smart_updater.py /app/esphome_smart_updater.py

# ============================================================================
# Set Permissions
# ============================================================================

RUN chmod +x /app/run.sh && \
    chmod +x /app/esphome_smart_updater.py

# ============================================================================
# Set Environment Variables
# ============================================================================

ENV LANG=C.UTF-8
ENV ADDON_VERSION=2.0.0

# ============================================================================
# Entry Point
# ============================================================================

CMD ["/app/run.sh"]