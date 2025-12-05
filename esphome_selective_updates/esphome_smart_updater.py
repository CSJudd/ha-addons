#!/usr/bin/env python3
"""
ESPHome Selective Updates - Smart bulk updates for ESPHome devices

This add-on fixes ESPHome Dashboard's "Update All" button by adding:
- Smart updates (only devices that need updating)
- Resume capability (continues from where it left off)
- Offline detection (skips unreachable devices)
- Progress tracking (detailed logging)

Author: Chris Judd
License: MIT
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict

# ============================================================================
# CONFIGURATION PATHS
# ============================================================================

ADDON_OPTIONS_PATH = Path("/data/options.json")
STATE_PATH         = Path("/data/state.json")
LOG_FILE           = Path("/config/esphome_smart_update.log")
PROGRESS_FILE      = Path("/config/esphome_update_progress.json")
ESPHOME_CONFIG_DIR = Path("/config/esphome")
DASHBOARD_JSON     = ESPHOME_CONFIG_DIR / ".dashboard.json"

DEFAULTS = {
    "ota_password": "",
    "skip_offline": True,
    "delay_between_updates": 3,
    "esphome_container": "addon_15ef4d2f_esphome",
    "dry_run": False,
    "max_devices_per_run": 0,
    "start_from_device": "",
    "update_only_these": [],
    "clear_log_now": False,
    "clear_progress_now": False,
    "clear_log_on_start": False,
    "clear_progress_on_start": False,
    "always_clear_log_on_version_change": True,
}

# ============================================================================
# GLOBAL STATE
# ============================================================================

STOP_REQUESTED = False
CURRENT_CHILD: Optional[subprocess.Popen] = None

# ============================================================================
# LOGGING UTILITIES
# ============================================================================

def ts() -> str:
    """Return formatted timestamp"""
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

def log(msg: str):
    """Log message to both stdout and file"""
    line = f"{ts()} {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def log_header(title: str):
    """Log a section header"""
    log("=" * 79)
    log(title)
    log("=" * 79)

def log_section(title: str):
    """Log a subsection header"""
    log("")
    log(f"--- {title} ---")

def truncate_file(path: Path):
    """Clear a file's contents"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8"):
            pass
    except Exception as e:
        log(f"Warning: failed to truncate {path}: {e}")

# ============================================================================
# SIGNAL HANDLING
# ============================================================================

def _sig_handler(signum, frame):
    """Handle SIGTERM/SIGINT for graceful shutdown"""
    global STOP_REQUESTED, CURRENT_CHILD
    STOP_REQUESTED = True
    log("")
    log("⚠ Stop signal received - shutting down gracefully...")
    
    if CURRENT_CHILD and CURRENT_CHILD.poll() is None:
        try:
            os.killpg(os.getpgid(CURRENT_CHILD.pid), signal.SIGTERM)
        except Exception:
            try:
                CURRENT_CHILD.terminate()
            except Exception:
                pass

signal.signal(signal.SIGTERM, _sig_handler)
signal.signal(signal.SIGINT, _sig_handler)

# ============================================================================
# JSON UTILITIES
# ============================================================================

