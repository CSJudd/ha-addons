# ESPHome Selective Updates - Codex Migration Guide

## Status: Phase 1 Complete (Framework Ready)

This document tracks the migration from HAOS addon to dual-mode (addon + standalone) deployment.

---

## 📦 What's Been Created

### ✅ Phase 1: Standalone Framework (COMPLETE)

**Files Created:**
1. `standalone/esphome-updater` - Main launcher script
2. `standalone/config.example.yaml` - Example configuration
3. `STANDALONE.md` - Complete user documentation
4. `MIGRATION_GUIDE.md` - This file

**Architecture:**
- **Environment detection**: Auto-detects HAOS addon vs standalone mode
- **Flexible configuration**: YAML file + environment variable support
- **Path abstraction**: Works with both `/config/esphome` (HAOS) and custom paths (Codex)
- **Backend abstraction**: Supports Docker, SSH (planned), and direct modes

**What Works:**
- ✅ Configuration loading from YAML or environment variables
- ✅ Docker mode detection and validation
- ✅ Path mapping for standalone environments
- ✅ Basic safety checks

**What's NOT Yet Implemented:**
- ⚠️ The wrapper script that adapts the original v2.0 code is **untested**
- ⚠️ SSH mode is **not implemented** (fallback to Docker for now)
- ⚠️ Direct CLI mode is **not implemented**

---

## 🎯 Your Setup: Codex Deployment

### Current Architecture

```
HAOS (haos) - Production
├── Home Assistant Core 2026.2.3
├── ESPHome Integration (415 devices via native API)
└── SSH access: root via Tron MCP

Codex (codex) - ESPHome Builder
├── Debian 12, Docker Compose
├── codex-esphome-production
│   ├── Dashboard: https://codex:6052
│   ├── Config: /opt/data-services/esphome/production/
│   └── 424 device YAMLs
├── codex-esphome-lab
│   ├── Dashboard: https://codex:6054
│   └── Config: /opt/data-services/esphome/lab/
└── SSH access: csjudd (sudo) via Tron MCP
```

### Recommended Deployment: Docker Mode

**Why Docker mode?**
- ✅ Codex already has Docker daemon with ESPHome containers
- ✅ No SSH overhead (runs locally on Codex)
- ✅ Direct access to `.dashboard.json` for version tracking
- ✅ Same compilation environment as ESPHome dashboard uses

**Installation Steps:**

1. **Copy addon code to Codex:**
   ```bash
   # On your Mac
   cd "/Users/csj/Dropbox (Personal)/Dev/HomeAssistant Add-Ons/ha-addons"
   git add esphome_selective_updates/
   git commit -m "Add standalone mode for separated ESPHome instances"
   git push
   
   # On Codex (via SSH)
   cd /opt
   git clone https://github.com/CSJudd/ha-addons.git esphome-updater
   cd esphome-updater/esphome_selective_updates
   ```

2. **Install dependencies:**
   ```bash
   sudo apt install python3-yaml
   ```

3. **Create production config:**
   ```bash
   cd standalone
   cp config.example.yaml config-production.yaml
   nano config-production.yaml
   ```
   
   **Production Config (`config-production.yaml`):**
   ```yaml
   mode: docker
   esphome_config_dir: /opt/data-services/esphome/production
   esphome_container: codex-esphome-production
   state_dir: /var/lib/esphome-updater-production
   log_dir: /var/log/esphome-updater
   skip_offline: true
   delay_between_updates: 3
   ota_password: ""  # Add if needed
   ```

4. **Create lab config (optional):**
   ```yaml
   mode: docker
   esphome_config_dir: /opt/data-services/esphome/lab
   esphome_container: codex-esphome-lab
   state_dir: /var/lib/esphome-updater-lab
   log_dir: /var/log/esphome-updater
   skip_offline: true
   delay_between_updates: 3
   ```

5. **Create directories:**
   ```bash
   sudo mkdir -p /var/lib/esphome-updater-production
   sudo mkdir -p /var/lib/esphome-updater-lab
   sudo mkdir -p /var/log/esphome-updater
   sudo chown csjudd:csjudd /var/lib/esphome-updater-* /var/log/esphome-updater
   ```

6. **Test (DRY RUN):**
   ```bash
   ./esphome-updater --config config-production.yaml --dry-run
   ```

