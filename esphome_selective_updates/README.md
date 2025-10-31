# ESPHome Selective Updates

**Smart bulk updates for ESPHome devices - only updates devices that actually need it**

---

## 🎯 Why This Add-on Exists

### The Problem

ESPHome's built-in "Update All" button has a critical flaw:

- ❌ **No intelligence** - Recompiles ALL devices, even ones already updated
- ❌ **No resume** - If interrupted at device #200, starts over from #1
- ❌ **Wastes time** - Takes 10+ hours for 375 devices when only 5 need updates
- ❌ **No offline detection** - Tries to update unreachable devices and fails

### The Solution

This add-on fixes ESPHome's missing functionality:

- ✅ **Smart updates** - Only compiles devices where `deployed_version ≠ current_version`
- ✅ **Resume capability** - Picks up exactly where it left off if interrupted
- ✅ **Offline detection** - Pings devices first, skips unreachable ones
- ✅ **Progress tracking** - Detailed logging and state persistence
- ✅ **Efficiency** - 15 minutes instead of 10 hours for large deployments

**Real-world results:** Updates 375 devices in ~30 minutes instead of 10+ hours

---

## 🎓 Who Should Use This

### ✅ **This add-on is for you if:**
- You have **50+ ESPHome devices**
- You're tired of ESPHome's "Update All" blindly recompiling everything
- You understand basic Docker concepts
- You're comfortable with "advanced user" tools

### ❌ **Don't use this if:**
- You only have a handful of devices (just use ESPHome dashboard manually)
- You're uncomfortable with the Protection Mode requirement (see below)
- You want a "set and forget" solution (this requires understanding)

---

## ⚠️ Important: Protection Mode Requirement

### **This add-on requires Protection Mode to be turned OFF**

**Why:**
- This add-on extends ESPHome's functionality
- It needs to compile firmware inside the ESPHome container
- This requires Docker socket access, which Protection Mode blocks

**Is this safe?**

Yes, because this add-on:
- ✅ **Only accesses the ESPHome add-on container** (not your host system)
- ✅ **Only reads/writes `/config/esphome/`** (same as ESPHome does)
- ✅ **Uses the same tools ESPHome uses** (esphome CLI, PlatformIO)
- ✅ **Doesn't access other containers** or privileged host resources
- ✅ **Open source** - you can audit the entire codebase

**What it does NOT do:**
- ❌ Does not access your Home Assistant container
- ❌ Does not modify system files
- ❌ Does not access other add-ons
- ❌ Does not expose network services
- ❌ Does not run background services

### How to Disable Protection Mode

1. Go to **Add-ons → ESPHome Selective Updates → Info tab**
2. Find **"Protection mode"** toggle
3. Turn it **OFF**
4. Restart the add-on

