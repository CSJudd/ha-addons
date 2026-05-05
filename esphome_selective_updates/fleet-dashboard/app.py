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

from flask import Flask, render_template, jsonify, request, send_from_directory, Response, stream_with_context
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
import threading
import time

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

    # Bulk operations log table
    c.execute('''
        CREATE TABLE IF NOT EXISTS fleet_manager.operations (
            id SERIAL PRIMARY KEY,
            operation_type TEXT NOT NULL,
            device_count INTEGER NOT NULL,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            status TEXT DEFAULT 'running',
            triggered_by TEXT,
            notes TEXT
        )
    ''')

    # Per-device operation results table
    c.execute('''
        CREATE TABLE IF NOT EXISTS fleet_manager.operation_results (
            id SERIAL PRIMARY KEY,
            operation_id INTEGER REFERENCES fleet_manager.operations(id) ON DELETE CASCADE,
            device_name TEXT NOT NULL,
            instance TEXT NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT,
            output TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            duration_seconds INTEGER
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

            # Extract substitutions first (needed for name resolution)
            substitutions = config.get("substitutions", {})

            # Extract device info from YAML
            esphome_config = config.get("esphome", {})
            name = esphome_config.get("name") or yaml_file.stem

            # Resolve substitution variables (e.g., ${name})
            if isinstance(name, str) and name.startswith("${") and name.endswith("}"):
                sub_var = name[2:-1]  # Extract variable name
                name = substitutions.get(sub_var, yaml_file.stem)
            area = substitutions.get("area", "Unknown")
            physical_location = substitutions.get("physical_location")

            # Get ESPHome version from substitutions (for version pinning)
            esphome_version = substitutions.get("esphome_version", "*")

            # Get device type from substitutions (no fallback detection)
            device_type = substitutions.get("device_type", "unknown")

            # Parse device name for room
            clean_name = name.replace("vd-", "").replace("_", "-")
            parts = clean_name.split("-")

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
                "pinned_version": esphome_version,  # From substitutions for version pinning
                "room": room,
                "area": area,  # From substitutions
                "physical_location": physical_location,  # From substitutions (optional)
                "ip_address": ip,
                "config_file": yaml_file.name,
                "yaml_filename": yaml_file.stem,  # Original filename without extension
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
        # Check if device has pinned ESPHome version
        config_dir = Path(instance_config["config_dir"])
        yaml_file = config_dir / f"{device_name}.yaml"

        pinned_version = None
        if yaml_file.exists():
            with yaml_file.open() as f:
                config = yaml.safe_load(f)
                substitutions = config.get("substitutions", {})
                pinned_version = substitutions.get("esphome_version", "*")

        # Use version-specific Docker image if pinned
        if pinned_version and pinned_version != "*":
            result = subprocess.run(
                ["docker", "run", "--rm",
                 "-v", f"{config_dir}:/config",
                 f"esphome/esphome:{pinned_version}",
                 "compile", f"/config/{device_name}.yaml"],
                capture_output=True,
                text=True,
                timeout=300
            )
        else:
            # Use existing container (latest version)
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

@app.route('/api/device/<instance>/<device_name>/compile/stream', methods=['POST'])
def compile_device_stream(instance, device_name):
    """Compile device firmware with real-time streaming output"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    def generate():
        try:
            # Check if device has pinned ESPHome version
            config_dir = Path(instance_config["config_dir"])
            yaml_file = config_dir / f"{device_name}.yaml"

            pinned_version = None
            if yaml_file.exists():
                with yaml_file.open() as f:
                    config = yaml.safe_load(f)
                    substitutions = config.get("substitutions", {})
                    pinned_version = substitutions.get("esphome_version", "*")

            # Build command
            if pinned_version and pinned_version != "*":
                cmd = ["docker", "run", "--rm",
                       "-v", f"{config_dir}:/config",
                       f"esphome/esphome:{pinned_version}",
                       "compile", f"/config/{device_name}.yaml"]
            else:
                cmd = ["docker", "exec", instance_config["container"],
                       "esphome", "compile", f"/config/{device_name}.yaml"]

            # Start process with real-time output
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            # Stream output line by line
            for line in iter(process.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"

            process.wait()

            # Send completion status
            yield f"data: {json.dumps({'done': True, 'success': process.returncode == 0})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/api/device/<instance>/<device_name>/upload', methods=['POST'])
def upload_device(instance, device_name):
    """Upload firmware to device (OTA)"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    try:
        # Check if device has pinned ESPHome version
        config_dir = Path(instance_config["config_dir"])
        yaml_file = config_dir / f"{device_name}.yaml"

        pinned_version = None
        if yaml_file.exists():
            with yaml_file.open() as f:
                config = yaml.safe_load(f)
                substitutions = config.get("substitutions", {})
                pinned_version = substitutions.get("esphome_version", "*")

        # Use version-specific Docker image if pinned
        if pinned_version and pinned_version != "*":
            result = subprocess.run(
                ["docker", "run", "--rm", "--network", "host",
                 "-v", f"{config_dir}:/config",
                 f"esphome/esphome:{pinned_version}",
                 "upload", f"/config/{device_name}.yaml", "--device", "OTA"],
                capture_output=True,
                text=True,
                timeout=300
            )
        else:
            # Use existing container (latest version)
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

@app.route('/api/device/<instance>/<device_name>/upload/stream', methods=['POST'])
def upload_device_stream(instance, device_name):
    """Upload firmware to device (OTA) with real-time streaming output"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    def generate():
        try:
            # Check if device has pinned ESPHome version
            config_dir = Path(instance_config["config_dir"])
            yaml_file = config_dir / f"{device_name}.yaml"

            pinned_version = None
            if yaml_file.exists():
                with yaml_file.open() as f:
                    config = yaml.safe_load(f)
                    substitutions = config.get("substitutions", {})
                    pinned_version = substitutions.get("esphome_version", "*")

            # Build command
            if pinned_version and pinned_version != "*":
                cmd = ["docker", "run", "--rm", "--network", "host",
                       "-v", f"{config_dir}:/config",
                       f"esphome/esphome:{pinned_version}",
                       "upload", f"/config/{device_name}.yaml", "--device", "OTA"]
            else:
                cmd = ["docker", "exec", instance_config["container"],
                       "esphome", "upload", f"/config/{device_name}.yaml", "--device", "OTA"]

            # Start process with real-time output
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )

            # Stream output line by line
            for line in iter(process.stdout.readline, ''):
                if line:
                    yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"

            process.wait()

            # Send completion status
            yield f"data: {json.dumps({'done': True, 'success': process.returncode == 0})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/api/device/<instance>/<device_name>/logs')
def get_device_logs(instance, device_name):
    """Get device logs (requires device to be online)"""
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

        # Check if command succeeded
        if result.returncode != 0:
            error_output = result.stderr or result.stdout
            # Check for common errors
            if "Can't connect" in error_output or "timed out" in error_output:
                return jsonify({"error": "Device is offline or unreachable"}), 503
            return jsonify({
                "error": "Failed to retrieve logs",
                "output": error_output
            }), 500

        return jsonify({
            "success": True,
            "logs": result.stdout or "No logs available"
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Device connection timed out (device may be offline)"}), 504
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

@app.route('/api/device/<instance>/<device_name>/firmware')
def download_firmware(instance, device_name):
    """Download compiled firmware binary for UART flashing"""
    instance_config = get_instance_config(instance)
    if not instance_config:
        return jsonify({"error": "Instance not found"}), 404

    try:
        config_dir = Path(instance_config["config_dir"])
        build_dir = config_dir / ".esphome" / "build" / device_name

        # Check if device has been compiled
        if not build_dir.exists():
            return jsonify({
                "error": "Device has not been compiled yet. Click 'Compile' first, then download firmware."
            }), 404

        # Common firmware locations - try multiple naming conventions
        firmware_paths = [
            build_dir / ".pioenvs" / device_name / "firmware.bin",
            build_dir / ".pioenvs" / device_name / "firmware.factory.bin",
            build_dir / ".pioenvs" / device_name / "firmware-factory.bin",
            build_dir / "firmware.bin",
            build_dir / "firmware.factory.bin",
        ]

        # Find first existing firmware
        for firmware_path in firmware_paths:
            if firmware_path.exists():
                return send_from_directory(
                    firmware_path.parent,
                    firmware_path.name,
                    as_attachment=True,
                    download_name=f"{device_name}_firmware.bin"
                )

        # Build directory exists but no firmware found
        pioenvs_dir = build_dir / ".pioenvs" / device_name
        available_files = []
        if pioenvs_dir.exists():
            available_files = [f.name for f in pioenvs_dir.iterdir() if f.is_file()]

        return jsonify({
            "error": "Firmware binary not found in expected locations.",
            "build_dir": str(build_dir),
            "available_files": available_files,
            "hint": "Try compiling the device again."
        }), 404

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
# SETTINGS & CONFIGURATION API
# ============================================================================

@app.route('/api/config')
def get_config():
    """Get dashboard configuration for Settings UI"""
    # Return safe config (without sensitive data like passwords)
    safe_config = {
        "instances": CONFIG.get("instances", []),
        "homeassistant": CONFIG.get("homeassistant", {}),
        "settings": CONFIG.get("settings", {
            "ping_workers": 100,
            "compile_timeout": 300,
            "upload_timeout": 300
        })
    }
    return jsonify(safe_config)

@app.route('/api/config', methods=['POST'])
def update_config():
    """Update dashboard configuration"""
    try:
        data = request.json

        # Update in-memory config
        if "instances" in data:
            CONFIG["instances"] = data["instances"]
        if "homeassistant" in data:
            CONFIG["homeassistant"] = data["homeassistant"]
        if "settings" in data:
            CONFIG["settings"] = data["settings"]

        # Write to file
        with CONFIG_FILE.open('w') as f:
            yaml.dump(CONFIG, f, default_flow_style=False)

        return jsonify({"success": True, "message": "Configuration updated"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/check-substitutions')
def check_substitutions():
    """Check all device YAMLs for required and recommended substitutions"""
    required_substitutions = ['device_type', 'esphome_version', 'area']
    recommended_substitutions = ['physical_location']
    results = {
        'total_devices': 0,
        'compliant_devices': 0,
        'missing_recommended': 0,
        'issues': [],
        'recommended_issues': [],
        'by_instance': {}
    }

    try:
        for instance_config in CONFIG.get("instances", []):
            if not instance_config.get("enabled"):
                continue

            instance_name = instance_config["name"]
            config_dir = Path(instance_config["config_dir"])

            if not config_dir.exists():
                continue

            instance_issues = []
            instance_recommended_issues = []

            for yaml_file in sorted(config_dir.glob("*.yaml")):
                try:
                    with yaml_file.open() as f:
                        config = yaml.safe_load(f)

                    substitutions = config.get("substitutions", {})
                    missing_required = []
                    missing_recommended = []

                    # Check required substitutions
                    for req_sub in required_substitutions:
                        if req_sub not in substitutions:
                            missing_required.append(req_sub)

                    # Check recommended substitutions
                    for rec_sub in recommended_substitutions:
                        if rec_sub not in substitutions:
                            missing_recommended.append(rec_sub)

                    results['total_devices'] += 1

                    # Track required issues
                    if missing_required:
                        instance_issues.append({
                            'device': yaml_file.stem,
                            'missing': missing_required,
                            'has': list(substitutions.keys())
                        })
                    else:
                        results['compliant_devices'] += 1

                    # Track recommended issues separately
                    if missing_recommended:
                        instance_recommended_issues.append({
                            'device': yaml_file.stem,
                            'missing': missing_recommended,
                            'has': list(substitutions.keys())
                        })
                        results['missing_recommended'] += 1

                except Exception as e:
                    instance_issues.append({
                        'device': yaml_file.stem,
                        'error': str(e)
                    })

            if instance_issues:
                results['issues'].extend([{**issue, 'instance': instance_name} for issue in instance_issues])
                results['by_instance'][instance_name] = len(instance_issues)

            if instance_recommended_issues:
                results['recommended_issues'].extend([{**issue, 'instance': instance_name} for issue in instance_recommended_issues])

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================================
# BULK OPERATIONS API
# ============================================================================

def execute_bulk_operation(operation_id: int, operation_type: str, device_list: List[Dict]):
    """Execute bulk operation in background thread"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        success_count = 0
        failure_count = 0

        for device_info in device_list:
            instance_slug = device_info.get("instance")
            device_name = device_info.get("name")
            start_time = datetime.now()

            try:
                # Get instance config
                instance_config = get_instance_config(instance_slug)
                if not instance_config:
                    raise Exception(f"Instance {instance_slug} not found")

                config_dir = Path(instance_config["config_dir"])
                yaml_file = config_dir / f"{device_name}.yaml"

                # Check for pinned version
                pinned_version = None
                if yaml_file.exists():
                    with yaml_file.open() as f:
                        config = yaml.safe_load(f)
                        substitutions = config.get("substitutions", {})
                        pinned_version = substitutions.get("esphome_version", "*")

                # Build command based on operation type
                if pinned_version and pinned_version != "*":
                    cmd = ["docker", "run", "--rm"]
                    if operation_type == "upload":
                        cmd.append("--network")
                        cmd.append("host")
                    cmd.extend([
                        "-v", f"{config_dir}:/config",
                        f"esphome/esphome:{pinned_version}",
                        operation_type, f"/config/{device_name}.yaml"
                    ])
                    if operation_type == "upload":
                        cmd.extend(["--device", "OTA"])
                else:
                    cmd = ["docker", "exec", instance_config["container"],
                           "esphome", operation_type, f"/config/{device_name}.yaml"]
                    if operation_type == "upload":
                        cmd.extend(["--device", "OTA"])

                # Execute command
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300
                )

                end_time = datetime.now()
                duration = int((end_time - start_time).total_seconds())

                if result.returncode == 0:
                    success_count += 1
                    status = "success"
                    error_msg = None
                else:
                    failure_count += 1
                    status = "failed"
                    error_msg = result.stderr[:500] if result.stderr else "Unknown error"

                # Store result
                c.execute('''
                    INSERT INTO fleet_manager.operation_results
                    (operation_id, device_name, instance, status, error_message, output, started_at, completed_at, duration_seconds)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (operation_id, device_name, instance_slug, status, error_msg,
                      (result.stdout + result.stderr)[:1000], start_time, end_time, duration))
                conn.commit()

            except Exception as e:
                failure_count += 1
                end_time = datetime.now()
                duration = int((end_time - start_time).total_seconds())

                # Store error
                c.execute('''
                    INSERT INTO fleet_manager.operation_results
                    (operation_id, device_name, instance, status, error_message, started_at, completed_at, duration_seconds)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (operation_id, device_name, instance_slug, "failed", str(e)[:500], start_time, end_time, duration))
                conn.commit()

        # Update operation status
        c.execute('''
            UPDATE fleet_manager.operations
            SET status = 'completed', success_count = %s, failure_count = %s, completed_at = NOW()
            WHERE id = %s
        ''', (success_count, failure_count, operation_id))
        conn.commit()

        print(f"✅ Bulk operation {operation_id} completed: {success_count} success, {failure_count} failed")

    except Exception as e:
        print(f"❌ Bulk operation {operation_id} failed: {e}")
        if conn:
            try:
                c.execute('''
                    UPDATE fleet_manager.operations
                    SET status = 'failed', completed_at = NOW()
                    WHERE id = %s
                ''', (operation_id,))
                conn.commit()
            except:
                pass
    finally:
        if conn:
            conn.close()

@app.route('/api/bulk-operation', methods=['POST'])
def start_bulk_operation():
    """Start a bulk operation (compile/upload/validate) on multiple devices"""
    try:
        data = request.json
        operation_type = data.get("operation")  # compile, upload, validate
        device_list = data.get("devices", [])  # List of {instance, name, yaml_filename}

        if not operation_type or not device_list:
            return jsonify({"error": "Missing operation or devices"}), 400

        # Normalize device list to use yaml_filename
        normalized_devices = []
        for device in device_list:
            normalized_devices.append({
                "instance": device.get("instance"),
                "name": device.get("yaml_filename") or device.get("name")
            })

        # Create operation log in database
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO fleet_manager.operations
            (operation_type, device_count, status, triggered_by)
            VALUES (%s, %s, 'running', 'web-ui')
            RETURNING id
        ''', (operation_type, len(normalized_devices)))
        operation_id = c.fetchone()[0]
        conn.commit()
        conn.close()

        # Start background thread to execute operation
        thread = threading.Thread(
            target=execute_bulk_operation,
            args=(operation_id, operation_type, normalized_devices),
            daemon=True
        )
        thread.start()

        return jsonify({
            "success": True,
            "operation_id": operation_id,
            "message": f"Started {operation_type} on {len(normalized_devices)} devices"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/operations/<int:operation_id>/progress')
def get_operation_progress(operation_id):
    """Get real-time progress of a running operation"""
    try:
        conn = get_db_connection()
        c = conn.cursor(cursor_factory=RealDictCursor)

        # Get operation info
        c.execute('''
            SELECT id, operation_type, device_count, success_count, failure_count,
                   started_at, completed_at, status
            FROM fleet_manager.operations
            WHERE id = %s
        ''', (operation_id,))
        operation = c.fetchone()

        if not operation:
            return jsonify({"error": "Operation not found"}), 404

        # Get completed device results
        c.execute('''
            SELECT device_name, instance, status, error_message, completed_at
            FROM fleet_manager.operation_results
            WHERE operation_id = %s
            ORDER BY completed_at DESC
        ''', (operation_id,))
        results = c.fetchall()
        conn.close()

        # Convert timestamps
        if operation['started_at']:
            operation['started_at'] = operation['started_at'].isoformat()
        if operation['completed_at']:
            operation['completed_at'] = operation['completed_at'].isoformat()

        for result in results:
            if result['completed_at']:
                result['completed_at'] = result['completed_at'].isoformat()

        completed_count = len(results)
        in_progress_count = operation['device_count'] - completed_count

        return jsonify({
            "operation": operation,
            "completed_count": completed_count,
            "in_progress_count": in_progress_count,
            "results": results
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/operations')
def get_operations():
    """Get operation history"""
    try:
        limit = request.args.get('limit', 50, type=int)

        conn = get_db_connection()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT id, operation_type, device_count, success_count, failure_count,
                   started_at, completed_at, status, triggered_by
            FROM fleet_manager.operations
            ORDER BY started_at DESC
            LIMIT %s
        ''', (limit,))
        operations = c.fetchall()
        conn.close()

        # Convert to JSON-serializable format
        for op in operations:
            if op['started_at']:
                op['started_at'] = op['started_at'].isoformat()
            if op['completed_at']:
                op['completed_at'] = op['completed_at'].isoformat()

        return jsonify({"operations": operations})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/operations/<int:operation_id>')
def get_operation_details(operation_id):
    """Get detailed results for a specific operation"""
    try:
        conn = get_db_connection()
        c = conn.cursor(cursor_factory=RealDictCursor)

        # Get operation info
        c.execute('''
            SELECT * FROM fleet_manager.operations WHERE id = %s
        ''', (operation_id,))
        operation = c.fetchone()

        if not operation:
            return jsonify({"error": "Operation not found"}), 404

        # Get device results
        c.execute('''
            SELECT device_name, instance, status, error_message,
                   started_at, completed_at, duration_seconds
            FROM fleet_manager.operation_results
            WHERE operation_id = %s
            ORDER BY completed_at DESC
        ''', (operation_id,))
        results = c.fetchall()
        conn.close()

        # Convert timestamps
        if operation['started_at']:
            operation['started_at'] = operation['started_at'].isoformat()
        if operation['completed_at']:
            operation['completed_at'] = operation['completed_at'].isoformat()

        for result in results:
            if result['started_at']:
                result['started_at'] = result['started_at'].isoformat()
            if result['completed_at']:
                result['completed_at'] = result['completed_at'].isoformat()

        return jsonify({
            "operation": operation,
            "results": results
        })

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
