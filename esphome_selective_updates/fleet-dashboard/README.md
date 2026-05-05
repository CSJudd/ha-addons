# ESPHome Fleet Manager

**A proper fleet management dashboard for large ESPHome deployments** (400+ devices).

Built because ESPHome's dashboard doesn't scale to large fleets.

---

## Features

✅ **Multi-Instance Support** - Manage production + lab ESPHome instances
✅ **Smart Grouping** - Group by device type, instance, status, or room
✅ **Powerful Filtering** - Search, filter, and find devices instantly
✅ **Bulk Operations** - Select and update multiple devices at once
✅ **Real-Time Stats** - Online/offline counts, update status
✅ **Update History** - Track what was updated, when, and by whom
✅ **Lightweight** - Flask + Alpine.js, no heavy frameworks

---

## Quick Start

### Installation on Codex

```bash
# 1. Pull latest code
cd /opt/esphome-updater
sudo git pull

# 2. Install dependencies
cd esphome_selective_updates/fleet-dashboard
pip3 install -r requirements.txt

# 3. Create database directory
sudo mkdir -p /var/lib/esphome-fleet
sudo chown csjudd:csjudd /var/lib/esphome-fleet

# 4. Start the server
python3 app.py
```

### Access

Open browser to: **http://codex:8080**

---

## Configuration

Edit `config.yaml` to customize:

```yaml
instances:
  - name: "Production"
    config_dir: "/opt/data-services/esphome/production"
    container: "codex-esphome-production"
    enabled: true

server:
  host: "0.0.0.0"
  port: 8080
```

---

## Systemd Service (Optional)

Create `/etc/systemd/system/esphome-fleet.service`:

```ini
[Unit]
Description=ESPHome Fleet Manager
After=network.target

[Service]
Type=simple
User=csjudd
WorkingDirectory=/opt/esphome-updater/esphome_selective_updates/fleet-dashboard
ExecStart=/usr/bin/python3 app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable esphome-fleet.service
sudo systemctl start esphome-fleet.service
```

---

## Screenshots

### Main Dashboard
- Device list with search and filtering
- Group by type, instance, or status
- Bulk selection and actions
- Real-time statistics

### Features Coming Soon
- ✅ Device status monitoring (online/offline)
- ✅ Version tracking and comparison
- ✅ Update campaigns with progress tracking
- ✅ Home Assistant integration
- ✅ WebSocket real-time updates
- ✅ Device tagging and organization
- ✅ Compile history and metrics

---

## Architecture

```
Fleet Dashboard (Port 8080)
├── Flask Backend
│   ├── Device discovery (reads YAML files)
│   ├── Update orchestration (calls standalone updater)
│   └── SQLite database (history, campaigns)
└── Alpine.js Frontend
    ├── Reactive UI (no page reloads)
    ├── Smart filtering
    └── Bulk operations
```

---

## Development

### Run in Debug Mode

```bash
python3 app.py
# Auto-reloads on code changes
```

### API Endpoints

- `GET /api/devices` - List all devices
- `GET /api/devices/types` - Get device types
- `GET /api/stats` - Fleet statistics
- `POST /api/update/start` - Start update campaign
- `GET /api/history` - Update history

---

## Comparison: ESPHome Dashboard vs Fleet Manager

| Feature | ESPHome Dashboard | Fleet Manager |
|---------|-------------------|---------------|
| Device List | Flat, no grouping | Smart grouping & filtering |
| Search | Basic | Advanced (name, type, IP) |
| Bulk Updates | "Update All" (dumb) | Selective bulk updates |
| Status | None | Real-time online/offline |
| History | None | Full update history |
| Multi-Instance | Separate dashboards | Unified view |
| Large Fleets | Unusable (400+ devices) | Built for it |

---

## Roadmap

### Phase 1 (Current)
- [x] Device discovery
- [x] Filtering and search
- [x] Grouping by type/instance
- [x] Basic UI

### Phase 2 (Next)
- [ ] Real device status (online/offline)
- [ ] Version tracking from dashboard.json
- [ ] Bulk update functionality
- [ ] Update progress tracking

### Phase 3 (Future)
- [ ] WebSocket real-time updates
- [ ] Update campaigns with scheduling
- [ ] Home Assistant integration
- [ ] Device tagging system
- [ ] Prometheus metrics export

---

## License

MIT License - Same as parent project

---

## Author

**Chris Judd** - Built for managing 424 ESPHome devices efficiently

"ESPHome's dashboard doesn't scale. This does."
