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

import re
import tornado.web
import tornado.ioloop
import tornado.websocket
import tornado.gen
from pathlib import Path
import json
import subprocess
import yaml
import time
from datetime import datetime
from typing import List, Dict, Optional
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
import aioesphomeapi
import threading
import requests

# Add custom YAML constructors to handle ESPHome directives
def include_constructor(loader, node):
    """Dummy constructor for !include - just return empty dict"""
    return {}

def lambda_constructor(loader, node):
    """Dummy constructor for !lambda - just return empty string"""
    return ""

yaml.add_constructor('!include', include_constructor, Loader=yaml.SafeLoader)
yaml.add_constructor('!lambda', lambda_constructor, Loader=yaml.SafeLoader)

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
                "dashboard_url": "https://codex.grid.foxrunn.net:6052",
                "enabled": True
            },
            {
                "name": "Lab",
                "slug": "lab",
                "config_dir": "/opt/data-services/esphome/lab",
                "container": "codex-esphome-lab",
                "dashboard_url": "https://codex.grid.foxrunn.net:6054",
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

# Global cache for HA connection status
_ha_status_cache = {}
_ha_status_cache_time = 0

# Per-instance device list cache (30-second TTL)
_device_cache: Dict[str, List[Dict]] = {}
_device_cache_time: Dict[str, float] = {}
DEVICE_CACHE_TTL = 30

def discover_devices_cached(instance: Dict) -> List[Dict]:
    """Cached wrapper for discover_devices — re-reads YAMLs at most every 30s"""
    slug = instance["slug"]
    now = time.time()
    if slug in _device_cache and now - _device_cache_time.get(slug, 0) < DEVICE_CACHE_TTL:
        return _device_cache[slug]
    devices = discover_devices(instance)
    _device_cache[slug] = devices
    _device_cache_time[slug] = now
    return devices

# Global cache for ESPHome latest version
_esphome_latest_version = None
_esphome_version_cache_time = 0

def get_ha_device_status() -> Dict[str, str]:
    """Query Home Assistant for ESPHome device connection status"""
    global _ha_status_cache, _ha_status_cache_time

    # Cache for 10 seconds to avoid hammering HA
    if time.time() - _ha_status_cache_time < 10:
        return _ha_status_cache

    try:
        # Query HA for all ESPHome connection status sensors
        ha_cfg = CONFIG.get("homeassistant", {})
        ha_base_url = ha_cfg.get("url", "").rstrip("/")
        ha_token = ha_cfg.get("token", "") or os.environ.get("HA_TOKEN", "")
        if not ha_base_url or not ha_token:
            return {}
        ha_url = f"{ha_base_url}/api/states"

        headers = {
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json"
        }

        response = requests.get(ha_url, headers=headers, timeout=5, verify=False)
        if response.status_code == 200:
            states = response.json()
            status_map = {}

            # Find all connection_status sensors and extract device names
            for entity in states:
                entity_id = entity.get("entity_id", "")
                if "connection_status" in entity_id and entity_id.startswith("binary_sensor."):
                    # Extract device name from entity_id
                    # Format: binary_sensor.devicename_something_connection_status
                    parts = entity_id.replace("binary_sensor.", "").split("_connection_status")[0]
                    # Get the device name (first part before underscores for compound names)
                    device_parts = parts.split("_")
                    if device_parts:
                        device_name = device_parts[0]  # e.g., "sp101", "mjs007"
                        state = entity.get("state", "off")
                        status_map[device_name] = "online" if state == "on" else "offline"

            _ha_status_cache = status_map
            _ha_status_cache_time = time.time()
            return status_map
    except Exception as e:
        print(f"  Error querying HA for device status: {e}")

    return {}

def get_latest_esphome_version() -> Optional[str]:
    """Query GitHub API for latest ESPHome release"""
    global _esphome_latest_version, _esphome_version_cache_time

    # Cache for 1 hour to avoid hammering GitHub
    if time.time() - _esphome_version_cache_time < 3600:
        return _esphome_latest_version

    try:
        url = "https://api.github.com/repos/esphome/esphome/releases/latest"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            version = data.get("tag_name", "").lstrip("v")  # Remove 'v' prefix
            _esphome_latest_version = version
            _esphome_version_cache_time = time.time()
            print(f"Latest ESPHome version: {version}")
            return version
    except Exception as e:
        print(f"Error checking ESPHome releases: {e}")

    return _esphome_latest_version

def check_device_online_tcp(ip: str) -> str:
    """Fallback: check device online via TCP port 6053 (for devices not in HA)"""
    if not ip:
        return "unknown"

    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect((ip, 6053))
        sock.close()
        return "online"
    except:
        return "offline"

def check_device_online(ip: str, device_name: str = "") -> str:
    """Check if device is online via Home Assistant ESPHome integration, fallback to TCP"""
    if not device_name:
        return "unknown"

    # Get status from HA
    ha_status = get_ha_device_status()

    # Check if we have status for this device in HA
    if device_name in ha_status:
        return ha_status[device_name]

    # Device not in HA - fall back to TCP port check for new/unadded devices
    return check_device_online_tcp(ip)

async def get_device_version_async(ip: str, password: str = "") -> Optional[str]:
    """Query ESPHome device via API to get running version"""
    if not ip:
        return None

    try:
        cli = aioesphomeapi.APIClient(ip, 6053, password)
        await asyncio.wait_for(cli.connect(login=True), timeout=3.0)
        device_info = await asyncio.wait_for(cli.device_info(), timeout=2.0)
        await cli.disconnect()
        return device_info.esphome_version
    except (asyncio.TimeoutError, ConnectionError, OSError):
        return None
    except Exception:
        return None

def get_device_version(ip: str) -> Optional[str]:
    """Wrapper to run async version query in sync context"""
    try:
        return asyncio.run(get_device_version_async(ip))
    except Exception:
        return None

async def _query_one_device(name: str, ip: str) -> tuple:
    """Return (name, version_or_None) for a single device"""
    version = await get_device_version_async(ip)
    return name, version


def _compute_update_available(
    deployed_version: Optional[str],
    pinned_version: str,
    instance_slug: str,
    container_versions: Dict[str, str]
) -> bool:
    """Return True if the device's deployed firmware is behind its target version"""
    if not deployed_version:
        return False
    if pinned_version and pinned_version != "*":
        # Device is pinned to a specific ESPHome version
        return deployed_version != pinned_version
    # Device follows the running container
    container_ver = container_versions.get(instance_slug)
    return bool(container_ver and deployed_version != container_ver)


def discover_devices(instance: Dict) -> List[Dict]:
    """Discover all ESPHome devices for an instance"""
    config_dir = Path(instance["config_dir"])
    devices = []

    if not config_dir.exists():
        return devices

    container_versions = get_builder_current_versions()

    # First pass: collect all device info without pinging
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        try:
            with yaml_file.open() as f:
                config = yaml.safe_load(f)

            # Extract substitutions first
            substitutions = config.get("substitutions", {})

            # Extract device info from YAML
            esphome_config = config.get("esphome", {})
            name = esphome_config.get("name") or yaml_file.stem

            # Resolve substitution variables
            if isinstance(name, str) and name.startswith("${") and name.endswith("}"):
                sub_var = name[2:-1]
                name = substitutions.get(sub_var, yaml_file.stem)
            area = substitutions.get("area", "Unknown")
            physical_location = substitutions.get("physical_location")

            # Get ESPHome version from substitutions
            esphome_version = substitutions.get("esphome_version", "*")

            # Get device type from substitutions
            device_type = substitutions.get("device_type", "unknown")

            # Parse device name for room
            clean_name = name.replace("vd-", "").replace("_", "-")
            parts = clean_name.split("-")

            # Extract room from device name
            room = area
            if len(parts) > 1:
                room_parts = parts[1:]
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
                "pinned_version": esphome_version,
                "room": room,
                "area": area,
                "physical_location": physical_location,
                "ip_address": ip,
                "config_file": yaml_file.name,
                "yaml_filename": yaml_file.stem,
                "status": "unknown",
                "deployed_version": deployed_version,
                "current_version": None,
                "update_available": _compute_update_available(
                    deployed_version, esphome_version, instance["slug"], container_versions
                )
            })

        except Exception as e:
            print(f"Error parsing {yaml_file}: {e}")
            continue

    # Second pass: get device status from Home Assistant
    print(f"Checking status for {len(devices)} devices in {instance['name']} via HA...")
    ha_status = get_ha_device_status()

    for device in devices:
        device_name = device["name"]
        device["status"] = check_device_online(device["ip_address"], device_name)

    online_count = sum(1 for d in devices if d["status"] == "online")
    unknown_count = sum(1 for d in devices if d["status"] == "unknown")
    print(f"  {online_count}/{len(devices)} devices online ({unknown_count} unknown - not in HA)")

    return devices