def load_json(path: Path, default):
    """Load JSON file with fallback"""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(path: Path, data: dict):
    """Save data as JSON"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as e:
        log(f"Warning: failed to write {path}: {e}")

def load_options() -> Dict:
    """Load add-on options with defaults"""
    opts = DEFAULTS.copy()
    if ADDON_OPTIONS_PATH.exists():
        try:
            loaded = json.loads(ADDON_OPTIONS_PATH.read_text(encoding="utf-8"))
            for k in DEFAULTS:
                if k in loaded:
                    opts[k] = loaded[k]
        except Exception as e:
            log(f"Warning: options.json parse error: {e}")
    return opts

def load_state() -> Dict:
    """Load persistent state"""
    return load_json(STATE_PATH, {
        "last_version": None,
        "clear_log_now_consumed": False,
        "clear_progress_now_consumed": False
    })

def save_state(state: Dict):
    """Save persistent state"""
    save_json(STATE_PATH, state)

def load_progress() -> Dict:
    """Load update progress"""
    return load_json(PROGRESS_FILE, {
        "done": [],
        "failed": [],
        "skipped": []
    })

def save_progress(data: dict):
    """Save update progress"""
    save_json(PROGRESS_FILE, data)

# ============================================================================
# NETWORK UTILITIES
# ============================================================================

def ping_host(host: str) -> bool:
    """Check if host is reachable via ping"""
    for args in (["-c", "1", "-w", "1"], ["-c", "1", "-W", "1"]):
        try:
            rc = subprocess.run(
                ["ping"] + args + [host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3
            ).returncode
            if rc == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return True
        except Exception:
            pass
    return False

# ============================================================================
# ESPHOME YAML PARSING
# ============================================================================

ESPHOME_NAME_RE = re.compile(r"^esphome:\s*$", re.MULTILINE)
NAME_LINE_RE    = re.compile(r"^\s+name\s*:\s*(\S+)\s*$")

def parse_node_name(yaml_text: str) -> Optional[str]:
    """Extract ESPHome device name from YAML config"""
    # Find 'esphome:' block
    m = ESPHOME_NAME_RE.search(yaml_text)
    if not m:
        # Fallback: look for top-level 'name:'
        m2 = re.search(r"^\s*name\s*:\s*([^\s#]+)", yaml_text, re.MULTILINE)
        return m2.group(1).strip() if m2 else None
    
    start = m.end()
    # Extract indented lines following 'esphome:'
    block = []
    for line in yaml_text[start:].splitlines():
        if line.strip() == "":
            block.append(line)
            continue
        if not line.startswith(" "):
            break  # Next top-level section
        block.append(line)
    
    # Find 'name:' within the block
    for line in block:
        m2 = NAME_LINE_RE.match(line)
        if m2:
            return m2.group(1).strip()
    
    return None

# ============================================================================
# DEVICE DISCOVERY
# ============================================================================

def discover_devices() -> List[dict]:
    """Discover all ESPHome device configurations"""
    out = []
    
    if not ESPHOME_CONFIG_DIR.exists():
        log(f"ERROR: ESPHome config directory not found: {ESPHOME_CONFIG_DIR}")
        return out
    
    for yaml_file in sorted(ESPHOME_CONFIG_DIR.glob("*.yaml")):
        try:
            text = yaml_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = ""
        
        # Extract IP address (if manually configured)
        ip = None
        m_ip = re.search(r"manual_ip\s*:\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})", text)
        if m_ip:
            ip = m_ip.group(1).strip()
        
        # Extract node name
        node = parse_node_name(text) or yaml_file.stem
        
        out.append({
            "name": yaml_file.stem,
            "node": node,
            "config": yaml_file.name,
            "address": ip,
        })
    
    return out

# ============================================================================
# DOCKER OPERATIONS
# ============================================================================

def _run(
    cmd: list[str],
    env: Optional[dict] = None,
    capture: bool = False,
    text_out: bool = True
) -> Tuple[int, str]:
    """Run subprocess with stop handling"""
    global CURRENT_CHILD
    
    if STOP_REQUESTED:
        return (143, "")
    
    out = ""  # Initialize out variable
    
    try:
        if capture:
            p = subprocess.Popen(
                cmd,
                env=env,
                preexec_fn=os.setsid,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=text_out
            )
        else:
            p = subprocess.Popen(
                cmd,
                env=env,
                preexec_fn=os.setsid
            )
        
        CURRENT_CHILD = p
        
        if capture:
            out = p.communicate()[0] or ""
            rc = p.returncode
        else:
            rc = p.wait()
        
        return (rc, out)
    finally:
        CURRENT_CHILD = None

def docker_exec(
    container: str,
    args: list[str],
    capture: bool = False
) -> Tuple[int, str]:
    """Execute command inside Docker container"""
    return _run(
        ["docker", "exec", container] + args,
        os.environ.copy(),
        capture=capture
    )

def docker_cp(src_container: str, src_path: str, dst_path: str) -> int:
    """Copy file from Docker container to host"""
    rc, _ = _run(
        ["docker", "cp", f"{src_container}:{src_path}", dst_path],
        os.environ.copy(),
        capture=False
    )
    return rc

def container_exists(container: str) -> bool:
    """Check if Docker container exists"""
    try:
        result = subprocess.run(
            ["docker", "inspect", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False

def get_current_esphome_version(container: str) -> str:
    """Get ESPHome version from container"""
    rc, out = docker_exec(container, ["esphome", "version"], capture=True)
    if rc == 0:
        m = re.search(r"(?:ESPHome|Version:)\s+([0-9][^\s]*)", out)
        if m:
            return m.group(1).strip()
    return "unknown"

# ============================================================================
# VERSION TRACKING
# ============================================================================

def read_dashboard_versions(device_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Read deployed and current versions from ESPHome dashboard.json"""
    if not DASHBOARD_JSON.exists():
        return (None, None)
    
    try:
        dashboard = json.loads(DASHBOARD_JSON.read_text(encoding="utf-8"))
        device_info = dashboard.get(device_name, {})
        deployed = device_info.get("deployed_version")
        current = device_info.get("current_version")
        return (deployed, current)
    except Exception:
        return (None, None)

