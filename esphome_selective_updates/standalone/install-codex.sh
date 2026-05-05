#!/bin/bash
# ESPHome Selective Updates - Codex Installation Script
#
# This script automates the standalone deployment on Codex VM
#
# Usage:
#   ./install-codex.sh production   # Install for production fleet
#   ./install-codex.sh lab          # Install for lab fleet
#   ./install-codex.sh both         # Install both

set -euo pipefail

# ============================================================================
# CONFIGURATION
# ============================================================================

INSTALL_DIR="/opt/esphome-updater"
REPO_URL="https://github.com/CSJudd/ha-addons.git"
SERVICE_USER="csjudd"

# Production fleet settings
PROD_CONFIG_DIR="/opt/data-services/esphome/production"
PROD_CONTAINER="codex-esphome-production"
PROD_STATE_DIR="/var/lib/esphome-updater-production"

# Lab fleet settings
LAB_CONFIG_DIR="/opt/data-services/esphome/lab"
LAB_CONTAINER="codex-esphome-lab"
LAB_STATE_DIR="/var/lib/esphome-updater-lab"

# Shared settings
LOG_DIR="/var/log/esphome-updater"

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

log_info() {
    echo -e "\033[1;34m[INFO]\033[0m $*"
}

log_success() {
    echo -e "\033[1;32m[OK]\033[0m $*"
}

log_warn() {
    echo -e "\033[1;33m[WARN]\033[0m $*"
}

log_error() {
    echo -e "\033[1;31m[ERROR]\033[0m $*"
    exit 1
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root (use sudo)"
    fi
}

check_user_exists() {
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        log_error "User '$SERVICE_USER' does not exist"
    fi
}

check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        log_error "Docker not found. Please install Docker first."
    fi

    if ! docker ps >/dev/null 2>&1; then
        log_error "Cannot communicate with Docker daemon. Is it running?"
    fi

    log_success "Docker is available"
}

check_dependencies() {
    log_info "Checking dependencies..."

    # Check Python 3
    if ! command -v python3 >/dev/null 2>&1; then
        log_warn "Python 3 not found, installing..."
        apt update
        apt install -y python3
    fi

    # Check PyYAML
    if ! python3 -c "import yaml" 2>/dev/null; then
        log_warn "PyYAML not found, installing..."
        apt install -y python3-yaml
    fi

    log_success "All dependencies installed"
}

install_repository() {
    log_info "Installing repository..."

    if [[ -d "$INSTALL_DIR" ]]; then
        log_warn "Installation directory exists: $INSTALL_DIR"
        read -p "Remove and reinstall? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "$INSTALL_DIR"
        else
            log_error "Installation aborted"
        fi
    fi

    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR/esphome_selective_updates/standalone"
    chmod +x esphome-updater

    log_success "Repository installed to $INSTALL_DIR"
}

create_directories() {
    local mode=$1

    log_info "Creating directories for $mode mode..."

    mkdir -p "$LOG_DIR"

    if [[ "$mode" == "production" ]] || [[ "$mode" == "both" ]]; then
        mkdir -p "$PROD_STATE_DIR"
        chown "$SERVICE_USER:$SERVICE_USER" "$PROD_STATE_DIR"
    fi

    if [[ "$mode" == "lab" ]] || [[ "$mode" == "both" ]]; then
        mkdir -p "$LAB_STATE_DIR"
        chown "$SERVICE_USER:$SERVICE_USER" "$LAB_STATE_DIR"
    fi

    chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"

    log_success "Directories created"
}

create_config() {
    local mode=$1
    local config_file config_dir container state_dir

    if [[ "$mode" == "production" ]]; then
        config_file="config-production.yaml"
        config_dir="$PROD_CONFIG_DIR"
        container="$PROD_CONTAINER"
        state_dir="$PROD_STATE_DIR"
    elif [[ "$mode" == "lab" ]]; then
        config_file="config-lab.yaml"
        config_dir="$LAB_CONFIG_DIR"
        container="$LAB_CONTAINER"
        state_dir="$LAB_STATE_DIR"
    else
        log_error "Unknown mode: $mode"
    fi

    log_info "Creating config file: $config_file"

    cat > "$INSTALL_DIR/esphome_selective_updates/standalone/$config_file" <<EOF
# ESPHome Selective Updates - Codex $mode Configuration
# Generated: $(date)

mode: docker
esphome_config_dir: $config_dir
esphome_container: $container
state_dir: $state_dir
log_dir: $LOG_DIR

# Update behavior
ota_password: ""
skip_offline: true
delay_between_updates: 3

# Testing options
dry_run: false
max_devices_per_run: 0
start_from_device: ""
update_only_these: []
EOF

    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/esphome_selective_updates/standalone/$config_file"
    log_success "Config file created: $config_file"
}

verify_docker_container() {
    local container=$1
    local mode=$2

    log_info "Verifying ESPHome container: $container"

    if ! docker inspect "$container" >/dev/null 2>&1; then
        log_error "ESPHome container '$container' not found for $mode mode"
    fi

    log_success "Container '$container' is accessible"
}

verify_config_dir() {
    local config_dir=$1
    local mode=$2

    log_info "Verifying ESPHome config directory: $config_dir"

    if [[ ! -d "$config_dir" ]]; then
        log_error "Config directory not found: $config_dir (for $mode mode)"
    fi

    local yaml_count
    yaml_count=$(find "$config_dir" -maxdepth 1 -name "*.yaml" | wc -l)

    if [[ $yaml_count -eq 0 ]]; then
        log_warn "No YAML files found in $config_dir"
    else
        log_success "Found $yaml_count device configurations in $config_dir"
    fi
}

