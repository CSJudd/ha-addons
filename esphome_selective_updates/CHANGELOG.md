# Changelog

All notable changes to this project will be documented in this file.

---

## [2.0.0] - 2025-10-30

### Major Rewrite - Production Ready Release

This version represents a complete rewrite focused on safety, usability, and transparency about the Protection Mode requirement.

### Added
- **Safety Features:**
  - Comprehensive startup safety checks
  - Clear Protection Mode detection and user messaging
  - Operation boundary documentation
  - Docker socket verification
  - ESPHome container validation
  
- **Control Features:**
  - `dry_run` mode - Preview updates without executing
  - `max_devices_per_run` - Limit batch size
  - `start_from_device` - Manual resume point
  - `update_only_these` - Whitelist specific devices
  
- **Logging Improvements:**
  - Structured logging with clear sections
  - Better progress indicators
  - Safety check results
  - Operation summaries
  - More descriptive error messages

### Changed
- **Renamed add-on** from "ESPHome Smart Updater" to "ESPHome Selective Updates"
- **Complete README rewrite:**
  - Honest explanation of Protection Mode requirement
  - Clear target audience definition
  - Safety assurances with technical details
  - Better documentation structure
  - Real-world use case examples
  
- **Improved error handling:**
  - Graceful failures with actionable messages
  - Better Docker socket detection
  - Clear instructions when things go wrong
  
- **Better startup script:**
  - Step-by-step validation
  - Clear error messages with solutions
  - Pre-flight summary

### Fixed
- Improved signal handling for SIGTERM/SIGINT
- Better progress file management
- More robust version detection
- Cleaner housekeeping logic

### Documentation
- Added comprehensive troubleshooting guide
- Added advanced use case examples
- Added clear installation instructions
- Added feature request information for ESPHome
- Added safety explanation and audit guidelines

### Breaking Changes
- Configuration schema updated (new options added)
- Slug changed from `esphome_smart_updater` to `esphome_selective_updates`
- Requires reinstallation if upgrading from 1.x

---

## [1.2.42] - 2025-10-30

### Fixed
- Updated firmware path for ESPHome ≥ 2025.9
- Prevented false "Could not copy binary" errors
- Added fallback for legacy ESPHome path structure

### Added
- Housekeeping options for log and progress management

---

## [1.2.39] - 2025-10-30

### Fixed
- Dashboard metadata update after successful OTA
- ESPHome UI now clears "Update needed" badge correctly

---

## [1.2.38] - 2025-10-30

### Fixed
- Removed unsupported `--no-logs` flag from ESPHome upload command
- Improved OTA failure diagnostics

---

## [1.2.34] - 2025-10-30

### Fixed
- Documented Protection Mode requirement clearly
- Added explicit startup hint when socket missing
- Improved user guidance

---

## [1.2.33] - 2025-10-30

### Fixed
- Enforced Protection Mode OFF requirement
- Added clear error messages about Protection Mode
- Removed unnecessary `full_access` flag

---

## [1.2.32] - 2025-10-30

### Fixed
- Added `full_access: true` for diagnostic builds
- Confirmed socket visibility issue resolution

---

## [1.2.31] - 2025-10-30

### Fixed
- **Critical:** Restored Supervisor Docker socket mount
- Added default `BUILD_FROM` value in Dockerfile
- Fixed base image compliance with Supervisor security model
- Ensured `hassio_role: "manager"` and `docker_api: true` honored

---

## [1.2.1] - 2025-10-30

### Fixed
- Ensured Docker socket mount via `docker_api: true`
- Disabled automatic restart during failures for simpler testing

---

## [1.2.0] - 2025-10-30

### Changed
- **Major:** Switched to Docker-exec-only compilation
- Removed built-in/venv compiler (Alpine musl incompatibility)
- Now requires Supervisor Docker socket

### Fixed
- Eliminated "xtensa-lx106-elf-g++: not found" errors
- Robust stop handling with process group termination

---

## [1.1.1] - 2025-10-26

### Fixed
- Corrected ESPHome CLI invocation order
- Implemented full graceful stop handling
- Progress saved immediately on SIGTERM/SIGINT

---

## [1.1.0] - 2025-10-26

### Fixed
- Replaced unavailable `python3-venv` with `py3-virtualenv`
- Fixed venv creation for built-in compiler

---

## [1.0.4] - 2025-10-26

### Changed
- New compile strategy with automatic fallback
- Added `compile_mode` configuration option
- Made `docker_api` optional

---

## [1.0.3] - 2025-10-26

### Fixed
- Robust Docker socket detection
- Dynamic `DOCKER_HOST` setting
- Alpine-only Python dependencies

---

## [1.0.2] - 2025-10-26

### Fixed
- Resolved PEP 668 build failure on Alpine 3.19
- Replaced pip with APK package management

---

## [1.0.1] - 2025-10-26

### Added
- Docker client installation
- Supervisor socket binding
- Configurable ESPHome container name

---

## [1.0.0] - 2025-10-25

### Added
- Initial release
- Smart update logic (only update when needed)
- Offline device detection
- Resume capability
- Comprehensive logging
- Multi-architecture support
- HTTP OTA updates
- Progress tracking

---

## Version Numbering

- **Major (X.0.0):** Breaking changes, architecture changes
- **Minor (1.X.0):** New features, significant improvements
- **Patch (1.0.X):** Bug fixes, minor improvements

---

## Upgrade Guide

### From 1.x to 2.0

1. **Backup your configuration** (copy options to a text file)
2. **Uninstall the old version**
3. **Reinstall from repository** (new slug)
4. **Restore configuration**
5. **Ensure Protection Mode is OFF**
6. **Test with `dry_run: true` first**

The add-on slug has changed, so this requires a fresh installation.

---

## Future Plans

### Planned Features (2.1.x)
- [ ] Parallel compilation support
- [ ] Better progress UI integration
- [ ] Email/notification support
- [ ] Compile queue management
- [ ] Per-device update scheduling

### Waiting on ESPHome
- [ ] Native HTTP compile API (requested from ESPHome team)
- [ ] Protection Mode compatibility (requires API changes)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

**Note:** Versions prior to 2.0.0 are considered beta. Version 2.0.0 is the first production-ready release.