# ============================================================================
# HELPERS
# ============================================================================

def get_instance_config(instance_slug: str) -> Optional[Dict]:
    """Get instance configuration by slug"""
    for instance in CONFIG["instances"]:
        if instance["slug"] == instance_slug:
            return instance
    return None

# ============================================================================
# BASE WEBSOCKET HANDLER
# ============================================================================

class BaseWebSocketHandler(tornado.websocket.WebSocketHandler):
    """Base WebSocket handler with CORS support"""

    def check_origin(self, origin):
        """Allow all origins for now"""
        return True

    def open(self, *args, **kwargs):
        """Called when WebSocket connection is opened"""
        self.set_nodelay(True)  # Disable Nagle algorithm for real-time streaming
        print(f"WebSocket opened: {self.__class__.__name__}")

    def on_close(self):
        """Called when WebSocket connection is closed"""
        print(f"WebSocket closed: {self.__class__.__name__}")

# ============================================================================
# COMPILE WEBSOCKET HANDLER
# ============================================================================

class CompileWebSocketHandler(BaseWebSocketHandler):
    """WebSocket handler for device compilation with real-time output streaming"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proc = None

    async def on_message(self, message):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "spawn":
                await self.handle_compile(data)
            else:
                await self.write_message(json.dumps({
                    "type": "error",
                    "data": f"Unknown message type: {msg_type}"
                }))
        except Exception as e:
            await self.write_message(json.dumps({
                "type": "error",
                "data": str(e)
            }))

    async def handle_compile(self, data):
        """Start compilation and stream output"""
        if self._proc is not None:
            return

        try:
            instance = data.get("instance")
            device_name = data.get("device")

            if not instance or not device_name:
                await self.write_message(json.dumps({
                    "type": "error",
                    "data": "Missing instance or device name"
                }))
                return

            instance_config = get_instance_config(instance)
            if not instance_config:
                await self.write_message(json.dumps({
                    "type": "error",
                    "data": f"Instance {instance} not found"
                }))
                return

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
                # Use script to force unbuffered output
                docker_cmd = f"docker exec {instance_config['container']} esphome compile /config/{device_name}.yaml"
                cmd = ["script", "-qfc", docker_cmd, "/dev/null"]

            # Use Tornado's subprocess
            self._proc = tornado.process.Subprocess(
                cmd,
                stdout=tornado.process.Subprocess.STREAM,
                stderr=subprocess.STDOUT
            )

            # Spawn stdout reader
            tornado.ioloop.IOLoop.current().spawn_callback(self._stream_output)

        except Exception as e:
            await self.write_message(json.dumps({
                "type": "error",
                "data": str(e)
            }))

    @tornado.gen.coroutine
    def _stream_output(self):
        """Stream subprocess output to WebSocket"""
        try:
            while True:
                try:
                    # Read until newline or carriage return
                    line = yield self._proc.stdout.read_until_regex(b"[\n\r]")
                    text = line.decode("utf-8", "replace").rstrip()

                    self.write_message(json.dumps({
                        "type": "line",
                        "data": text
                    }))
                except tornado.iostream.StreamClosedError:
                    break
                except Exception as e:
                    print(f"Error reading stdout: {e}")
                    break

            # Wait for process to exit
            yield self._proc.wait_for_exit(raise_error=False)

            # Send completion message
            self.write_message(json.dumps({
                "type": "exit",
                "code": self._proc.returncode
            }))

        except tornado.websocket.WebSocketClosedError:
            # Client disconnected, kill process
            if self._proc and self._proc.returncode is None:
                self._proc.proc.kill()
        except Exception as e:
            try:
                self.write_message(json.dumps({
                    "type": "error",
                    "data": str(e)
                }))
            except:
                pass

    def on_close(self):
        """Clean up when WebSocket closes"""
        if self._proc and self._proc.returncode is None:
            self._proc.proc.kill()
        super().on_close()

# ============================================================================
# UPLOAD WEBSOCKET HANDLER
# ============================================================================

class UploadWebSocketHandler(BaseWebSocketHandler):
    """WebSocket handler for device upload (OTA) with real-time output streaming"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proc = None

    async def on_message(self, message):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "spawn":
                await self.handle_upload(data)
            else:
                await self.write_message(json.dumps({
                    "type": "error",
                    "data": f"Unknown message type: {msg_type}"
                }))
        except Exception as e:
            await self.write_message(json.dumps({
                "type": "error",
                "data": str(e)
            }))

    async def handle_upload(self, data):
        """Start OTA upload and stream output"""
        if self._proc is not None:
            return

        try:
            instance = data.get("instance")
            device_name = data.get("device")

            if not instance or not device_name:
                await self.write_message(json.dumps({
                    "type": "error",
                    "data": "Missing instance or device name"
                }))
                return

            instance_config = get_instance_config(instance)
            if not instance_config:
                await self.write_message(json.dumps({
                    "type": "error",
                    "data": f"Instance {instance} not found"
                }))
                return

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
                       "run", f"/config/{device_name}.yaml", "--device", "OTA"]
            else:
                # Use script to force unbuffered output
                docker_cmd = f"docker exec {instance_config['container']} esphome run /config/{device_name}.yaml --device OTA"
                cmd = ["script", "-qfc", docker_cmd, "/dev/null"]

            # Use Tornado's subprocess
            self._proc = tornado.process.Subprocess(
                cmd,
                stdout=tornado.process.Subprocess.STREAM,
                stderr=subprocess.STDOUT
            )

            # Spawn stdout reader
            tornado.ioloop.IOLoop.current().spawn_callback(self._stream_output)

        except Exception as e:
            await self.write_message(json.dumps({
                "type": "error",
                "data": str(e)
            }))

    @tornado.gen.coroutine
    def _stream_output(self):
        """Stream subprocess output to WebSocket"""
        try:
            while True:
                try:
                    # Read until newline or carriage return
                    line = yield self._proc.stdout.read_until_regex(b"[\n\r]")
                    text = line.decode("utf-8", "replace").rstrip()

                    self.write_message(json.dumps({
                        "type": "line",
                        "data": text
                    }))
                except tornado.iostream.StreamClosedError:
                    break
                except Exception as e:
                    print(f"Error reading stdout: {e}")
                    break

            # Wait for process to exit
            yield self._proc.wait_for_exit(raise_error=False)

            # Send completion message
            self.write_message(json.dumps({
                "type": "exit",
                "code": self._proc.returncode
            }))

        except tornado.websocket.WebSocketClosedError:
            # Client disconnected, kill process
            if self._proc and self._proc.returncode is None:
                self._proc.proc.kill()
        except Exception as e:
            try:
                self.write_message(json.dumps({
                    "type": "error",
                    "data": str(e)
                }))
            except:
                pass

    def on_close(self):
        """Clean up when WebSocket closes"""
        if self._proc and self._proc.returncode is None:
            self._proc.proc.kill()
        super().on_close()