7. **Create systemd service:**
   
   `/etc/systemd/system/esphome-updater-production.service`:
   ```ini
   [Unit]
   Description=ESPHome Selective Updates - Production Fleet
   After=docker.service
   Requires=docker.service

   [Service]
   Type=oneshot
   User=csjudd
   Group=csjudd
   WorkingDirectory=/opt/esphome-updater/esphome_selective_updates/standalone
   ExecStart=/opt/esphome-updater/esphome_selective_updates/standalone/esphome-updater --config /opt/esphome-updater/esphome_selective_updates/standalone/config-production.yaml

   StandardOutput=journal
   StandardError=journal
   SyslogIdentifier=esphome-updater-prod

   [Install]
   WantedBy=multi-user.target
   ```

   Enable:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable esphome-updater-production.service
   ```

8. **Manual trigger (on-demand):**
   ```bash
   sudo systemctl start esphome-updater-production.service
   ```

9. **Or: Scheduled via timer:**
   
   `/etc/systemd/system/esphome-updater-production.timer`:
   ```ini
   [Unit]
   Description=ESPHome Updates - Sunday Night

   [Timer]
   OnCalendar=Sun *-*-* 02:00:00
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

   Enable:
   ```bash
   sudo systemctl enable esphome-updater-production.timer
   sudo systemctl start esphome-updater-production.timer
   ```

---

## ⚠️ Known Limitations & Next Steps

### Immediate Testing Required

The standalone launcher uses a **dynamic path-patching approach** to adapt the original v2.0 addon code. This needs testing:

1. **Test configuration loading:**
   ```bash
   ./standalone/esphome-updater --config standalone/config-production.yaml --list-config
   ```

2. **Test Docker detection:**
   ```bash
   # On Codex
   ./standalone/esphome-updater --config config-production.yaml --dry-run
   ```

3. **Verify path mapping:**
   - Check that it reads from `/opt/data-services/esphome/production/`
   - Check that `.dashboard.json` is found
   - Check that state files go to `/var/lib/esphome-updater-production/`

### If Wrapper Approach Fails

If the dynamic path-patching breaks, Plan B:

**Option 1: Extract shared library (cleaner, more work)**
1. Create `esphome_core.py` with device discovery, version checking, compilation logic
2. Create `esphome_addon.py` (thin wrapper for HAOS addon)
3. Create `esphome_standalone.py` (thin wrapper for standalone)
4. Both modes import the shared core

**Option 2: Fork for standalone (faster, duplicates code)**
1. Copy `esphome_smart_updater.py` → `esphome_smart_updater_standalone.py`
2. Refactor standalone version to use ConfigLoader and path abstractions
3. Keep addon version untouched
4. Accept code duplication

**Option 3: SSH bridge mode (quick workaround for Codex)**
Instead of running standalone code on Codex, trigger it FROM Home Assistant via SSH:
1. Install addon on HAOS as normal
2. Addon SSH's to Codex and runs commands there
3. Requires ESPHome dashboard to be accessible from HAOS

**Recommended:** Try Option 1 (shared library) if you have time; Option 2 (fork) if you need it working ASAP.

---

## 🧪 Testing Plan

### Phase 1: Dry Run Testing
1. ✅ Config loading
2. ✅ Docker container detection
3. ✅ Device discovery
4. ✅ Version checking
5. ⚠️ Path adaptation (needs testing)

### Phase 2: Canary Testing
1. Update 1-2 sacrificial devices
2. Verify compilation works
3. Verify OTA upload works
4. Check log files in correct locations

### Phase 3: Small Batch
1. `max_devices_per_run: 5`
2. Monitor for issues
3. Check progress file persistence

### Phase 4: Full Fleet
1. Remove `max_devices_per_run` limit
2. Run against all devices needing updates
3. Monitor performance (415 devices)

---

## 📂 Repository Structure

```
ha-addons/
└── esphome_selective_updates/
    ├── config.json               # HAOS addon config
    ├── Dockerfile                # HAOS addon image
    ├── run.sh                    # HAOS addon entry point
    ├── esphome_smart_updater.py  # Core logic (v2.0, addon mode)
    ├── README.md                 # HAOS addon docs
    ├── STANDALONE.md             # Standalone mode docs (NEW)
    ├── MIGRATION_GUIDE.md        # This file (NEW)
    ├── CHANGELOG.md              # Version history
    └── standalone/               # Standalone mode (NEW)
        ├── esphome-updater       # Launcher script
        ├── config.example.yaml   # Example config
        └── config-production.yaml # Your production config (gitignored)
```

### .gitignore Additions

Add to `.gitignore`:
```
# Standalone configs (may contain passwords)
standalone/config-*.yaml
standalone/*.log
standalone/*.json
```

---

## 🔄 Integration with Home Assistant

Even though ESPHome is now on Codex, you can still trigger updates from HA:

### Method 1: SSH Command (Immediate)

`configuration.yaml`:
```yaml
shell_command:
  esphome_update_production: >-
    ssh -i /config/.ssh/id_ed25519 csjudd@codex 
    'sudo systemctl start esphome-updater-production.service'
  
  esphome_update_lab: >-
    ssh -i /config/.ssh/id_ed25519 csjudd@codex 
    'sudo systemctl start esphome-updater-lab.service'
```

Dashboard button:
```yaml
type: button
tap_action:
  action: call-service
  service: shell_command.esphome_update_production
name: Update ESPHome Fleet
icon: mdi:chip
```

### Method 2: SSH Sensor (Monitor Status)

```yaml
sensor:
  - platform: command_line
    name: ESPHome Updater Last Run
    command: >-
      ssh -i /config/.ssh/id_ed25519 csjudd@codex 
      'sudo systemctl show -p ActiveEnterTimestamp esphome-updater-production.service 
      | cut -d= -f2'
    scan_interval: 3600
```

### Method 3: REST API (Future Enhancement)

A future version could add a REST API server on Codex that HA calls directly.

---

## 🚧 Future Enhancements

### Short-term (Next)
- [ ] Test and fix path-patching wrapper
- [ ] Document actual Codex deployment
- [ ] Create systemd service files
- [ ] SSH key setup for HA → Codex triggers

### Medium-term
- [ ] SSH mode implementation (run from HA, compile on Codex remotely)
- [ ] Direct CLI mode (native ESPHome installation)
- [ ] Progress sensor for Home Assistant
- [ ] Notification integration

### Long-term
- [ ] Web UI for standalone mode
- [ ] Prometheus metrics export
- [ ] Multi-instance orchestration
- [ ] HA REST API integration (bidirectional)

---

## 🎓 Lessons Learned

### Why Separation Makes Sense

**Before (ESPHome on HAOS):**
- ❌ Large ESP fleet causes slow HA restarts (2-3 min)
- ❌ Compilation load on HA VM
- ❌ Add-on conflicts and dependencies
- ❌ Single point of failure

**After (ESPHome on Codex):**
- ✅ HA restarts fast (HA core only)
- ✅ Compilation isolated to builder VM
- ✅ Prod + Lab environments
- ✅ Independent scaling
- ✅ Better resource allocation

### Why This Tool Still Matters

Even with separated ESPHome, you still have the original problem:
- ESPHome dashboard "Update All" is still dumb
- Recompiles all 415 devices unnecessarily
- No resume capability
- No offline detection

**This tool fixes that**, whether ESPHome is on HAOS or Codex.

---

## 📞 Support & Feedback

### Testing Help Needed

If you test the standalone mode, please report:
1. Does configuration loading work?
2. Does Docker mode detect the container?
3. Does path mapping work correctly?
4. Does compilation succeed?
5. Does OTA upload work?
6. Any errors in logs?

### Questions to Resolve

1. **Docker socket access on Codex:** Does `csjudd` user have Docker group membership?
2. **Path permissions:** Can the script read `/opt/data-services/esphome/production/.dashboard.json`?
3. **Network access:** Can Codex ping ESPHome devices for OTA upload?
4. **Systemd privileges:** Does `csjudd` need sudo for systemctl, or can we use user services?

### GitHub Issues

Open issues for:
- Bugs in standalone mode
- Feature requests
- Documentation improvements
- Integration examples

---

## ✅ Checklist for Codex Deployment

### Pre-deployment
- [ ] Push addon code to GitHub
- [ ] Clone repo to Codex (`/opt/esphome-updater`)
- [ ] Install dependencies (`python3-yaml`)
- [ ] Create production config file
- [ ] Create state/log directories
- [ ] Verify Docker socket access

### Testing
- [ ] Dry run with `--list-config`
- [ ] Dry run with `--dry-run`
- [ ] Update 1 sacrificial device
- [ ] Verify log files written correctly
- [ ] Check progress file persistence

### Production
- [ ] Create systemd service file
- [ ] Enable service
- [ ] Test manual trigger
- [ ] Set up timer (if automated)
- [ ] Create HA integration (shell command)
- [ ] Document in HAOS instance memory

### Monitoring
- [ ] Set up log rotation
- [ ] Create HA sensor for last run
- [ ] Add Grafana dashboard (optional)
- [ ] Configure alerts (optional)

---

**Status:** Framework complete, awaiting Codex testing

**Next:** Test standalone launcher on Codex, fix any path/Docker issues

**Timeline:** Ready for Phase 1 testing now; production deployment depends on test results
