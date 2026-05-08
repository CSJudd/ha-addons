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

def check_device_online(ip: str) -> str:
    """Check if device is online via ping (3s timeout)"""
    if not ip:
        return "unknown"
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "3", ip],
            capture_output=True,
            timeout=4
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
                "update_available": False
            })

        except Exception as e:
            print(f"Error parsing {yaml_file}: {e}")
            continue

    # Second pass: ping all devices in parallel
    print(f"Checking status for {len(devices)} devices in {instance['name']}...")
    with ThreadPoolExecutor(max_workers=50) as executor:
        future_to_device = {
            executor.submit(check_device_online, device["ip_address"]): device
            for device in devices
        }

        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                device["status"] = future.result()
            except Exception as e:
                print(f"Error checking {device['name']}: {e}")
                device["status"] = "unknown"

    online_count = sum(1 for d in devices if d["status"] == "online")
    print(f"  {online_count}/{len(devices)} devices online")

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
            devices = discover_devices(instance)
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

            devices = discover_devices(instance)
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
            config_dir = Path(instance_config["config_dir"])
            build_dir = config_dir / ".esphome" / "build" / device_name

            if not build_dir.exists():
                self.set_status(404)
                return self.write({
                    "error": "Device has not been compiled yet. Click 'Compile' first, then download firmware."
                })

            # Common firmware locations
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
                    self.set_header('Content-Type', 'application/octet-stream')
                    self.set_header('Content-Disposition', f'attachment; filename="{device_name}_firmware.bin"')
                    with firmware_path.open('rb') as f:
                        self.write(f.read())
                    return

            # Build directory exists but no firmware found
            pioenvs_dir = build_dir / ".pioenvs" / device_name
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

        # API endpoints
        (r"/api/instances", InstancesHandler),
        (r"/api/devices", DevicesHandler),
        (r"/api/devices/types", DeviceTypesHandler),
        (r"/api/stats", StatsHandler),
        (r"/api/device/([^/]+)/([^/]+)", DeviceDetailHandler),
        (r"/api/device/([^/]+)/([^/]+)/config", DeviceConfigHandler),
        (r"/api/device/([^/]+)/([^/]+)/validate", DeviceValidateHandler),
        (r"/api/device/([^/]+)/([^/]+)/clean", DeviceCleanHandler),
        (r"/api/device/([^/]+)/([^/]+)/logs", DeviceLogsHandler),
        (r"/api/device/([^/]+)/([^/]+)/firmware", DeviceFirmwareHandler),
        (r"/api/instance/([^/]+)/clean-platformio", CleanPlatformIOHandler),
        (r"/api/system/kill-stuck-processes", KillStuckProcessesHandler),
        (r"/api/config", ConfigHandler),
        (r"/api/check-substitutions", CheckSubstitutionsHandler),
        (r"/api/ha/versions", HAVersionsHandler),
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