# ============================================================================
# LOGS WEBSOCKET HANDLER
# ============================================================================

class LogsWebSocketHandler(BaseWebSocketHandler):
    """WebSocket handler for live device log streaming via esphome logs"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proc = None

    async def on_message(self, message):
        try:
            data = json.loads(message)
            if data.get("type") == "spawn":
                await self.handle_logs(data)
            elif data.get("type") == "stop":
                if self._proc and self._proc.returncode is None:
                    self._proc.proc.kill()
        except Exception as e:
            await self.write_message(json.dumps({"type": "error", "data": str(e)}))

    async def handle_logs(self, data):
        if self._proc is not None:
            return

        instance = data.get("instance")
        device_name = data.get("device")

        if not instance or not device_name:
            await self.write_message(json.dumps({"type": "error", "data": "Missing instance or device name"}))
            return

        instance_config = get_instance_config(instance)
        if not instance_config:
            await self.write_message(json.dumps({"type": "error", "data": f"Instance {instance} not found"}))
            return

        # esphome logs runs indefinitely; wrap in script to force unbuffered output
        docker_cmd = f"docker exec {instance_config['container']} esphome logs /config/{device_name}.yaml --no-color"
        cmd = ["script", "-qfc", docker_cmd, "/dev/null"]

        try:
            self._proc = tornado.process.Subprocess(
                cmd,
                stdout=tornado.process.Subprocess.STREAM,
                stderr=subprocess.STDOUT
            )
            tornado.ioloop.IOLoop.current().spawn_callback(self._stream_output)
        except Exception as e:
            await self.write_message(json.dumps({"type": "error", "data": str(e)}))

    @tornado.gen.coroutine
    def _stream_output(self):
        try:
            while True:
                try:
                    line = yield self._proc.stdout.read_until_regex(b"[\n\r]")
                    text = line.decode("utf-8", "replace").rstrip()
                    self.write_message(json.dumps({"type": "line", "data": text}))
                except tornado.iostream.StreamClosedError:
                    break
                except Exception:
                    break

            yield self._proc.wait_for_exit(raise_error=False)
            self.write_message(json.dumps({"type": "exit", "code": self._proc.returncode}))

        except tornado.websocket.WebSocketClosedError:
            if self._proc and self._proc.returncode is None:
                self._proc.proc.kill()
        except Exception as e:
            try:
                self.write_message(json.dumps({"type": "error", "data": str(e)}))
            except Exception:
                pass

    def on_close(self):
        if self._proc and self._proc.returncode is None:
            self._proc.proc.kill()
        super().on_close()


# ============================================================================
# REST API HANDLERS
# ============================================================================

class MainHandler(tornado.web.RequestHandler):
    """Main dashboard page"""
    def get(self):
        self.render("templates/index.html", config=CONFIG)

class InstancesHandler(tornado.web.RequestHandler):
    """Get all ESPHome instances"""
    def get(self):
        self.write({"instances": CONFIG["instances"]})

class DevicesHandler(tornado.web.RequestHandler):
    """Get all devices from all instances"""
    def get(self):
        instance_filter = self.get_argument('instance', None)
        type_filter = self.get_argument('type', None)
        status_filter = self.get_argument('status', None)
        search = self.get_argument('search', '').lower()

        all_devices = []

        for instance in CONFIG["instances"]:
            if not instance.get("enabled"):
                continue

            if instance_filter and instance["slug"] != instance_filter:
                continue

            devices = discover_devices_cached(instance)
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
                   search in d.get("friendly_name", "").lower() or
                   search in d["device_type"].lower() or
                   search in d.get("room", "").lower() or
                   search in d.get("area", "").lower()
            ]

        self.write({
            "devices": all_devices,
            "total": len(all_devices)
        })

class DeviceTypesHandler(tornado.web.RequestHandler):
    """Get unique device types across all instances"""
    def get(self):
        types = set()

        for instance in CONFIG["instances"]:
            if not instance.get("enabled"):
                continue
            devices = discover_devices_cached(instance)
            types.update(d["device_type"] for d in devices)

        self.write({"types": sorted(list(types))})

class StatsHandler(tornado.web.RequestHandler):
    """Get fleet statistics"""
    def get(self):
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

            devices = discover_devices_cached(instance)
            instance_count = len(devices)

            stats["total_devices"] += instance_count
            stats["by_instance"][instance["name"]] = instance_count

            for device in devices:
                dtype = device["device_type"]
                stats["by_type"][dtype] = stats["by_type"].get(dtype, 0) + 1

                if device["status"] == "online":
                    stats["online"] += 1
                elif device["status"] == "offline":
                    stats["offline"] += 1
                if device.get("update_available"):
                    stats["updates_available"] += 1

        return self.write(stats)

class DeviceDetailHandler(tornado.web.RequestHandler):
    """Get detailed device information"""
    def get(self, instance, device_name):
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        config_dir = Path(instance_config["config_dir"])
        yaml_file = config_dir / f"{device_name}.yaml"

        if not yaml_file.exists():
            self.set_status(404)
            return self.write({"error": "Device not found"})

        # Read storage JSON for full details
        storage_json = config_dir / ".esphome" / "storage" / f"{device_name}.yaml.json"
        device_info = {"name": device_name, "instance": instance}

        if storage_json.exists():
            with storage_json.open() as f:
                device_info.update(json.load(f))

        self.write(device_info)

class DeviceConfigHandler(tornado.web.RequestHandler):
    """Get/save device YAML configuration"""
    def get(self, instance, device_name):
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        config_dir = Path(instance_config["config_dir"])
        yaml_file = config_dir / f"{device_name}.yaml"

        if not yaml_file.exists():
            self.set_status(404)
            return self.write({"error": "Device not found"})

        with yaml_file.open() as f:
            content = f.read()

        self.write({"config": content})

    def post(self, instance, device_name):
        """Save device YAML configuration"""
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        config_dir = Path(instance_config["config_dir"])
        yaml_file = config_dir / f"{device_name}.yaml"

        if not yaml_file.exists():
            self.set_status(404)
            return self.write({"error": "Device not found"})

        data = json.loads(self.request.body)
        new_content = data.get("config", "")

        if not new_content:
            self.set_status(400)
            return self.write({"error": "No content provided"})

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

            self.write({"success": True, "message": "Configuration saved"})

        except Exception as e:
            # Restore backup on error
            if backup_file.exists():
                backup_file.rename(yaml_file)
            self.set_status(500)
            self.write({"error": f"Failed to save: {str(e)}"})

_ESP32_BOARDS = {"esp32dev", "esp32-c3-devkitm-1", "esp32-s2-saola-1", "esp32-s3-devkitc-1"}
_ESP8266_BOARDS = {"nodemcuv2", "d1_mini", "esp01_1m", "esp8285"}

def _device_yaml_template(data: dict) -> str:
    """Generate a starter YAML for a new ESPHome device matching fleet conventions"""
    name = data["device_name"]
    board = data.get("board", "esp32dev")
    esphome_version = data.get("esphome_version") or "*"
    static_ip = data.get("static_ip", "")

    if board in _ESP32_BOARDS or board.startswith("esp32"):
        platform_block = f"esp32:\n  board: {board}"
        keepalive = "common/keepalive-esp32.yaml"
    else:
        platform_block = f"esp8266:\n  board: {board}\n  restore_from_flash: true"
        keepalive = "common/keepalive-esp8266.yaml"

    ip_line = f"\n  device_static_ip: {static_ip}" if static_ip else ""

    return f"""substitutions:
  name: {name}
  friendly_name: {data.get("friendly_name", name)}
  area: {data.get("area", "Unknown")}{ip_line}
  esphome_version: "{esphome_version}"
  device_type: "{data.get("device_type", "unknown")}"
  physical_location: "{data.get("physical_location", "")}"

