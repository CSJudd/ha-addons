# ESPHome Selective Updates - Standalone Mode

This guide is for users who have **separated their ESPHome builder** from Home Assistant OS (like Codex, a dedicated VM, or a separate server).

For traditional HAOS addon installation, see [README.md](README.md).

---

## 🎯 Use Cases

**Use standalone mode if:**
- ✅ You run ESPHome on a **separate server/VM** (not as a HAOS addon)
- ✅ You have a dedicated build server like Codex
- ✅ You manage large ESPHome fleets (50+ devices)
- ✅ You want automated selective updates without HAOS

**Use the HAOS addon if:**
- ✅ ESPHome runs as a Home Assistant addon
- ✅ You want Home Assistant integration
- ✅ You prefer the addon configuration UI

---

## 🏗️ Architecture: Codex Example

```
┌─────────────────────────────────────────────────────────────┐
│ HAOS (VM 237)                                               │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Home Assistant Core                                     │ │
│ │ ✓ ESPHome Integration (native API connections)         │ │
│ │ ✓ 415 devices connected                                │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                           ▲
                           │ Native API connections (6053)
                           │
┌─────────────────────────────────────────────────────────────┐
│ Codex (VM 211) - ESPHome Builder                            │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Docker: codex-esphome-production                        │ │
│ │ • Dashboard: https://codex:6052                         │ │
│ │ • Configs: /opt/data-services/esphome/production/       │ │
│ │ • 424 device YAML files                                 │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Docker: codex-esphome-lab                               │ │
│ │ • Dashboard: https://codex:6054                         │ │
│ │ • Configs: /opt/data-services/esphome/lab/              │ │
│ └─────────────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Standalone Updater Service ⬅ THIS TOOL                 │ │
│ │ • Runs as systemd service or cron job                   │ │
│ │ • Uses Docker exec to compile/upload                    │ │
│ │ • Monitors production configs for updates               │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## 📋 Requirements

- **Separated ESPHome instance** (Docker, native, or remote)
- **Python 3.8+** with PyYAML
- **Docker CLI** (if using Docker mode)
- **SSH access** (if using SSH mode - NOT YET IMPLEMENTED)
- **Access to ESPHome `.dashboard.json`** for version tracking

---

## 🚀 Installation

### Step 1: Clone Repository

```bash
cd /opt
git clone https://github.com/CSJudd/ha-addons.git esphome-updater
cd esphome-updater/esphome_selective_updates
```

### Step 2: Install Dependencies

```bash
# Debian/Ubuntu
sudo apt install python3-yaml

# Or via pip
pip3 install pyyaml
```

### Step 3: Create Configuration

```bash
cd standalone
cp config.example.yaml config.yaml
nano config.yaml
```

**Example for Codex Production:**

```yaml
mode: docker
esphome_config_dir: /opt/data-services/esphome/production
esphome_container: codex-esphome-production
state_dir: /var/lib/esphome-updater
log_dir: /var/log/esphome-updater
skip_offline: true
delay_between_updates: 3
```

**Example for Codex Lab:**

```yaml
mode: docker
esphome_config_dir: /opt/data-services/esphome/lab
esphome_container: codex-esphome-lab
state_dir: /var/lib/esphome-updater-lab
log_dir: /var/log/esphome-updater
skip_offline: true
delay_between_updates: 3
```

### Step 4: Create State/Log Directories

```bash
sudo mkdir -p /var/lib/esphome-updater
sudo mkdir -p /var/log/esphome-updater
sudo chown $USER:$USER /var/lib/esphome-updater /var/log/esphome-updater
```

### Step 5: Make Script Executable

```bash
chmod +x standalone/esphome-updater
```

### Step 6: Test Run (Dry Run)

```bash
./standalone/esphome-updater --config standalone/config.yaml --dry-run
```

Expected output:
```
ESPHome Selective Updates - Standalone Mode
Mode: docker
Config dir: /opt/data-services/esphome/production

✓ Docker mode - container: codex-esphome-production
[2026-05-05 14:23:45] ======================================
[2026-05-05 14:23:45] ESPHome Selective Updates v2.0
[2026-05-05 14:23:45] ======================================
[2026-05-05 14:23:45] ⚠ DRY RUN MODE - No actual updates will be performed
...
```

---

## 🛠️ Usage

### Manual Execution

```bash
# Normal run
./standalone/esphome-updater --config standalone/config.yaml

# Dry run (preview)
./standalone/esphome-updater --config standalone/config.yaml --dry-run

# Show current configuration
./standalone/esphome-updater --config standalone/config.yaml --list-config
```

### Environment Variable Mode

```bash
# Without config file (all via env vars)
ESPHOME_CONFIG_DIR=/opt/data-services/esphome/production \
ESPHOME_CONTAINER=codex-esphome-production \
ESPHOME_LOG_DIR=/var/log/esphome-updater \
ESPHOME_STATE_DIR=/var/lib/esphome-updater \
./standalone/esphome-updater
```

### Systemd Service (Recommended for Codex)

Create `/etc/systemd/system/esphome-updater.service`:

```ini
[Unit]
Description=ESPHome Selective Updates - Production
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=csjudd
Group=csjudd
WorkingDirectory=/opt/esphome-updater/esphome_selective_updates/standalone
ExecStart=/opt/esphome-updater/esphome_selective_updates/standalone/esphome-updater --config /opt/esphome-updater/esphome_selective_updates/standalone/config.yaml

# Restart policy
Restart=no

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=esphome-updater

[Install]
WantedBy=multi-user.target
```

Enable and use:

```bash
sudo systemctl daemon-reload
sudo systemctl enable esphome-updater.service