**Note:** This is a documented limitation until ESPHome adds a native compile API (which we're requesting - see below).

---

## 🚀 Features

- **Smart Updates** – Only updates devices where deployed_version ≠ current_version
- **Offline Detection** – Pings devices before updating, skips offline devices
- **Resume Capability** – Tracks progress, can resume if interrupted
- **Dry Run Mode** – Preview what would be updated without actually updating
- **Batch Control** – Limit updates per run, whitelist specific devices
- **Comprehensive Logging** – Logs to `/config/esphome_smart_update.log`
- **Integration Ready** – Trigger from Home Assistant automations or scripts
- **Bulk Processing** – Efficiently handles 375+ devices
- **Safe Compilation** – Builds inside the ESPHome add-on (no toolchain issues)
- **Graceful Stop** – Handles Supervisor stop signals, preserves progress

---

## 📋 Requirements

- Home Assistant OS or Supervisor
- Official **ESPHome** add-on installed and running
- Supervisor Docker socket access (`docker_api: true`, `hassio_role: "manager"`)
- **Protection Mode OFF** (see above)
- Correct ESPHome container name (default: `addon_15ef4d2f_esphome`)

---

## 🧩 Installation

### Step 1 – Add the Repository

1. Open **Settings → Add-ons → Add-on Store**
2. Click **⋮ → Repositories**
3. Add: `https://github.com/CSJudd/ha-addons-dev.git`
4. Click **Add**, then **Close**, then **Reload**

### Step 2 – Install the Add-on

1. Find **ESPHome Selective Updates**
2. Click **Install**
3. Wait for installation to complete

### Step 3 – Configure

#### Basic Configuration

```yaml
ota_password: "YOUR_ESPHOME_OTA_PASSWORD"
skip_offline: true
delay_between_updates: 3
esphome_container: "addon_15ef4d2f_esphome"
```

#### Advanced Options

```yaml
# Testing & Control
dry_run: false                    # Preview updates without executing
max_devices_per_run: 0            # Limit devices per run (0 = unlimited)
start_from_device: ""             # Resume from specific device
update_only_these: []             # Whitelist specific devices

# Housekeeping
clear_log_on_start: false         # Clear log every start
clear_log_on_version_change: true # Clear log when add-on updates
clear_progress_on_start: false    # Clear progress every start
```

#### Configuration Reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `ota_password` | string | – | ESPHome OTA password |
| `skip_offline` | boolean | true | Skip devices that fail ping |
| `delay_between_updates` | int | 3 | Seconds between updates |
| `esphome_container` | string | addon_15ef4d2f_esphome | ESPHome container name |
| `dry_run` | boolean | false | Preview mode (no actual updates) |
| `max_devices_per_run` | int | 0 | Limit devices per run (0 = all) |
| `start_from_device` | string | "" | Resume from specific device |
| `update_only_these` | list | [] | Update only these devices |

### Step 4 – Disable Protection Mode

**CRITICAL:** You must turn OFF Protection Mode or the add-on will not work.

1. Go to the **Info** tab
2. Find **"Protection mode"** toggle
3. Turn it **OFF**
4. Read the safety explanation above if you have concerns

### Step 5 – Start the Add-on

1. Go to the **Info** tab
2. Turn **Start on boot** OFF (recommended - run on-demand)
3. Click **Start**

Expected initial log lines:
```
[INFO] ======================================================================
[INFO] ESPHome Selective Updates - Starting
[INFO] ======================================================================
[INFO] ✓ Docker socket found: /run/docker.sock
[INFO] ✓ Docker CLI available: Docker version 24.0.7
[INFO] ✓ Docker daemon connection OK
[INFO] ✓ ESPHome container found and accessible
[INFO] ✓ ESPHome version: 2025.10.3
```

---

## 🛠️ Usage

### Manual Button Script

Create a button in Home Assistant:

```yaml
type: button
tap_action:
  action: call-service
  service: hassio.addon_start
  data:
    addon: local_esphome_selective_updates
name: Update ESPHome Devices
icon: mdi:chip
```

### Scheduled Nightly Update

```yaml
alias: ESPHome Nightly Update
trigger:
  - platform: time
    at: "21:00:00"
action:
  - service: hassio.addon_start
    data:
      addon: local_esphome_selective_updates
```

### Input Boolean Trigger

```yaml
trigger:
  - platform: state
    entity_id: input_boolean.esphome_update_trigger
    to: "on"
action:
  - service: hassio.addon_start
    data:
      addon: local_esphome_selective_updates
  - delay: 5
  - service: input_boolean.turn_off
    target:
      entity_id: input_boolean.esphome_update_trigger
```

### Dry Run First (Recommended)

Before your first real run:

1. Set `dry_run: true` in options
2. Start the add-on
3. Review logs to see what would be updated
4. Set `dry_run: false` when satisfied
5. Run for real

---

## 📊 Monitoring

### Logs

**Live logs:**
- Add-on **Log** tab → real-time output

**Persistent log:**
- `/config/esphome_smart_update.log`

**Progress file:**
- `/config/esphome_update_progress.json`

### Example Output

```
[2025-10-30 21:00:00] ======================================================================
[2025-10-30 21:00:00] ESPHome Selective Updates v2.0
[2025-10-30 21:00:00] ======================================================================
[2025-10-30 21:00:01] 
[2025-10-30 21:00:01] --- Device Discovery ---
[2025-10-30 21:00:01] Found 375 total device configuration(s)
[2025-10-30 21:00:01] 
[2025-10-30 21:00:01] --- Filtering Devices ---
[2025-10-30 21:00:02] Devices needing update: 5
[2025-10-30 21:00:02] Devices to skip: 370
[2025-10-30 21:00:02] 
[2025-10-30 21:00:02] ======================================================================
[2025-10-30 21:00:02] Processing 5 Device(s)
[2025-10-30 21:00:02] ======================================================================
[2025-10-30 21:00:03] 
[2025-10-30 21:00:03] [1/5] Processing: ai001
[2025-10-30 21:00:03] Config: ai001.yaml
[2025-10-30 21:00:03] Versions: deployed=2025.10.2, current=2025.10.3
[2025-10-30 21:00:03] → Compiling ai001.yaml via Docker in 'addon_15ef4d2f_esphome'
[2025-10-30 21:00:45] → Binary copied to /config/esphome/builds/ai001.bin
[2025-10-30 21:00:50] → OTA upload successful
[2025-10-30 21:00:50] ✓ ai001 completed successfully
```

---

## 🧠 How It Works

### Compilation Process

1. Uses `docker exec` to run ESPHome CLI inside the official ESPHome add-on
2. Compiles firmware: `esphome compile /config/esphome/<device>.yaml`
3. Locates compiled `.bin` file in container
4. Copies binary to `/config/esphome/builds/` on host
5. Performs OTA upload via `esphome upload` command

### Smart Update Logic

```python
for device in all_devices:
    deployed_version = dashboard.json["device"]["deployed_version"]
    current_version  = dashboard.json["device"]["current_version"]
    
    if deployed_version == current_version:
        skip(device)  # Already up-to-date
    else:
        update(device)  # Needs update
```

### Resume Capability

Progress is tracked in `/config/esphome_update_progress.json`:

```json
{
  "done": ["ai001", "ai002", "ai003"],
  "failed": ["as007"],
  "skipped": ["offline-device"]
}
```

If the add-on is stopped or crashes, the next run will:
- Skip devices in "done" array
- Retry devices in "failed" array
- Re-evaluate devices in "skipped" array

---

## 🧰 Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `FATAL: Docker socket not found` | Protection Mode is ON | Turn OFF Protection Mode in add-on Info tab |
| `ESPHome container not found` | Container name incorrect | Check actual name in Supervisor logs, update config |
| `Cannot communicate with Docker` | Protection Mode or socket issue | Ensure Protection Mode is OFF, restart add-on |
| Devices skipped | They were offline or already updated | Check logs for skip reasons |
| Compilation failed | YAML syntax error or missing dependencies | Check device YAML in ESPHome dashboard |
| OTA upload failed | Device offline, wrong password, or network issue | Check device reachability and OTA password |

### Common ESPHome Container Names

If `addon_15ef4d2f_esphome` doesn't work, try:
- `addon_a0d7b954_esphome`
- `addon_5c53de3b_esphome`

To find your actual container name:
1. Go to **Settings → System → Logs**
2. Select **Supervisor**
3. Look for "Starting addon_XXXXXXXX_esphome"

### Viewing Progress File

```bash
cat /config/esphome_update_progress.json
```

To manually reset progress:
```yaml
clear_progress_now: true
```
(Remember to set it back to `false` after)

---

## 🔧 Advanced Use Cases

### Update Only Specific Devices

```yaml
update_only_these:
  - ai001
  - ai002
  - as007
```

### Batch Updates (Test on 5 devices first)

```yaml
max_devices_per_run: 5
```

Run multiple times to process all devices in batches.

### Resume from Specific Device

```yaml
start_from_device: "ai150"
```

Useful if you want to manually control where to start.

### Nightly Updates with Notification

```yaml
alias: ESPHome Nightly Update with Notification
trigger:
  - platform: time
    at: "02:00:00"
action:
  - service: hassio.addon_start
    data:
      addon: local_esphome_selective_updates
  - delay: 300  # Wait 5 minutes for completion
  - service: notify.mobile_app
    data:
      title: "ESPHome Updates"
      message: "Nightly device updates completed. Check add-on logs for details."
```

---

## 📡 Network Requirements

- Access to ESPHome dashboard container (local Docker)
- Access to device IPs for HTTP OTA (ports 3232, 8266, or 6053)
- Supervisor Docker socket (`/run/docker.sock`)

---

## 🤝 Contributing to ESPHome

**This add-on is a workaround for a missing ESPHome feature.**

I've opened a feature request with ESPHome to add native "Smart Update All" functionality to the dashboard. If you'd like to see this become an official feature:

1. **Star the ESPHome GitHub repository**
2. **Comment on the feature request** (link in issues)
3. **Share your use case** (how many devices, why you need this)

If ESPHome adds a compile API, I'll refactor this add-on to use it and make Protection Mode unnecessary.

---

## 🧾 Changelog Summary

| Version | Changes |
|---------|---------|
| **2.0.0** | Major rewrite: Added dry run, batch control, better safety checks, clearer messaging about Protection Mode requirement |
| **1.2.x** | Switched to Docker exec mode, added graceful stop |
| **1.1.x** | Added resume capability |
| **1.0.0** | Initial release |

See [CHANGELOG.md](CHANGELOG.md) for detailed version history.

---

## 🧑‍💻 Support

### Self-Help

1. Check the add-on **Log** tab for errors
2. Inspect `/config/esphome_smart_update.log` for detailed history
3. Verify `esphome_container` name is correct
4. Confirm Protection Mode is OFF
5. Test device reachability with manual ping

### Getting Help

If you encounter issues:

1. **Enable dry run mode** and check logs
2. **Check ESPHome add-on is running** and accessible
3. **Open an issue** on GitHub with:
   - Add-on version
   - ESPHome version
   - Relevant log excerpts
   - Configuration (redact passwords)

### Feature Requests

Have an idea? Open an issue with:
- Clear description of the problem
- Proposed solution
- Your use case

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file

---

## 👨‍💻 Author

**Chris Judd** - Large-scale ESPHome management made reliable and repeatable.

Built out of necessity to manage 375+ ESPHome devices efficiently.

---

## 🙏 Acknowledgments

- **ESPHome Team** - For creating an amazing firmware platform
- **Home Assistant Team** - For the add-on infrastructure
- **Community** - For testing and feedback

---

## 🔗 Links

- **Repository:** https://github.com/CSJudd/ha-addons-dev
- **ESPHome:** https://esphome.io
- **Home Assistant:** https://www.home-assistant.io
- **Issues:** https://github.com/CSJudd/ha-addons-dev/issues

---

**Remember:** This add-on is a power-user tool that fixes a real limitation in ESPHome. Use it responsibly, understand the Protection Mode requirement, and help advocate for native ESPHome support for this functionality.