packages:
  common_settings: !include common/common-settings.yaml
  keepalive: !include {keepalive}
  common_buttons: !include common/common-buttons.yaml
  common_ota: !include common/common-ota.yaml

esphome:
  name: ${{name}}
  friendly_name: ${{friendly_name}}

{platform_block}

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
{f"  manual_ip:" + chr(10) + f"    static_ip: {static_ip}" + chr(10) + f"    gateway: 10.128.0.1" + chr(10) + f"    subnet: 255.255.0.0" if static_ip else ""}

logger:

api:

ota:
  - platform: esphome

# ----------------------------------------------------------------
# Device-specific configuration goes here
"""


class DeviceCreateHandler(tornado.web.RequestHandler):
    """Create a new ESPHome device YAML config"""
    def post(self, instance_slug):
        instance_config = get_instance_config(instance_slug)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        try:
            data = json.loads(self.request.body)
        except Exception:
            self.set_status(400)
            return self.write({"error": "Invalid JSON body"})

        device_name = (data.get("device_name") or "").strip()
        if not device_name:
            self.set_status(400)
            return self.write({"error": "device_name is required"})

        # Sanitize filename: allow alphanumerics, hyphens, underscores
        import re as _re
        if not _re.match(r'^[a-zA-Z0-9_-]+$', device_name):
            self.set_status(400)
            return self.write({"error": "device_name may only contain letters, numbers, hyphens, and underscores"})

        config_dir = Path(instance_config["config_dir"])
        yaml_file = config_dir / f"{device_name}.yaml"

        if yaml_file.exists():
            self.set_status(409)
            return self.write({"error": f"{device_name}.yaml already exists"})

        try:
            yaml_file.write_text(_device_yaml_template(data), encoding="utf-8")
            # Invalidate device cache for this instance
            _device_cache.pop(instance_slug, None)
            self.write({"success": True, "device_name": device_name, "file": yaml_file.name})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


class DeviceDeleteHandler(tornado.web.RequestHandler):
    """Delete a device: YAML config, storage metadata, and build artifacts"""
    def delete(self, instance_slug, device_name):
        import shutil

        instance_config = get_instance_config(instance_slug)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        config_dir = Path(instance_config["config_dir"])
        yaml_file = config_dir / f"{device_name}.yaml"

        if not yaml_file.exists():
            self.set_status(404)
            return self.write({"error": f"Device {device_name} not found"})

        # Remove YAML config
        yaml_file.unlink()

        # Remove storage metadata (ESPHome writes this after first compile)
        storage_json = config_dir / ".esphome" / "storage" / f"{device_name}.yaml.json"
        if storage_json.exists():
            storage_json.unlink()

        # Remove build directory (name may include friendly-name suffix)
        build_base = config_dir / ".esphome" / "build"
        if build_base.exists():
            for build_dir in build_base.glob(f"{device_name}*"):
                if build_dir.is_dir():
                    shutil.rmtree(build_dir)

        # Invalidate discovery cache
        _device_cache.pop(instance_slug, None)

        self.write({"success": True, "message": f"Device {device_name} deleted"})


class DeviceValidateHandler(tornado.web.RequestHandler):
    """Validate device configuration"""
    def post(self, instance, device_name):
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        try:
            result = subprocess.run(
                ["docker", "exec", instance_config["container"],
                 "esphome", "config", f"/config/{device_name}.yaml"],
                capture_output=True,
                text=True,
                timeout=60
            )

            self.write({
                "success": result.returncode == 0,
                "output": result.stdout + result.stderr
            })
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class DeviceCleanHandler(tornado.web.RequestHandler):
    """Clean device build files"""
    def post(self, instance, device_name):
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        try:
            result = subprocess.run(
                ["docker", "exec", instance_config["container"],
                 "esphome", "clean", f"/config/{device_name}.yaml"],
                capture_output=True,
                text=True,
                timeout=60
            )

            self.write({
                "success": result.returncode == 0,
                "output": result.stdout + result.stderr
            })
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class CleanPlatformIOHandler(tornado.web.RequestHandler):
    """Clean PlatformIO cache to fix toolchain corruption"""
    def post(self, instance):
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        try:
            result = subprocess.run(
                ["docker", "exec", instance_config["container"],
                 "rm", "-rf", "/root/.platformio"],
                capture_output=True,
                text=True,
                timeout=30
            )

            self.write({
                "success": result.returncode == 0,
                "message": "PlatformIO cache cleaned. Next compile will reinstall toolchain."
            })
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class KillStuckProcessesHandler(tornado.web.RequestHandler):
    """Kill stuck ESPHome compile/upload processes"""
    def post(self):
        try:
            # Kill stuck ESPHome processes
            result = subprocess.run(
                ["pkill", "-9", "-f", "esphome.*(compile|run)"],
                capture_output=True,
                text=True,
                timeout=10
            )

            # Check how many are left
            check = subprocess.run(
                ["pgrep", "-fc", "esphome.*(compile|run)"],
                capture_output=True,
                text=True
            )

            remaining = int(check.stdout.strip() or "0")

            self.write({
                "success": True,
                "message": f"Killed stuck processes. {remaining} processes remaining.",
                "remaining": remaining
            })
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class DeviceLogsHandler(tornado.web.RequestHandler):
    """Get device logs (requires device to be online)"""
    def get(self, instance, device_name):
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        try:
            result = subprocess.run(
                ["docker", "exec", instance_config["container"],
                 "esphome", "logs", f"/config/{device_name}.yaml", "--no-color"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                error_output = result.stderr or result.stdout
                if "Can't connect" in error_output or "timed out" in error_output:
                    self.set_status(503)
                    return self.write({"error": "Device is offline or unreachable"})
                self.set_status(500)
                return self.write({
                    "error": "Failed to retrieve logs",
                    "output": error_output
                })

            self.write({
                "success": True,
                "logs": result.stdout or "No logs available"
            })
        except subprocess.TimeoutExpired:
            self.set_status(504)
            self.write({"error": "Device connection timed out (device may be offline)"})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class DeviceFirmwareHandler(tornado.web.RequestHandler):
    """Download compiled firmware binary for UART flashing"""
    def get(self, instance, device_name):
        instance_config = get_instance_config(instance)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        try:
            # Parse YAML to get node_name (ESPHome uses node_name for build directories)
            config_dir = Path(instance_config["config_dir"])
            yaml_file = config_dir / f"{device_name}.yaml"

            if not yaml_file.exists():
                self.set_status(404)
                return self.write({"error": f"Config file {device_name}.yaml not found"})

            with yaml_file.open() as f:
                config = yaml.safe_load(f)

            substitutions = config.get("substitutions", {})
            esphome_config = config.get("esphome", {})
            node_name = esphome_config.get("name") or device_name

            # Resolve substitution if name uses ${var}
            if isinstance(node_name, str) and node_name.startswith("${") and node_name.endswith("}"):
                sub_var = node_name[2:-1]
                node_name = substitutions.get(sub_var, device_name)

            build_dir = config_dir / ".esphome" / "build" / node_name

            if not build_dir.exists():
                self.set_status(404)
                return self.write({
                    "error": "Device has not been compiled yet. Click 'Compile' first, then download firmware."
                })

            # Common firmware locations - prioritize .factory.bin for UART flashing
            firmware_paths = [
                build_dir / ".pioenvs" / node_name / "firmware.factory.bin",
                build_dir / ".pioenvs" / node_name / "firmware-factory.bin",
                build_dir / "firmware.factory.bin",
                build_dir / ".pioenvs" / node_name / "firmware.bin",
                build_dir / "firmware.bin",
            ]

            # Find first existing firmware
            for firmware_path in firmware_paths:
                if firmware_path.exists():
                    # Use original ESPHome filename structure: node_name.factory.bin or node_name.bin
                    if "factory" in firmware_path.name:
                        filename = f"{node_name}.factory.bin"
                    else:
                        filename = f"{node_name}.bin"

                    self.set_header('Content-Type', 'application/octet-stream')
                    self.set_header('Content-Disposition', f'attachment; filename="{filename}"')
                    with firmware_path.open('rb') as f:
                        self.write(f.read())
                    return

            # Build directory exists but no firmware found
            pioenvs_dir = build_dir / ".pioenvs" / node_name
            available_files = []
            if pioenvs_dir.exists():
                available_files = [f.name for f in pioenvs_dir.iterdir() if f.is_file()]

            self.set_status(404)
            self.write({
                "error": "Firmware binary not found in expected locations.",
                "build_dir": str(build_dir),
                "available_files": available_files,
                "hint": "Try compiling the device again."
            })

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class CommonFilesHandler(tornado.web.RequestHandler):
    """List common/shared YAML files for an ESPHome instance"""
    def get(self, instance_slug):
        instance_config = get_instance_config(instance_slug)
        if not instance_config:
            self.set_status(404)
            return self.write({"error": "Instance not found"})

        common_dir = Path(instance_config["config_dir"]) / "common"
        if not common_dir.exists():
            return self.write({"files": []})

        files = []
        for f in sorted(common_dir.iterdir()):
            if f.is_file() and f.suffix == ".yaml" and not f.name.endswith(".bak"):
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()
                })

        self.write({"files": files})


class CommonFileHandler(tornado.web.RequestHandler):
    """Read, save, or delete a specific common YAML file"""

    def _resolve(self, instance_slug: str, filename: str):
        """Return (instance_config, resolved_path) or raise 404/400"""
        instance_config = get_instance_config(instance_slug)
        if not instance_config:
            self.set_status(404)
            self.write({"error": "Instance not found"})
            return None, None

        # Prevent path traversal
        if "/" in filename or "\\" in filename or filename.startswith("."):
            self.set_status(400)
            self.write({"error": "Invalid filename"})
            return None, None

        common_dir = Path(instance_config["config_dir"]) / "common"
        common_dir.mkdir(exist_ok=True)
        return instance_config, common_dir / filename

    def get(self, instance_slug, filename):
        _, path = self._resolve(instance_slug, filename)
        if path is None:
            return

        if not path.exists():
            self.set_status(404)
            return self.write({"error": "File not found"})

        self.write({"content": path.read_text(encoding="utf-8")})

    def post(self, instance_slug, filename):
        _, path = self._resolve(instance_slug, filename)
        if path is None:
            return

        data = json.loads(self.request.body)
        content = data.get("content", "")

        # Backup if file already exists
        if path.exists():
            backup = path.with_suffix(f".yaml.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            path.rename(backup)

        try:
            path.write_text(content, encoding="utf-8")
            self.write({"success": True, "message": f"Saved {filename}"})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

    def delete(self, instance_slug, filename):
        _, path = self._resolve(instance_slug, filename)
        if path is None:
            return

        if not path.exists():
            self.set_status(404)
            return self.write({"error": "File not found"})

        # Move to backup instead of hard-delete
        backup = path.with_suffix(f".yaml.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        path.rename(backup)
        self.write({"success": True, "message": f"Deleted {filename} (backup kept)"})


class ConfigHandler(tornado.web.RequestHandler):
    """Get/update dashboard configuration"""
    def get(self):
        safe_config = {
            "instances": CONFIG.get("instances", []),
            "homeassistant": CONFIG.get("homeassistant", {}),
            "settings": CONFIG.get("settings", {
                "ping_workers": 100,
                "compile_timeout": 300,
                "upload_timeout": 300
            })
        }
        self.write(safe_config)

    def post(self):
        try:
            data = json.loads(self.request.body)

            if "instances" in data:
                CONFIG["instances"] = data["instances"]
            if "homeassistant" in data:
                CONFIG["homeassistant"] = data["homeassistant"]
            if "settings" in data:
                CONFIG["settings"] = data["settings"]

            # Write to file
            with CONFIG_FILE.open('w') as f:
                yaml.dump(CONFIG, f, default_flow_style=False)

            self.write({"success": True, "message": "Configuration updated"})

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class CheckSubstitutionsHandler(tornado.web.RequestHandler):
    """Check all device YAMLs for required and recommended substitutions"""
    def get(self):
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

                        for req_sub in required_substitutions:
                            if req_sub not in substitutions:
                                missing_required.append(req_sub)

                        for rec_sub in recommended_substitutions:
                            if rec_sub not in substitutions:
                                missing_recommended.append(rec_sub)

                        results['total_devices'] += 1

                        if missing_required:
                            instance_issues.append({
                                'device': yaml_file.stem,
                                'missing': missing_required,
                                'has': list(substitutions.keys())
                            })
                        else:
                            results['compliant_devices'] += 1

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

            self.write(results)

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class HAVersionsHandler(tornado.web.RequestHandler):
    """Query Home Assistant for ESPHome device versions"""
    def post(self):
        data = json.loads(self.request.body)
        ha_url = data.get("ha_url", "http://homeassistant.local:8123")
        ha_token = data.get("ha_token", "")

        if not ha_token:
            self.set_status(400)
            return self.write({"error": "Home Assistant token required"})

        try:
            import requests
            headers = {
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json"
            }

            response = requests.get(
                f"{ha_url}/api/states",
                headers=headers,
                timeout=10
            )

            if response.status_code != 200:
                self.set_status(500)
                return self.write({"error": f"HA API error: {response.status_code}"})

            states = response.json()

            # Extract ESPHome device versions
            versions = {}
            for state in states:
                entity_id = state.get("entity_id", "")
                if entity_id.endswith("_esphome_version"):
                    device_name = entity_id.replace("sensor.", "").replace("_esphome_version", "")
                    version = state.get("state")
                    if version and version != "unknown":
                        versions[device_name] = version

            self.write({"versions": versions, "count": len(versions)})

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

# ============================================================================
# BULK OPERATIONS
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
                instance_config = get_instance_config(instance_slug)
                if not instance_config:
                    raise Exception(f"Instance {instance_slug} not found")

                config_dir = Path(instance_config["config_dir"])
                yaml_file = config_dir / f"{device_name}.yaml"

                # Special handling for delete operations - just delete files, don't run esphome
                if operation_type == "delete":
                    import shutil

                    # Delete YAML file
                    if yaml_file.exists():
                        yaml_file.unlink()

                    # Delete storage JSON
                    storage_json = config_dir / ".esphome" / "storage" / f"{device_name}.yaml.json"
                    if storage_json.exists():
                        storage_json.unlink()

                    # Delete build directory (may have friendly name suffix)
                    build_base = config_dir / ".esphome" / "build"
                    if build_base.exists():
                        for build_dir in build_base.glob(f"{device_name}*"):
                            if build_dir.is_dir():
                                shutil.rmtree(build_dir)

                    end_time = datetime.now()
                    duration = int((end_time - start_time).total_seconds())
                    success_count += 1
                    status = "success"
                    error_msg = None

                    # Store result
                    c.execute('''
                        INSERT INTO fleet_manager.operation_results
                        (operation_id, device_name, instance, status, error_message, started_at, completed_at, duration_seconds)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (operation_id, device_name, instance_slug, status, error_msg, start_time, end_time, duration))
                    conn.commit()
                    continue

                # Check for pinned version
                pinned_version = None
                if yaml_file.exists():
                    with yaml_file.open() as f:
                        config = yaml.safe_load(f)
                        substitutions = config.get("substitutions", {})
                        pinned_version = substitutions.get("esphome_version", "*")

                # Build command - use 'run' for upload operations (compile+flash)
                # to match the individual OTA behavior that works
                actual_operation = "run" if operation_type == "upload" else operation_type

                if pinned_version and pinned_version != "*":
                    cmd = ["docker", "run", "--rm"]
                    if operation_type == "upload":
                        cmd.append("--network")
                        cmd.append("host")
                    cmd.extend([
                        "-v", f"{config_dir}:/config",
                        f"esphome/esphome:{pinned_version}",
                        actual_operation, f"/config/{device_name}.yaml"
                    ])
                    if operation_type == "upload":
                        cmd.extend(["--device", "OTA", "--no-logs"])
                else:
                    cmd = ["docker", "exec", instance_config["container"],
                           "esphome", actual_operation, f"/config/{device_name}.yaml"]
                    if operation_type == "upload":
                        cmd.extend(["--device", "OTA", "--no-logs"])

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

                # CRITICAL: Delay between devices to prevent ESPHome state conflicts
                time.sleep(3)

            except Exception as e:
                failure_count += 1
                end_time = datetime.now()
                duration = int((end_time - start_time).total_seconds())

                c.execute('''
                    INSERT INTO fleet_manager.operation_results
                    (operation_id, device_name, instance, status, error_message, started_at, completed_at, duration_seconds)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (operation_id, device_name, instance_slug, "failed", str(e)[:500], start_time, end_time, duration))
                conn.commit()

                # Delay after failure too
                time.sleep(3)

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

class BulkOperationHandler(tornado.web.RequestHandler):
    """Start a bulk operation (compile/upload/validate) on multiple devices"""
    def post(self):
        try:
            data = json.loads(self.request.body)
            operation_type = data.get("operation")
            device_list = data.get("devices", [])

            if not operation_type or not device_list:
                self.set_status(400)
                return self.write({"error": "Missing operation or devices"})

            # Normalize device list
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

            # Start background thread
            thread = threading.Thread(
                target=execute_bulk_operation,
                args=(operation_id, operation_type, normalized_devices),
                daemon=True
            )
            thread.start()

            self.write({
                "success": True,
                "operation_id": operation_id,
                "message": f"Started {operation_type} on {len(normalized_devices)} devices"
            })

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class OperationProgressHandler(tornado.web.RequestHandler):
    """Get real-time progress of a running operation"""
    def get(self, operation_id):
        try:
            conn = get_db_connection()
            c = conn.cursor(cursor_factory=RealDictCursor)

            c.execute('''
                SELECT id, operation_type, device_count, success_count, failure_count,
                       started_at, completed_at, status
                FROM fleet_manager.operations
                WHERE id = %s
            ''', (operation_id,))
            operation = c.fetchone()

            if not operation:
                self.set_status(404)
                return self.write({"error": "Operation not found"})

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

            self.write({
                "operation": operation,
                "completed_count": completed_count,
                "in_progress_count": in_progress_count,
                "results": results
            })

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class OperationsHandler(tornado.web.RequestHandler):
    """Get operation history"""
    def get(self):
        try:
            limit = int(self.get_argument('limit', 50))

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

            for op in operations:
                if op['started_at']:
                    op['started_at'] = op['started_at'].isoformat()
                if op['completed_at']:
                    op['completed_at'] = op['completed_at'].isoformat()

            self.write({"operations": operations})

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class OperationDetailHandler(tornado.web.RequestHandler):
    """Get detailed results for a specific operation"""
    def get(self, operation_id):
        try:
            conn = get_db_connection()
            c = conn.cursor(cursor_factory=RealDictCursor)

            c.execute('''
                SELECT * FROM fleet_manager.operations WHERE id = %s
            ''', (operation_id,))
            operation = c.fetchone()

            if not operation:
                self.set_status(404)
                return self.write({"error": "Operation not found"})

            c.execute('''
                SELECT device_name, instance, status, error_message,
                       started_at, completed_at, duration_seconds
                FROM fleet_manager.operation_results
                WHERE operation_id = %s
                ORDER BY completed_at DESC
            ''', (operation_id,))
            results = c.fetchall()
            conn.close()

            if operation['started_at']:
                operation['started_at'] = operation['started_at'].isoformat()
            if operation['completed_at']:
                operation['completed_at'] = operation['completed_at'].isoformat()

            for result in results:
                if result['started_at']:
                    result['started_at'] = result['started_at'].isoformat()
                if result['completed_at']:
                    result['completed_at'] = result['completed_at'].isoformat()

            self.write({
                "operation": operation,
                "results": results
            })

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

def get_builder_current_versions():
    """Get current ESPHome versions from running containers"""
    try:
        versions = {}
        for instance in CONFIG.get("instances", []):
            container_name = instance.get("container")
            if not container_name:
                continue

            try:
                # Get container image
                cmd = ["docker", "inspect", container_name, "--format={{.Config.Image}}"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

                if result.returncode == 0:
                    image = result.stdout.strip()
                    # Extract version from image tag (e.g., ghcr.io/esphome/esphome:2026.4.3)
                    if ":" in image:
                        version = image.split(":")[-1]
                        versions[instance.get("slug")] = version
            except Exception as e:
                print(f"  Error getting version for {container_name}: {e}")
                continue

        return versions
    except Exception as e:
        print(f"  Error getting builder versions: {e}")
        return {}

class LiveVersionsHandler(tornado.web.RequestHandler):
    """Query all devices for their running firmware version via ESPHome native API"""

    async def get(self):
        # Collect all queryable devices (those with a known IP)
        queryable = []
        for instance in CONFIG.get("instances", []):
            if not instance.get("enabled"):
                continue
            for device in discover_devices_cached(instance):
                if device.get("ip_address"):
                    queryable.append((device["name"], device["ip_address"]))

        if not queryable:
            return self.write({"versions": {}, "queried": 0, "found": 0})

        # Query all devices in parallel; each has an internal 3s timeout
        results = await asyncio.gather(
            *[_query_one_device(name, ip) for name, ip in queryable],
            return_exceptions=True
        )

        versions = {}
        for result in results:
            if isinstance(result, tuple):
                name, version = result
                if version is not None:
                    versions[name] = version

        self.write({"versions": versions, "queried": len(queryable), "found": len(versions)})


class ESPHomeVersionHandler(tornado.web.RequestHandler):
    """Get latest ESPHome version from GitHub and current builder versions"""
    def get(self):
        try:
            latest_version = get_latest_esphome_version()
            current_versions = get_builder_current_versions()
            self.write({
                "latest_version": latest_version,
                "current_versions": current_versions,
                "checked_at": datetime.now().isoformat()
            })
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

class UpdateBuilderHandler(tornado.web.RequestHandler):
    """Update ESPHome builder container to a new version via docker-compose"""
    def post(self):
        try:
            data = json.loads(self.request.body)
            instance_slug = data.get("instance")
            target_version = data.get("version")

            if not instance_slug or not target_version:
                self.set_status(400)
                return self.write({"error": "Missing instance or version"})

            instance = get_instance_config(instance_slug)
            if not instance:
                self.set_status(404)
                return self.write({"error": f"Instance {instance_slug} not found"})

            compose_file = instance.get("compose_file")
            compose_service = instance.get("compose_service")

            if not compose_file or not compose_service:
                self.set_status(400)
                return self.write({"error": "Instance missing compose_file / compose_service in config"})

            compose_path = Path(compose_file)
            if not compose_path.exists():
                self.set_status(500)
                return self.write({"error": f"docker-compose.yml not found: {compose_file}"})

            # Update image version in docker-compose.yml
            compose_text = compose_path.read_text()
            image_prefix = "ghcr.io/esphome/esphome:"
            # Replace the specific service's image line
            updated = re.sub(
                rf"(image:\s*{re.escape(image_prefix)})[^\s]+",
                rf"\g<1>{target_version}",
                compose_text
            )
            if updated == compose_text:
                self.set_status(400)
                return self.write({"error": f"Could not find '{image_prefix}' in {compose_file}"})

            # Backup then write
            backup_path = compose_path.with_suffix(".yml.bak")
            backup_path.write_text(compose_text)
            compose_path.write_text(updated)

            # Pull new image
            print(f"Pulling esphome:{target_version} for {compose_service}...")
            pull_result = subprocess.run(
                ["docker", "compose", "-f", compose_file, "pull", compose_service],
                capture_output=True, text=True, timeout=300
            )
            if pull_result.returncode != 0:
                # Restore backup on failure
                backup_path.rename(compose_path)
                self.set_status(500)
                return self.write({"error": f"docker compose pull failed: {pull_result.stderr}"})

            # Recreate container from updated compose file
            print(f"Restarting {compose_service}...")
            up_result = subprocess.run(
                ["docker", "compose", "-f", compose_file, "up", "-d", "--no-deps", compose_service],
                capture_output=True, text=True, timeout=60
            )
            if up_result.returncode != 0:
                self.set_status(500)
                return self.write({"error": f"docker compose up failed: {up_result.stderr}"})

            backup_path.unlink(missing_ok=True)

            self.write({
                "success": True,
                "instance": instance_slug,
                "version": target_version,
                "message": f"Updated {compose_service} to {target_version}"
            })

        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})

# ============================================================================
# APPLICATION
# ============================================================================

def make_app():
    """Create Tornado application with all handlers"""
    return tornado.web.Application([
        # Main page
        (r"/", MainHandler),

        # WebSocket handlers
        (r"/ws/compile", CompileWebSocketHandler),
        (r"/ws/upload", UploadWebSocketHandler),
        (r"/ws/logs", LogsWebSocketHandler),

        # API endpoints
        (r"/api/instances", InstancesHandler),
        (r"/api/devices", DevicesHandler),
        (r"/api/devices/types", DeviceTypesHandler),
        (r"/api/stats", StatsHandler),
        (r"/api/device/([^/]+)/create", DeviceCreateHandler),
        (r"/api/device/([^/]+)/([^/]+)/delete", DeviceDeleteHandler),
        (r"/api/device/([^/]+)/([^/]+)", DeviceDetailHandler),
        (r"/api/device/([^/]+)/([^/]+)/config", DeviceConfigHandler),
        (r"/api/device/([^/]+)/([^/]+)/validate", DeviceValidateHandler),
        (r"/api/device/([^/]+)/([^/]+)/clean", DeviceCleanHandler),
        (r"/api/device/([^/]+)/([^/]+)/logs", DeviceLogsHandler),
        (r"/api/device/([^/]+)/([^/]+)/firmware", DeviceFirmwareHandler),
        (r"/api/instance/([^/]+)/clean-platformio", CleanPlatformIOHandler),
        (r"/api/system/kill-stuck-processes", KillStuckProcessesHandler),
        (r"/api/common/([^/]+)", CommonFilesHandler),
        (r"/api/common/([^/]+)/(.+)", CommonFileHandler),
        (r"/api/config", ConfigHandler),
        (r"/api/check-substitutions", CheckSubstitutionsHandler),
        (r"/api/ha/versions", HAVersionsHandler),
        (r"/api/devices/live-versions", LiveVersionsHandler),
        (r"/api/esphome/version", ESPHomeVersionHandler),
        (r"/api/esphome/update-builder", UpdateBuilderHandler),
        (r"/api/bulk-operation", BulkOperationHandler),
        (r"/api/operations/([0-9]+)/progress", OperationProgressHandler),
        (r"/api/operations", OperationsHandler),
        (r"/api/operations/([0-9]+)", OperationDetailHandler),
    ],
    template_path=Path(__file__).parent,
    static_path=Path(__file__).parent / "static",
    debug=True)

# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    init_db()

    print("=" * 70)
    print("ESPHome Fleet Manager (Tornado)")
    print("=" * 70)
    print(f"Dashboard: http://{CONFIG['server']['host']}:{CONFIG['server']['port']}")
    print(f"Instances: {len(CONFIG['instances'])}")
    print("=" * 70)

    app = make_app()
    app.listen(CONFIG['server']['port'], CONFIG['server']['host'])
    tornado.ioloop.IOLoop.current().start()