create_systemd_service() {
    local mode=$1
    local service_name config_file

    if [[ "$mode" == "production" ]]; then
        service_name="esphome-updater-production"
        config_file="config-production.yaml"
    elif [[ "$mode" == "lab" ]]; then
        service_name="esphome-updater-lab"
        config_file="config-lab.yaml"
    else
        log_error "Unknown mode: $mode"
    fi

    log_info "Creating systemd service: $service_name"

    cat > "/etc/systemd/system/$service_name.service" <<EOF
[Unit]
Description=ESPHome Selective Updates - $(echo $mode | tr '[:lower:]' '[:upper:]') Fleet
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR/esphome_selective_updates/standalone
ExecStart=$INSTALL_DIR/esphome_selective_updates/standalone/esphome-updater --config $INSTALL_DIR/esphome_selective_updates/standalone/$config_file

# Restart policy
Restart=no

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$service_name

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$service_name.service"

    log_success "Service created: $service_name.service"
}

test_dry_run() {
    local mode=$1
    local config_file

    if [[ "$mode" == "production" ]]; then
        config_file="config-production.yaml"
    elif [[ "$mode" == "lab" ]]; then
        config_file="config-lab.yaml"
    else
        return
    fi

    log_info "Testing dry run for $mode..."

    cd "$INSTALL_DIR/esphome_selective_updates/standalone"

    if sudo -u "$SERVICE_USER" ./esphome-updater --config "$config_file" --list-config >/dev/null 2>&1; then
        log_success "Configuration loads successfully"
    else
        log_warn "Configuration test failed (this may be normal if paths need adjustment)"
    fi
}

print_next_steps() {
    local mode=$1

    echo
    echo "============================================================================"
    echo " Installation Complete!"
    echo "============================================================================"
    echo
    echo "Next steps:"
    echo

    if [[ "$mode" == "production" ]] || [[ "$mode" == "both" ]]; then
        echo "Production Fleet:"
        echo "  1. Edit config: $INSTALL_DIR/esphome_selective_updates/standalone/config-production.yaml"
        echo "  2. Test dry run: sudo systemctl start esphome-updater-production.service --dry-run"
        echo "  3. View logs:    sudo journalctl -u esphome-updater-production.service -f"
        echo "  4. Trigger:      sudo systemctl start esphome-updater-production.service"
        echo
    fi

    if [[ "$mode" == "lab" ]] || [[ "$mode" == "both" ]]; then
        echo "Lab Fleet:"
        echo "  1. Edit config: $INSTALL_DIR/esphome_selective_updates/standalone/config-lab.yaml"
        echo "  2. Test dry run: sudo systemctl start esphome-updater-lab.service --dry-run"
        echo "  3. View logs:    sudo journalctl -u esphome-updater-lab.service -f"
        echo "  4. Trigger:      sudo systemctl start esphome-updater-lab.service"
        echo
    fi

    echo "Log files:"
    echo "  Main log:   $LOG_DIR/esphome_smart_update.log"

    if [[ "$mode" == "production" ]] || [[ "$mode" == "both" ]]; then
        echo "  Prod state: $PROD_STATE_DIR/esphome_update_progress.json"
    fi

    if [[ "$mode" == "lab" ]] || [[ "$mode" == "both" ]]; then
        echo "  Lab state:  $LAB_STATE_DIR/esphome_update_progress.json"
    fi

    echo
    echo "Documentation:"
    echo "  Standalone guide: $INSTALL_DIR/esphome_selective_updates/STANDALONE.md"
    echo "  Migration guide:  $INSTALL_DIR/esphome_selective_updates/MIGRATION_GUIDE.md"
    echo
}

# ============================================================================
# MAIN INSTALLATION
# ============================================================================

main() {
    local mode=${1:-}

    if [[ -z "$mode" ]] || [[ ! "$mode" =~ ^(production|lab|both)$ ]]; then
        echo "Usage: $0 {production|lab|both}"
        echo
        echo "  production - Install for production ESPHome fleet"
        echo "  lab        - Install for lab ESPHome fleet"
        echo "  both       - Install for both fleets"
        exit 1
    fi

    echo "============================================================================"
    echo " ESPHome Selective Updates - Codex Installation"
    echo "============================================================================"
    echo
    echo "Mode: $mode"
    echo "Install directory: $INSTALL_DIR"
    echo "Service user: $SERVICE_USER"
    echo

    # Pre-flight checks
    check_root
    check_user_exists
    check_docker
    check_dependencies

    # Installation
    install_repository
    create_directories "$mode"

    # Production setup
    if [[ "$mode" == "production" ]] || [[ "$mode" == "both" ]]; then
        create_config "production"
        verify_docker_container "$PROD_CONTAINER" "production"
        verify_config_dir "$PROD_CONFIG_DIR" "production"
        create_systemd_service "production"
        test_dry_run "production"
    fi

    # Lab setup
    if [[ "$mode" == "lab" ]] || [[ "$mode" == "both" ]]; then
        create_config "lab"
        verify_docker_container "$LAB_CONTAINER" "lab"
        verify_config_dir "$LAB_CONFIG_DIR" "lab"
        create_systemd_service "lab"
        test_dry_run "lab"
    fi

    # Done
    print_next_steps "$mode"
}

main "$@"