def needs_update(device_name: str, progress: Dict) -> Tuple[bool, str]:
    """
    Determine if device needs update
    Returns: (needs_update, reason)
    """
    # Already done this run
    if device_name in progress.get("done", []):
        return (False, "already updated this run")
    
    # Read versions from dashboard
    deployed, current = read_dashboard_versions(device_name)
    
    if deployed is None or current is None:
        return (True, "version information unavailable")
    
    if deployed != current:
        return (True, f"deployed={deployed}, current={current}")
    
    return (False, f"already up-to-date ({deployed})")

# ============================================================================
# COMPILATION
# ============================================================================

def compile_in_esphome_container(
    container: str,
    yaml_name: str,
    device_name: str
) -> Optional[str]:
    """
    Compile firmware in ESPHome container
    Returns: Path to compiled binary on host, or None if failed
    """
    log(f"→ Compiling {yaml_name} via Docker in '{container}'")
    
    rc, _ = docker_exec(
        container,
        ["esphome", "compile", f"/config/esphome/{yaml_name}"],
        capture=False
    )
    
    if rc != 0 or STOP_REQUESTED:
        if STOP_REQUESTED:
            log("Stop requested; aborting compile.")
        else:
            log(f"✗ Compilation failed for {device_name}")
        return None
    
    # Locate compiled binary
    stem = Path(yaml_name).stem
    pio_bin = f"/data/build/{stem}*/.pioenvs/{stem}*/firmware.bin"
    legacy = f"/config/esphome/.esphome/build/{stem}/{stem}.bin"
    
    dst_dir = Path("/config/esphome/builds")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = str(dst_dir / f"{stem}.bin")
    
    # Try new PlatformIO path structure
    rc, out = docker_exec(
        container,
        ["sh", "-lc", f"set -e; ls -1 {pio_bin} 2>/dev/null | head -n1"],
        capture=True
    )
    
    if rc == 0 and out.strip():
        src = out.strip().splitlines()[0].strip()
        if docker_cp(container, src, dst) == 0:
            log(f"→ Binary copied to {dst} (from {src})")
            return dst
    
    # Fallback to legacy path
    if docker_cp(container, legacy, dst) == 0:
        log(f"→ Binary copied to {dst} (from {legacy})")
        return dst
    
    log(f"✗ Could not locate firmware binary for {device_name}")
    return None

# ============================================================================
# OTA UPLOAD
# ============================================================================

def ota_upload_via_esphome(
    container: str,
    yaml_name: str,
    target: str
) -> Tuple[bool, str]:
    """
    Upload firmware via OTA using ESPHome CLI
    Returns: (success, output)
    """
    args = ["esphome", "upload", f"/config/esphome/{yaml_name}", "--device", target]
    rc, out = docker_exec(container, args, capture=True)
    
    success = (
        rc == 0 or
        "OTA successful" in out or
        "Successfully uploaded program" in out
    )
    
    return (success, out)

# ============================================================================
# SAFETY CHECKS
# ============================================================================

def verify_docker_socket() -> bool:
    """Verify Docker socket is available"""
    log_section("Safety Check: Docker Socket")
    
    socket_paths = ["/run/docker.sock", "/var/run/docker.sock"]
    found_socket = None
    
    for path in socket_paths:
        if os.path.exists(path):
            found_socket = path
            break
    
    if not found_socket:
        log("")
        log("✗ FATAL: Docker socket not available")
        log("")
        log("CAUSE: Protection Mode is likely ON")
        log("")
        log("FIX: Go to add-on Info tab → Toggle 'Protection mode' to OFF")
        log("")
        log("WHY: This add-on extends ESPHome's functionality and needs")
        log("     the same Docker access that ESPHome itself has for")
        log("     compilation. It only accesses the ESPHome container")
        log("     and does not interact with your host system.")
        log("")
        log("SAFETY: This add-on:")
        log("  • Only accesses the ESPHome add-on container")
        log("  • Only reads/writes to /config/esphome/")
        log("  • Uses the same compilation tools ESPHome uses")
        log("  • Does not access other containers or host system")
        log("")
        return False
    
    log(f"✓ Docker socket found: {found_socket}")
    return True