# Manual trigger
sudo systemctl start esphome-updater.service

# Check status
sudo systemctl status esphome-updater.service

# View logs
sudo journalctl -u esphome-updater.service -f
```

### Cron Job (Alternative)

Weekly updates on Sunday night:

```bash
sudo crontab -e
```

Add:

```cron
# ESPHome Selective Updates - Every Sunday at 2 AM
0 2 * * 0 /opt/esphome-updater/esphome_selective_updates/standalone/esphome-updater --config /opt/esphome-updater/esphome_selective_updates/standalone/config.yaml >> /var/log/esphome-updater/cron.log 2>&1
```

---

## 🔧 Configuration Options

### Core Settings

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `mode` | string | docker | Backend mode: docker, ssh, or direct |
| `esphome_config_dir` | path | (required) | Path to ESPHome YAML configs |
| `esphome_container` | string | (required) | Docker container name (docker mode) |
| `state_dir` | path | /var/lib/esphome-updater | Progress/state directory |
| `log_dir` | path | /var/log/esphome-updater | Log file directory |

### Update Behavior

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `ota_password` | string | "" | ESPHome OTA password |
| `skip_offline` | bool | true | Skip devices that fail ping |
| `delay_between_updates` | int | 3 | Seconds between updates |
| `dry_run` | bool | false | Preview mode (no updates) |
| `max_devices_per_run` | int | 0 | Limit per run (0 = all) |
| `start_from_device` | string | "" | Resume from device name |
| `update_only_these` | list | [] | Device whitelist |

### SSH Mode (Not Yet Implemented)

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `ssh_host` | string | null | SSH hostname |
| `ssh_user` | string | null | SSH username |
| `ssh_key_file` | path | null | SSH private key |
| `ssh_port` | int | 22 | SSH port |

---

## 📊 Monitoring

### Log Files

```bash
# Real-time monitoring
tail -f /var/log/esphome-updater/esphome_smart_update.log

# Check progress
cat /var/lib/esphome-updater/esphome_update_progress.json
```

### Progress File Format

```json
{
  "done": ["ai001", "ai002", "mjs018"],
  "failed": ["sp069"],
  "skipped": ["offline-device"]
}
```

### Systemd Journal

```bash
# Follow live updates
sudo journalctl -u esphome-updater.service -f

# Last run
sudo journalctl -u esphome-updater.service -n 100
```

---

## 🧰 Troubleshooting

### Docker Mode Issues

**"Docker not available"**
```bash
# Verify Docker is running
docker ps

# Check user permissions
groups $USER  # Should include 'docker' group
sudo usermod -aG docker $USER  # If not in docker group
```

**"ESPHome container not found"**
```bash
# List all containers
docker ps -a

# Check ESPHome dashboard is running
docker logs codex-esphome-production
```

### Permission Issues

**"Permission denied: /var/lib/esphome-updater"**
```bash
# Fix ownership
sudo chown -R $USER:$USER /var/lib/esphome-updater /var/log/esphome-updater
```

### Configuration Issues

**"ESPHome config directory not found"**
```bash
# Verify path exists
ls -la /opt/data-services/esphome/production/

# Check mount points (if using network storage)
df -h | grep esphome
```

---

## 🔄 Comparison: Addon vs Standalone

| Feature | HAOS Addon | Standalone |
|---------|------------|------------|
| **Target Users** | ESPHome on HAOS | Separated ESPHome instances |
| **Configuration** | HAOS addon UI | YAML file or env vars |
| **Execution** | Addon start/stop | Systemd, cron, or manual |
| **HA Integration** | Native | Optional (via REST API - future) |
| **Docker Access** | Protection mode OFF | Direct Docker socket |
| **SSH Support** | N/A | Planned |
| **Multi-instance** | One addon | Multiple configs |

---

## 🗺️ Roadmap

- [x] Docker mode (local ESPHome container)
- [ ] SSH mode (remote ESPHome builder)
- [ ] Direct mode (native ESPHome CLI)
- [ ] Home Assistant REST API integration
- [ ] Web UI for standalone mode
- [ ] Prometheus metrics export
- [ ] Email/notification integration

---

## 📡 Home Assistant Integration (Optional)

You can trigger standalone updates from Home Assistant using SSH commands or REST API:

### Via SSH Command

Create a shell command in HA:

```yaml
# configuration.yaml
shell_command:
  esphome_update_production: >
    ssh csjudd@codex 'sudo systemctl start esphome-updater.service'
```

Button:

```yaml
type: button
tap_action:
  action: call-service
  service: shell_command.esphome_update_production
name: Update ESPHome (Codex)
icon: mdi:chip
```

### Via REST API (Future)

A future version will include a REST API server for remote control.

---

## 🧪 Testing Recommendations

1. **First run: Dry run mode**
   ```bash
   ./standalone/esphome-updater --config config.yaml --dry-run
   ```

2. **Test with whitelist**
   ```yaml
   update_only_these:
     - test_device_01
     - sacrificial_device
   ```

3. **Batch testing**
   ```yaml
   max_devices_per_run: 5
   ```

4. **Monitor first real run**
   ```bash
   tail -f /var/log/esphome-updater/esphome_smart_update.log
   ```

---

## 📄 License

MIT License - Same as parent addon

---

## 👨‍💻 Author

**Chris Judd** - Built for large-scale ESPHome fleet management

Originally created as a HAOS addon, now adapted for standalone deployment.

---

## 🔗 Links

- **Main Addon**: [README.md](README.md)
- **Repository**: https://github.com/CSJudd/ha-addons
- **ESPHome**: https://esphome.io
- **Issues**: https://github.com/CSJudd/ha-addons/issues
