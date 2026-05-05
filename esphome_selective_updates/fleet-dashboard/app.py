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
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
import aioesphomeapi

# Add custom YAML constructors to handle ESPHome directives
def include_constructor(loader, node):
    """Dummy constructor for !include - just return empty dict"""
    return {}

def lambda_constructor(loader, node):
    """Dummy constructor for !lambda - just return empty string"""
    return ""

yaml.add_constructor('!include', include_constructor, Loader=yaml.SafeLoader)
yaml.add_constructor('!lambda', lambda_constructor, Loader=yaml.SafeLoader)

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
        "database": {
            "type": "postgresql",
            "host": "localhost",
            "port": 5432,
            "database": "esphome_fleet",
            "user": "homeassistant",
            "password": "Yg-vMc-fL2adAguFNuJuuWP3Km"
        },
        "server": {
            "host": "0.0.0.0",
            "port": 8080
        }
    }

CONFIG = load_config()

# ============================================================================
# DATABASE
# ============================================================================

def get_db_connection():
    """Get PostgreSQL database connection"""
    db_config = CONFIG["database"]
    return psycopg2.connect(
        host=db_config["host"],
        port=db_config["port"],
        database=db_config["database"],
        user=db_config["user"],
        password=db_config["password"]
    )

def init_db():
    """Initialize PostgreSQL database for fleet tracking"""
    conn = get_db_connection()
    c = conn.cursor()

    # Create schema if it doesn't exist
    c.execute("CREATE SCHEMA IF NOT EXISTS fleet_manager")

    # Devices table
    c.execute('''
        CREATE TABLE IF NOT EXISTS fleet_manager.devices (
            id SERIAL PRIMARY KEY,
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
        CREATE TABLE IF NOT EXISTS fleet_manager.update_history (
            id SERIAL PRIMARY KEY,
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
        CREATE TABLE IF NOT EXISTS fleet_manager.campaigns (
            id SERIAL PRIMARY KEY,
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

def check_device_online(ip: str) -> str:
    """Check if device is online via ping (1s timeout)"""
    if not ip:
        return "unknown"
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            capture_output=True,
            timeout=2
        )
        return "online" if result.returncode == 0 else "offline"
    except:
        return "offline"

async def get_device_version_async(ip: str, password: str = "") -> Optional[str]:
    """Query ESPHome device via API to get running version"""
    if not ip:
        return None

    try:
        cli = aioesphomeapi.APIClient(ip, 6053, password)
        # Use asyncio.wait_for to add timeout (older aioesphomeapi doesn't support timeout param)
        await asyncio.wait_for(cli.connect(login=True), timeout=3.0)
        device_info = await asyncio.wait_for(cli.device_info(), timeout=2.0)
        await cli.disconnect()
        return device_info.esphome_version
    except (asyncio.TimeoutError, ConnectionError, OSError):
        # Device unreachable or API not responding
        return None
    except Exception:
        # Any other error (encryption, auth, etc.) - silently skip
        return None

def get_device_version(ip: str) -> Optional[str]:
    """Wrapper to run async version query in sync context"""
    try:
        return asyncio.run(get_device_version_async(ip))
    except Exception:
        return None

def discover_devices(instance: Dict) -> List[Dict]:
    """Discover all ESPHome devices for an instance"""
    config_dir = Path(instance["config_dir"])
    devices = []

    if not config_dir.exists():
        return devices

    # First pass: collect all device info without pinging
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        try:
            with yaml_file.open() as f:
                config = yaml.safe_load(f)

            # Extract device info from YAML
            esphome_config = config.get("esphome", {})
            name = esphome_config.get("name") or yaml_file.stem

            # Extract substitutions for area
            substitutions = config.get("substitutions", {})
            area = substitutions.get("area", "Unknown")

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
            # Fallback to area from substitutions if room can't be parsed
            room = area
            if len(parts) > 1:
                room_parts = parts[1:]  # Everything after device code
                parsed_room = " ".join(word.capitalize() for word in room_parts if word)
                if parsed_room:
                    room = parsed_room

            wifi_config = config.get("wifi", {})
            ip = None
            if "manual_ip" in wifi_config:
                ip = wifi_config["manual_ip"].get("static_ip")

            # Try to read device info from .esphome storage
            storage_json = config_dir / ".esphome" / "storage" / f"{yaml_file.name}.json"
            deployed_version = None
            friendly_name = None

            if storage_json.exists():
                try:
                    with storage_json.open() as f:
                        storage_data = json.load(f)
                        deployed_version = storage_data.get("esphome_version")
                        friendly_name = storage_data.get("friendly_name")
                        # Use IP from storage if not in YAML
                        if not ip:
                            ip = storage_data.get("address")
                except Exception as e:
                    print(f"Error reading storage for {name}: {e}")

            devices.append({
                "instance": instance["slug"],
                "name": name,
                "friendly_name": friendly_name or name,
                "node_name": name,
                "device_type": device_type,
                "room": room,
                "area": area,  # From substitutions
                "ip_address": ip,
                "config_file": yaml_file.name,
                "status": "unknown",  # Will be updated in parallel
                "deployed_version": deployed_version,
                "current_version": None,
                "update_available": False
            })

        except Exception as e:
            print(f"Error parsing {yaml_file}: {e}")
            continue

    # Second pass: ping all devices in parallel (max 100 workers)
    print(f"Checking status for {len(devices)} devices in {instance['name']}...")
    with ThreadPoolExecutor(max_workers=100) as executor:
        # Submit all ping checks
        future_to_device = {
            executor.submit(check_device_online, device["ip_address"]): device
            for device in devices
        }

        # Collect results as they complete
        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                device["status"] = future.result()
            except Exception as e:
                print(f"Error checking {device['name']}: {e}")
                device["status"] = "unknown"

    online_count = sum(1 for d in devices if d["status"] == "online")
    print(f"  {online_count}/{len(devices)} devices online")

    # Note: ESPHome API version querying disabled - devices use encryption keys
    # Use HA API integration instead for running versions

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
# DEVICE ACTIONS
# ============================================================================

def get_instance_config(instance_slug: str) -> Optional[Dict]:
    """Get instance configuration by slug"""
    for instance in CONFIG["instances"]:
        if instance["slug"] == instance_slug:
            return instance
    return None

@app.route('/api/device/<instance>/<device_name>')
def get_device_detail(instance, device_name):
    """Get detailed device information"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    config_dir = Path(instance_config["config_dir"])
    yaml_file = config_dir / f"{device_name}.yaml"

    if not yaml_file.exists():
        return jsonify({"error": "Device not found"}), 404

    # Read storage JSON for full details
    storage_json = config_dir / ".esphome" / "storage" / f"{device_name}.yaml.json"
    device_info = {"name": device_name, "instance": instance}

    if storage_json.exists():
        with storage_json.open() as f:
            device_info.update(json.load(f))

    return jsonify(device_info)

@app.route('/api/device/<instance>/<device_name>/config')
def get_device_config(instance, device_name):
    """Get device YAML configuration"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    config_dir = Path(instance_config["config_dir"])
    yaml_file = config_dir / f"{device_name}.yaml"

    if not yaml_file.exists():
        return jsonify({"error": "Device not found"}), 404

    with yaml_file.open() as f:
        content = f.read()

    return jsonify({"config": content})

@app.route('/api/device/<instance>/<device_name>/config', methods=['POST'])
def save_device_config(instance, device_name):
    """Save device YAML configuration"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    config_dir = Path(instance_config["config_dir"])
    yaml_file = config_dir / f"{device_name}.yaml"

    if not yaml_file.exists():
        return jsonify({"error": "Device not found"}), 404

    data = request.json
    new_content = data.get("config", "")

    if not new_content:
        return jsonify({"error": "No content provided"}), 400

    try:
        # Backup original file
        backup_file = yaml_file.with_suffix(".yaml.bak")
        yaml_file.rename(backup_file)

        # Write new content
        with yaml_file.open('w') as f:
            f.write(new_content)

        # Try to parse to validate
        with yaml_file.open() as f:
            yaml.safe_load(f)

        # Success - remove backup
        backup_file.unlink()

        return jsonify({"success": True, "message": "Configuration saved"})

    except Exception as e:
        # Restore backup on error
        if backup_file.exists():
            backup_file.rename(yaml_file)
        return jsonify({"error": f"Failed to save: {str(e)}"}), 500

@app.route('/api/device/<instance>/<device_name>/validate', methods=['POST'])
def validate_device(instance, device_name):
    """Validate device configuration"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    try:
        result = subprocess.run(
            ["docker", "exec", instance_config["container"],
             "esphome", "config", f"/config/{device_name}.yaml"],
            capture_output=True,
            text=True,
            timeout=60
        )

        return jsonify({
            "success": result.returncode == 0,
            "output": result.stdout + result.stderr
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/device/<instance>/<device_name>/compile', methods=['POST'])
def compile_device(instance, device_name):
    """Compile device firmware"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    try:
        result = subprocess.run(
            ["docker", "exec", instance_config["container"],
             "esphome", "compile", f"/config/{device_name}.yaml"],
            capture_output=True,
            text=True,
            timeout=300
        )

        return jsonify({
            "success": result.returncode == 0,
            "output": result.stdout + result.stderr
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/device/<instance>/<device_name>/upload', methods=['POST'])
def upload_device(instance, device_name):
    """Upload firmware to device (OTA)"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    try:
        result = subprocess.run(
            ["docker", "exec", instance_config["container"],
             "esphome", "upload", f"/config/{device_name}.yaml", "--device", "OTA"],
            capture_output=True,
            text=True,
            timeout=300
        )

        return jsonify({
            "success": result.returncode == 0,
            "output": result.stdout + result.stderr
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/device/<instance>/<device_name>/logs')
def get_device_logs(instance, device_name):
    """Get device logs (streaming)"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    try:
        result = subprocess.run(
            ["docker", "exec", instance_config["container"],
             "esphome", "logs", f"/config/{device_name}.yaml", "--no-color"],
            capture_output=True,
            text=True,
            timeout=10
        )

        return jsonify({
            "logs": result.stdout
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/device/<instance>/<device_name>/clean', methods=['POST'])
def clean_device(instance, device_name):
    """Clean device build files"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    try:
        result = subprocess.run(
            ["docker", "exec", instance_config["container"],
             "esphome", "clean", f"/config/{device_name}.yaml"],
            capture_output=True,
            text=True,
            timeout=60
        )

        return jsonify({
            "success": result.returncode == 0,
            "output": result.stdout + result.stderr
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================================
# COMMON FILES MANAGEMENT
# ============================================================================

@app.route('/api/common/<instance>')
def list_common_files(instance):
    """List all common files for an instance"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    config_dir = Path(instance_config["config_dir"])
    common_dir = config_dir / "common"

    if not common_dir.exists():
        return jsonify({"files": []})

    files = []
    for file_path in sorted(common_dir.glob("*.yaml")):
        files.append({
            "name": file_path.name,
            "path": f"common/{file_path.name}",
            "size": file_path.stat().st_size,
            "modified": file_path.stat().st_mtime
        })

    return jsonify({"files": files})

@app.route('/api/common/<instance>/<path:file_path>')
def get_common_file(instance, file_path):
    """Get content of a common file"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    config_dir = Path(instance_config["config_dir"])
    common_file = config_dir / "common" / file_path

    if not common_file.exists() or not str(common_file).startswith(str(config_dir / "common")):
        return jsonify({"error": "File not found"}), 404

    with common_file.open() as f:
        content = f.read()

    return jsonify({"content": content, "path": file_path})

@app.route('/api/common/<instance>/<path:file_path>', methods=['POST'])
def save_common_file(instance, file_path):
    """Save content to a common file"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    config_dir = Path(instance_config["config_dir"])
    common_dir = config_dir / "common"
    common_file = common_dir / file_path

    # Security check
    if not str(common_file).startswith(str(common_dir)):
        return jsonify({"error": "Invalid file path"}), 400

    data = request.json
    new_content = data.get("content", "")

    if not new_content:
        return jsonify({"error": "No content provided"}), 400

    try:
        # Create common directory if it doesn't exist
        common_dir.mkdir(exist_ok=True)

        # Backup if file exists
        if common_file.exists():
            backup_file = common_file.with_suffix(".yaml.bak")
            common_file.rename(backup_file)

        # Write new content
        with common_file.open('w') as f:
            f.write(new_content)

        # Try to parse to validate
        with common_file.open() as f:
            yaml.safe_load(f)

        # Success - remove backup
        backup_file = common_file.with_suffix(".yaml.bak")
        if backup_file.exists():
            backup_file.unlink()

        return jsonify({"success": True, "message": "File saved"})

    except Exception as e:
        # Restore backup on error
        backup_file = common_file.with_suffix(".yaml.bak")
        if backup_file.exists():
            backup_file.rename(common_file)
        return jsonify({"error": f"Failed to save: {str(e)}"}), 500

@app.route('/api/common/<instance>/<path:file_path>', methods=['DELETE'])
def delete_common_file(instance, file_path):
    """Delete a common file"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    config_dir = Path(instance_config["config_dir"])
    common_file = config_dir / "common" / file_path

    # Security check
    if not str(common_file).startswith(str(config_dir / "common")):
        return jsonify({"error": "Invalid file path"}), 400

    if not common_file.exists():
        return jsonify({"error": "File not found"}), 404

    try:
        common_file.unlink()
        return jsonify({"success": True, "message": "File deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================================
# HOME ASSISTANT INTEGRATION
# ============================================================================

@app.route('/api/ha/versions', methods=['POST'])
def get_ha_versions():
    """Query Home Assistant for ESPHome device versions"""
    data = request.json
    ha_url = data.get("ha_url", "http://homeassistant.local:8123")
    ha_token = data.get("ha_token", "")

    if not ha_token:
        return jsonify({"error": "Home Assistant token required"}), 400

    try:
        import requests
        headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json"
        }

        # Query ESPHome devices from HA
        response = requests.get(
            f"{ha_url}/api/states",
            headers=headers,
            timeout=10
        )

        if response.status_code != 200:
            return jsonify({"error": f"HA API error: {response.status_code}"}), 500

        states = response.json()

        # Extract ESPHome device versions
        versions = {}
        for state in states:
            entity_id = state.get("entity_id", "")
            # Look for ESPHome version sensors
            if entity_id.endswith("_esphome_version"):
                device_name = entity_id.replace("sensor.", "").replace("_esphome_version", "")
                version = state.get("state")
                if version and version != "unknown":
                    versions[device_name] = version

        return jsonify({"versions": versions, "count": len(versions)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