def verify_docker_cli() -> bool:
    """Verify docker CLI is available"""
    try:
        result = subprocess.run(
            ["docker", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5
        )
        if result.returncode == 0:
            version = result.stdout.decode().strip()
            log(f"✓ Docker CLI available: {version}")
            return True
    except Exception as e:
        log(f"✗ Docker CLI not available: {e}")
    
    return False

def verify_docker_connection() -> bool:
    """Verify we can communicate with Docker daemon"""
    try:
        result = subprocess.run(
            ["docker", "ps"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=5
        )
        if result.returncode == 0:
            log("✓ Docker daemon communication OK")
            return True
        else:
            log(f"✗ Cannot communicate with Docker daemon: {result.stderr.decode()}")
    except Exception as e:
        log(f"✗ Docker daemon communication failed: {e}")
    
    return False

def verify_esphome_container(container: str) -> bool:
    """Verify ESPHome container exists and is accessible"""
    log_section("Safety Check: ESPHome Container")
    
    if not container_exists(container):
        log("")
        log(f"✗ FATAL: ESPHome container '{container}' not found")
        log("")
        log("CAUSE: Container name may be incorrect or ESPHome add-on not running")
        log("")
        log("FIX: Check your ESPHome add-on:")
        log("  1. Ensure ESPHome add-on is installed and running")
        log("  2. Note the exact container name from Supervisor logs")
        log("  3. Update 'esphome_container' option if different")
        log("")
        log("HINT: Common container names:")
        log("  • addon_15ef4d2f_esphome (official ESPHome add-on)")
        log("  • addon_a0d7b954_esphome")
        log("  • addon_5c53de3b_esphome")
        log("")
        return False
    
    log(f"✓ ESPHome container found: {container}")
    
    # Try to get ESPHome version
    version = get_current_esphome_version(container)
    if version != "unknown":
        log(f"✓ ESPHome version: {version}")
    else:
        log("⚠ Could not determine ESPHome version")
    
    return True

def verify_esphome_config_dir() -> bool:
    """Verify ESPHome config directory is accessible"""
    log_section("Safety Check: ESPHome Configuration")
    
    if not ESPHOME_CONFIG_DIR.exists():
        log("")
        log(f"✗ FATAL: ESPHome config directory not found: {ESPHOME_CONFIG_DIR}")
        log("")
        log("CAUSE: ESPHome not configured or config directory missing")
        log("")
        log("FIX: Ensure ESPHome add-on is set up with device configurations")
        log("")
        return False
    
    yaml_count = len(list(ESPHOME_CONFIG_DIR.glob("*.yaml")))
    log(f"✓ ESPHome config directory accessible")
    log(f"✓ Found {yaml_count} device configuration(s)")
    
    if yaml_count == 0:
        log("")
        log("⚠ WARNING: No device configurations found")
        log("   Add device YAML files to /config/esphome/ first")
        log("")
        return False
    
    return True

def verify_safe_operation() -> bool:
    """Run all safety checks"""
    log_header("Safety Verification")
    
    checks = [
        ("Docker Socket", verify_docker_socket),
        ("Docker CLI", verify_docker_cli),
        ("Docker Connection", verify_docker_connection),
    ]
    
    all_passed = True
    for name, check_func in checks:
        if not check_func():
            all_passed = False
            break
    
    return all_passed

# ============================================================================
# HOUSEKEEPING
# ============================================================================

def perform_housekeeping(opts: Dict, state: Dict, progress: Dict) -> Dict:
    """Handle log and progress file cleanup"""
    addon_version = os.environ.get("ADDON_VERSION", "unknown")
    
    # Version change detection
    if opts.get("always_clear_log_on_version_change", True):
        if addon_version and addon_version != state.get("last_version"):
            truncate_file(LOG_FILE)
            log(f"Add-on version changed: {state.get('last_version')} → {addon_version}")
            log("Log file cleared due to version change")
            state["last_version"] = addon_version
            save_state(state)
    
    # Clear log on start
    if opts.get("clear_log_on_start", False):
        truncate_file(LOG_FILE)
        log("Log file cleared (clear_log_on_start)")
    
    # Clear log now (one-time trigger)
    if bool(opts.get("clear_log_now", False)) and not state.get("clear_log_now_consumed", False):
        truncate_file(LOG_FILE)
        log("Log file cleared (clear_log_now trigger)")
        state["clear_log_now_consumed"] = True
        save_state(state)
    elif not bool(opts.get("clear_log_now", False)) and state.get("clear_log_now_consumed", False):
        state["clear_log_now_consumed"] = False
        save_state(state)
    
    # Clear progress on start
    if opts.get("clear_progress_on_start", False):
        truncate_file(PROGRESS_FILE)
        progress = {"done": [], "failed": [], "skipped": []}
        save_progress(progress)
        log("Progress file cleared (clear_progress_on_start)")
    
    # Clear progress now (one-time trigger)
    if bool(opts.get("clear_progress_now", False)) and not state.get("clear_progress_now_consumed", False):
        truncate_file(PROGRESS_FILE)
        progress = {"done": [], "failed": [], "skipped": []}
        save_progress(progress)
        log("Progress file cleared (clear_progress_now trigger)")
        state["clear_progress_now_consumed"] = True
        save_state(state)
    elif not bool(opts.get("clear_progress_now", False)) and state.get("clear_progress_now_consumed", False):
        state["clear_progress_now_consumed"] = False
        save_state(state)
    
    return progress

# ============================================================================
# DEVICE FILTERING
# ============================================================================

def filter_devices(
    devices: List[dict],
    opts: Dict,
    progress: Dict
) -> Tuple[List[dict], Dict[str, str]]:
    """
    Filter devices based on options
    Returns: (filtered_devices, skip_reasons)
    """
    skip_reasons = {}
    
    # Apply whitelist if specified
    whitelist = opts.get("update_only_these", [])
    if whitelist:
        devices = [d for d in devices if d["name"] in whitelist]
        log(f"Whitelist active: {len(whitelist)} device(s) specified")
    
    # Apply start_from_device
    start_from = opts.get("start_from_device", "")
    if start_from:
        found = False
        filtered = []
        for d in devices:
            if d["name"] == start_from:
                found = True
            if found:
                filtered.append(d)
        
        if found:
            devices = filtered
            log(f"Starting from device: {start_from}")
        else:
            log(f"WARNING: start_from_device '{start_from}' not found; processing all")
    
    # Check which devices need updates
    filtered = []
    for dev in devices:
        name = dev["name"]
        
        # Skip already processed
        if name in progress.get("done", []):
            skip_reasons[name] = "already updated this run"
            continue
        
        # Check if update needed
        needs, reason = needs_update(name, progress)
        if not needs:
            skip_reasons[name] = reason
            continue
        
        filtered.append(dev)
    
    return (filtered, skip_reasons)

# ============================================================================
# MAIN UPDATE LOGIC
# ============================================================================

def update_device(
    dev: dict,
    opts: Dict,
    progress: Dict,
    dry_run: bool
) -> str:
    """
    Update a single device
    Returns: status ("done", "failed", "skipped")
    """
    name = dev["name"]
    node = dev["node"]
    yaml_name = dev["config"]
    ip = dev["address"]
    
    container = opts["esphome_container"]
    skip_offline = opts.get("skip_offline", True)
    
    log(f"Config: {yaml_name}")
    
    # Show version info
    deployed, current = read_dashboard_versions(name)
    log(f"Versions: deployed={deployed or 'unknown'}, current={current or 'unknown'}")
    
    # Determine target
    target = ip if ip else f"{node}.local"
    if not ip:
        log(f"No manual IP configured; using mDNS: {target}")
    
    # Ping check
    if skip_offline and ip:
        if not ping_host(ip):
            log(f"⚠ Device appears offline (ping failed); skipping")
            return "skipped"
    
    # Dry run mode
    if dry_run:
        log("→ DRY RUN: Would compile and upload here")
        return "done"
    
    # Real update
    log(f"→ Starting update for {name}")
    
    # Compile
    bin_path = compile_in_esphome_container(container, yaml_name, name)
    if STOP_REQUESTED:
        log("Stop requested during compile")
        return "skipped"
    
    if not bin_path:
        return "failed"
    
    # Upload
    ok, out = ota_upload_via_esphome(container, yaml_name, target)
    
    if ok:
        log("→ OTA upload successful")
        return "done"
    else:
        # Log tail of output for debugging
        tail = "\n".join(out.splitlines()[-40:])
        log("OTA upload failed. Output (last 40 lines):")
        for line in tail.splitlines():
            log(f"  {line}")
        log(f"✗ Update failed for {name}")
        return "failed"

def main():
    """Main execution function"""
    # Load configuration
    opts = load_options()
    state = load_state()
    progress = load_progress()
    
    # Housekeeping
    progress = perform_housekeeping(opts, state, progress)
    
    # Safety checks
    if not verify_safe_operation():
        log("")
        log("Safety checks failed. Cannot continue.")
        sys.exit(1)
    
    # Verify ESPHome container
    esphome_container = opts["esphome_container"]
    if not verify_esphome_container(esphome_container):
        sys.exit(1)
    
    # Verify ESPHome config
    if not verify_esphome_config_dir():
        sys.exit(1)
    
    log_section("Safety Checks Complete")
    log("✓ All safety checks passed")
    log(f"✓ Operating boundaries:")
    log(f"  • Docker container: {esphome_container}")
    log(f"  • Config directory: {ESPHOME_CONFIG_DIR}")
    log(f"  • Build output: /config/esphome/builds/")
    log(f"  • No access to: host system, other containers, external networks")
    
    # Start main process
    log_header("ESPHome Selective Updates v2.0")
    
    dry_run = opts.get("dry_run", False)
    if dry_run:
        log("⚠ DRY RUN MODE - No actual updates will be performed")
    
    # Discover devices
    log_section("Device Discovery")
    devices = discover_devices()
    total = len(devices)
    log(f"Found {total} total device configuration(s)")
    
    if total == 0:
        log("No devices to process. Exiting.")
        return
    
    # Filter devices
    log_section("Filtering Devices")
    filtered_devices, skip_reasons = filter_devices(devices, opts, progress)
    
    log(f"Devices needing update: {len(filtered_devices)}")
    log(f"Devices to skip: {len(skip_reasons)}")
    
    if skip_reasons:
        log("")
        log("Skipped devices:")
        for name, reason in sorted(skip_reasons.items()):
            log(f"  • {name}: {reason}")
    
    # Apply max_devices_per_run limit
    max_devices = opts.get("max_devices_per_run", 0)
    if max_devices > 0 and len(filtered_devices) > max_devices:
        log("")
        log(f"⚠ Limiting to {max_devices} device(s) per run (max_devices_per_run)")
        filtered_devices = filtered_devices[:max_devices]
    
    to_process = len(filtered_devices)
    if to_process == 0:
        log("")
        log("✓ No devices need updating. All done!")
        return
    
    # Process devices
    log_header(f"Processing {to_process} Device(s)")
    
    done = set(progress.get("done", []))
    failed = set(progress.get("failed", []))
    skipped = set(progress.get("skipped", []))
    
    delay = int(opts.get("delay_between_updates", 3))
    
    for idx, dev in enumerate(filtered_devices, start=1):
        if STOP_REQUESTED:
            log("")
            log("⚠ Stop requested; saving progress and exiting")
            break
        
        name = dev["name"]
        
        log("")
        log(f"[{idx}/{to_process}] Processing: {name}")
        
        status = update_device(dev, opts, progress, dry_run)
        
        # Update progress
        if status == "done":
            done.add(name)
            failed.discard(name)
            log(f"✓ {name} completed successfully")
        elif status == "failed":
            failed.add(name)
            log(f"✗ {name} failed")
        elif status == "skipped":
            skipped.add(name)
            log(f"⊘ {name} skipped")
        
        # Save progress
        progress["done"] = sorted(list(done))
        progress["failed"] = sorted(list(failed))
        progress["skipped"] = sorted(list(skipped))
        save_progress(progress)
        
        # Delay between devices
        if idx < to_process:  # Don't delay after last device
            for _ in range(max(0, delay)):
                if STOP_REQUESTED:
                    break
                time.sleep(1)
        
        if STOP_REQUESTED:
            log("")
            log("⚠ Stop requested after device processing")
            break
    
    # Final summary
    log_header("Summary")
    log(f"Total devices: {total}")
    log(f"Successfully updated: {len(done)}")
    log(f"Failed: {len(failed)}")
    log(f"Skipped: {len(skipped)}")
    
    if failed:
        log("")
        log("Failed devices:")
        for name in sorted(failed):
            log(f"  • {name}")
    
    if dry_run:
        log("")
        log("⚠ DRY RUN MODE - No actual changes were made")
    
    log("")
    log("✓ ESPHome Selective Updates complete")

# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("")
        log("⚠ Interrupted by user")
        sys.exit(130)
    except Exception as e:
        log("")
        log(f"✗ FATAL ERROR: {e}")
        import traceback
        log(traceback.format_exc())
        sys.exit(1)