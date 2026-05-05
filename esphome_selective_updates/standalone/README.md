# ESPHome Selective Updates - Standalone Mode

Quick reference for standalone deployment (separated ESPHome instances).

For full documentation, see [STANDALONE.md](../STANDALONE.md) in the parent directory.

---

## Quick Start (Codex)

### Automated Installation

```bash
# Copy this directory to Codex
scp -r standalone csjudd@codex:/tmp/

# SSH to Codex
ssh csjudd@codex

# Run installer
cd /tmp/standalone
sudo ./install-codex.sh production  # or: lab, both
```

### Manual Installation

```bash
# 1. Copy config
cp config.example.yaml config.yaml
nano config.yaml

# 2. Create directories
sudo mkdir -p /var/lib/esphome-updater /var/log/esphome-updater
sudo chown $USER:$USER /var/lib/esphome-updater /var/log/esphome-updater

# 3. Test
./esphome-updater --config config.yaml --dry-run

# 4. Manual run
./esphome-updater --config config.yaml
```

---

## Files

- `esphome-updater` - Main launcher script
- `config.example.yaml` - Example configuration
- `install-codex.sh` - Automated Codex installer
- `README.md` - This file

---

## Configuration

Example for Codex production:

```yaml
mode: docker
esphome_config_dir: /opt/data-services/esphome/production
esphome_container: codex-esphome-production
state_dir: /var/lib/esphome-updater-production
log_dir: /var/log/esphome-updater
skip_offline: true
delay_between_updates: 3
```

---

## Usage

```bash
# Show configuration
./esphome-updater --config config.yaml --list-config

# Dry run (preview)
./esphome-updater --config config.yaml --dry-run

# Real run
./esphome-updater --config config.yaml

# Via systemd (after installation)
sudo systemctl start esphome-updater-production.service
sudo journalctl -u esphome-updater-production.service -f
```

---

## Environment Variables

Alternative to config file:

```bash
ESPHOME_CONFIG_DIR=/opt/data-services/esphome/production \
ESPHOME_CONTAINER=codex-esphome-production \
ESPHOME_LOG_DIR=/var/log/esphome-updater \
ESPHOME_STATE_DIR=/var/lib/esphome-updater \
./esphome-updater
```

---

## Documentation

- [STANDALONE.md](../STANDALONE.md) - Complete standalone guide
- [MIGRATION_GUIDE.md](../MIGRATION_GUIDE.md) - Migration from HAOS addon
- [README.md](../README.md) - HAOS addon documentation

---

## Support

Open issues at: https://github.com/CSJudd/ha-addons/issues
