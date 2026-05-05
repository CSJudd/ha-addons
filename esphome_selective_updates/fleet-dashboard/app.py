#!/usr/bin/env python3
"""
ESPHome Fleet Manager - Web Dashboard

A proper fleet management interface for large ESPHome deployments.
Built specifically for 400+ device fleets where ESPHome's dashboard fails.

Features:
- Device grouping by type, room, status
- Smart filtering and search
- Bulk selective updates
- Real-time status monitoring
- Update history tracking
- Multi-instance support (production + lab)

Author: Chris Judd
License: MIT
"""

from flask import Flask, render_template, jsonify, request, send_from_directory
from pathlib import Path
import json
import subprocess
import yaml
from datetime import datetime
from typing import List, Dict, Optional
import sqlite3
import os

app = Flask(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

CONFIG_FILE = Path(os.environ.get("FLEET_CONFIG", "/opt/esphome-updater/esphome_selective_updates/fleet-dashboard/config.yaml"))

def load_config():
    """Load fleet dashboard configuration"""
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open() as f:
            return yaml.safe_load(f)

    # Default configuration
    return {
        "instances": [
            {
                "name": "Production",
                "slug": "production",
                "config_dir": "/opt/data-services/esphome/production",
                "container": "codex-esphome-production",
                "dashboard_url": "https://codex:6052",
                "enabled": True
            },
            {
                "name": "Lab",
                "slug": "lab",
                "config_dir": "/opt/data-services/esphome/lab",
                "container": "codex-esphome-lab",
                "dashboard_url": "https://codex:6054",
                "enabled": True
            }
        ],
        "updater": {
            "script_path": "/opt/esphome-updater/esphome_selective_updates/standalone/esphome-updater",
            "config_template": "/opt/esphome-updater/esphome_selective_updates/standalone/config-production.yaml"
        },
        "database": "/var/lib/esphome-fleet/fleet.db",
        "server": {
            "host": "0.0.0.0",
            "port": 8080
        }
    }

CONFIG = load_config()

# ============================================================================
# DATABASE
# ============================================================================

def init_db():
    """Initialize SQLite database for fleet tracking"""
    db_path = Path(CONFIG["database"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Devices table
    c.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance TEXT NOT NULL,
            name TEXT NOT NULL,
            node_name TEXT,
            device_type TEXT,
            room TEXT,
            tags TEXT,
            ip_address TEXT,
            last_seen TIMESTAMP,
            status TEXT,
            deployed_version TEXT,
            current_version TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(instance, name)
        )
    ''')

    # Update history table
    c.execute('''
        CREATE TABLE IF NOT EXISTS update_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_name TEXT NOT NULL,
            instance TEXT NOT NULL,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT,
            from_version TEXT,
            to_version TEXT,
            duration_seconds INTEGER,
            error_message TEXT,
            triggered_by TEXT
        )
    ''')

    # Update campaigns table
    c.execute('''
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            instance TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT,
            device_count INTEGER,
            success_count INTEGER,
            failed_count INTEGER,
            config_json TEXT
        )
    ''')

    conn.commit()
    conn.close()

# ============================================================================
# DEVICE DISCOVERY
# ============================================================================

def discover_devices(instance: Dict) -> List[Dict]:
    """Discover all ESPHome devices for an instance"""
    config_dir = Path(instance["config_dir"])
    devices = []

    if not config_dir.exists():
        return devices

    for yaml_file in sorted(config_dir.glob("*.yaml")):
        try:
            with yaml_file.open() as f:
                config = yaml.safe_load(f)

            # Extract device info from YAML
            esphome_config = config.get("esphome", {})
            name = esphome_config.get("name") or yaml_file.stem

            # Extract device type from name pattern
            # Handle vd- prefix (e.g., vd-ai001-lounge-patio)
            clean_name = name.replace("vd-", "").replace("_", "-")
            parts = clean_name.split("-")
            device_code = parts[0] if parts else name

            device_type = "unknown"
            if device_code.startswith("sp"):
                device_type = "Sonoff S31"
            elif device_code.startswith("mjs") or device_code.startswith("mjd"):
                device_type = "Martin Jerry"
            elif device_code.startswith("as"):
                device_type = "Athom Switch"
            elif device_code.startswith("kauf"):
                device_type = "Kauf RGB"
            elif device_code.startswith("ap"):
                device_type = "Athom Plug"
            elif device_code.startswith("valve"):
                device_type = "Valve"
            elif device_code.startswith("ai"):
                device_type = "Athom Inching"
            elif device_code.startswith("aqp"):
                device_type = "Aquarium Pump"
            elif device_code.startswith("ratgdo"):
                device_type = "RATGDO"
            elif device_code.startswith("tsd"):
                device_type = "Touchscreen"
            elif device_code.startswith("wxd"):
                device_type = "Weather Display"

            # Extract room from device name (e.g., vd-ai001-lounge-patio -> "Lounge Patio")
            room = "Unknown"
            if len(parts) > 1:
                room_parts = parts[1:]  # Everything after device code
                room = " ".join(word.capitalize() for word in room_parts if word)
            wifi_config = config.get("wifi", {})
            ip = None
            if "manual_ip" in wifi_config:
                ip = wifi_config["manual_ip"].get("static_ip")

            devices.append({
                "instance": instance["slug"],
                "name": name,
                "node_name": name,
                "device_type": device_type,
                "room": room,
                "ip_address": ip,
                "config_file": yaml_file.name,
                "status": "unknown",
                "deployed_version": None,
                "current_version": None
            })

        except Exception as e:
            print(f"Error parsing {yaml_file}: {e}")
            continue

    return devices

# ============================================================================
# ROUTES
# ============================================================================

@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('index.html', config=CONFIG)

@app.route('/api/instances')
def get_instances():
    """Get all ESPHome instances"""
    return jsonify(CONFIG["instances"])

@app.route('/api/devices')
def get_devices():
    """Get all devices from all instances"""
    instance_filter = request.args.get('instance')
    type_filter = request.args.get('type')
    status_filter = request.args.get('status')
    search = request.args.get('search', '').lower()

    all_devices = []

    for instance in CONFIG["instances"]:
        if not instance.get("enabled"):
            continue

        if instance_filter and instance["slug"] != instance_filter:
            continue

        devices = discover_devices(instance)
        all_devices.extend(devices)

    # Apply filters
    if type_filter:
        all_devices = [d for d in all_devices if d["device_type"] == type_filter]

    if status_filter:
        all_devices = [d for d in all_devices if d["status"] == status_filter]

    if search:
        all_devices = [
            d for d in all_devices
            if search in d["name"].lower() or
               search in d["device_type"].lower() or
               search in d.get("room", "").lower()
        ]

    return jsonify({
        "devices": all_devices,
        "total": len(all_devices)
    })

@app.route('/api/devices/types')
def get_device_types():
    """Get unique device types across all instances"""
    types = set()

    for instance in CONFIG["instances"]:
        if not instance.get("enabled"):
            continue
        devices = discover_devices(instance)
        types.update(d["device_type"] for d in devices)

    return jsonify(sorted(list(types)))

@app.route('/api/stats')
def get_stats():
    """Get fleet statistics"""
    stats = {
        "total_devices": 0,
        "online": 0,
        "offline": 0,
        "updates_available": 0,
        "by_type": {},
        "by_instance": {}
    }

    for instance in CONFIG["instances"]:
        if not instance.get("enabled"):
            continue

        devices = discover_devices(instance)
        instance_count = len(devices)

        stats["total_devices"] += instance_count
        stats["by_instance"][instance["name"]] = instance_count

        for device in devices:
            dtype = device["device_type"]
            stats["by_type"][dtype] = stats["by_type"].get(dtype, 0) + 1

    return jsonify(stats)

@app.route('/api/update/start', methods=['POST'])
def start_update():
    """Start an update campaign"""
    data = request.json
    instance_slug = data.get("instance")
    device_names = data.get("devices", [])
    dry_run = data.get("dry_run", True)

    # TODO: Implement update orchestration
    # This will call the standalone updater with appropriate config

    return jsonify({
        "status": "started",
        "campaign_id": "TODO",
        "message": f"Starting update for {len(device_names)} devices"
    })

@app.route('/api/history')
def get_history():
    """Get update history"""
    # TODO: Query database for history
    return jsonify([])

# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    init_db()

    print("=" * 70)
    print("ESPHome Fleet Manager")
    print("=" * 70)
    print(f"Dashboard: http://{CONFIG['server']['host']}:{CONFIG['server']['port']}")
    print(f"Instances: {len(CONFIG['instances'])}")
    print("=" * 70)

    app.run(
        host=CONFIG['server']['host'],
        port=CONFIG['server']['port'],
        debug=True
    )
