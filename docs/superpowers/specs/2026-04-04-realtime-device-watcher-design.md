# Real-Time Device Watcher Design

**Date:** 2026-04-04
**Status:** Approved
**Scope:** Windows real-time hotplug detection for VRAM expansion resources

---

## Problem

The current system is entirely polling-based for device detection:
- `HealthMonitor`: 5-second polling via `os.path.exists()`
- `hotplug_launcher` GUI: 10-second polling via `os.path.exists()`
- System startup (`sd/enc/usb_vram_system`): one-time scan only

No WMI event subscription, no `WM_DEVICECHANGE`, no `RegisterDeviceNotification`.
A 5-10 second delay on device removal risks BSOD when GPU reads invalidated mmap pages.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Module placement | New `device_watcher.py` | Single responsibility, clean separation from existing modules |
| WMI implementation | PowerShell subprocess (primary) + 3s polling (fallback) | Zero extra dependencies, auto-degradation if PS fails |
| New device policy | Smart: auto-expand high-speed, prompt for medium, ignore slow | Balance between convenience and safety |

## Architecture

```
                    +------------------------+
                    |    DeviceWatcher        |  <-- new: core/device_watcher.py
                    |    (singleton, daemon)  |
                    +--------+-------+-------+
                    | Primary: PS    | Fallback:
                    | WMI Event Sub  | 3s Polling
                    +--------+-------+-------+
                             | DeviceChangeInfo callbacks
              +--------------+------------------+
              v              v                  v
     HealthMonitor    hotplug_launcher    *_vram_system
     (disconnect/     (GUI status +       (dynamic expand/
      reconnect)       notifications)      shrink pool)
```

## Module: `core/device_watcher.py`

### Public API

```python
class DeviceEvent(Enum):
    ARRIVED = "arrived"
    REMOVED = "removed"

@dataclass
class DeviceChangeInfo:
    event: DeviceEvent
    drive_letter: str                  # e.g. "E"
    device_info: Optional[DeviceInfo]  # full BusType/bandwidth info (ARRIVED only)
    timestamp: float

class DeviceWatcher:
    def __init__(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def on_change(self, callback: Callable[[DeviceChangeInfo], None]) -> None: ...

    @property
    def is_event_driven(self) -> bool:
        """True = PS WMI active; False = degraded to polling"""
```

### Primary: PowerShell WMI Event Subprocess

Launches a long-running PowerShell process executing:

```powershell
Register-WmiEvent -Class Win32_VolumeChangeEvent -SourceIdentifier VolChange
while ($true) {
    $evt = Wait-Event -SourceIdentifier VolChange -Timeout 30
    if ($evt) {
        Remove-Event -SourceIdentifier VolChange
        Write-Output '{"event":"volume_change","ts":"<timestamp>"}'
    }
    Write-Output '{"heartbeat":true}'
}
```

Python reader thread:
1. `readline()` blocks until PS outputs a line
2. On `volume_change`: call `get_external_drive_letters()` for current snapshot
3. Diff against previous snapshot -> generate ARRIVED/REMOVED events
4. For ARRIVED: call `classify_device()` to populate `DeviceChangeInfo.device_info`
5. Fire callbacks in registration order

### Fallback: 3-Second Polling

Activates automatically when PowerShell subprocess:
- Fails to start (`FileNotFoundError`)
- Dies unexpectedly (`proc.poll() is not None`)
- Misses heartbeat for 60 seconds

Fallback behavior:
- Every 3 seconds: `get_external_drive_letters()` -> diff -> callbacks
- Every 60 seconds: attempt to restart PS subprocess to restore event-driven mode
- Log: `logger.warning("DeviceWatcher degraded to polling mode")`

### Diff Logic (shared by both modes)

```python
prev_letters = {"E", "F"}
curr_letters = {"E", "G"}
arrived = curr_letters - prev_letters  # {"G"} -> ARRIVED
removed = prev_letters - curr_letters  # {"F"} -> REMOVED
```

## Integration: HealthMonitor

**Change**: Add `attach_watcher(watcher: DeviceWatcher)` method.

When watcher is attached:
- REMOVED events -> immediately trigger `_on_disconnect(device_id)` (< 1s, not 5s)
- ARRIVED events -> immediately trigger `_on_reconnect(device_id)`
- Connection status check removed from 5-second polling loop
- Temperature/wear/error polling remains at 5-second interval (no WMI events for these)

When watcher is NOT attached (backwards compatibility):
- Existing polling behavior unchanged

## Integration: hotplug_launcher

**Change**: Subscribe to DeviceWatcher in `_start_gui()`.

- REMOVED + matches `self._my_drive` -> immediate `_quit()` (< 1s, not 10s)
- ARRIVED -> trigger smart policy (see below)
- Retain 10-second polling for GUI status bar updates (memory bars, GPU info) only

## Integration: *_vram_system (sd/enc/usb)

**Change**: Accept optional `DeviceWatcher` in constructor or `activate()`.

- ARRIVED -> trigger smart expansion policy
- REMOVED -> trigger `_handle_disconnect` immediately

## Smart Expansion Policy (ARRIVED)

Based on `classify_device()` result:

| Condition | Action |
|---|---|
| Bandwidth >= 500 MB/s (NVMe enclosure, SD Express) | Auto-expand: create swap file, register blocks, log + GUI "Auto-joined X:\" |
| 50 MB/s <= Bandwidth < 500 MB/s (USB 3.0 SSD) | Notify user: GUI prompt "Found USB SSD (X:\, ~200 MB/s). Add to expansion?" |
| Bandwidth < 50 MB/s or unrecognized | Silent ignore: `logger.debug("Ignoring slow/non-storage device")` |

Bandwidth source: `classify_device()` protocol estimation or `benchmark_read_mbs` if available.

Auto-expand flow:
1. DeviceWatcher ARRIVED callback
2. `classify_device()` -> high-speed confirmed
3. `real_boost._scan_external_drives()` refresh
4. Create mmap swap file on new device
5. Register new blocks in MemoryPool
6. Prefetcher automatically utilizes new blocks

## Removal Notification Policy (REMOVED)

| Condition | Action |
|---|---|
| Device has active swap blocks (in use) | GUI warning: "X:\ removed! Emergency migration to RAM..." -> on complete: "Safely degraded to RAM mode" |
| Device registered but idle (no active blocks) | GUI info: "X:\ removed. Expansion capacity reduced by Y GB" |
| Device not part of expansion | Silent ignore |

Removal flow:
1. DeviceWatcher REMOVED callback
2. Check if device has active blocks in MemoryPool
3. If active: HealthMonitor `_handle_disconnect` -> pin blocks -> fallback to RAM -> notify GUI
4. If idle: remove from swap pool -> update capacity -> notify GUI
5. If unrelated: ignore

## Linux Compatibility

DeviceWatcher is Windows-focused (WMI). On Linux:
- Primary: `pyudev` or `/sys/block` inotify (if available)
- Fallback: 3-second polling with `lsblk -J`
- Same callback interface, same diff logic

Initial implementation: Windows only. Linux fallback = polling only.

## File Changes Summary

| File | Change |
|---|---|
| `core/device_watcher.py` | **NEW** — DeviceWatcher class, PS WMI subprocess, polling fallback |
| `core/health_monitor.py` | Add `attach_watcher()`, remove connection polling when watcher active |
| `hotplug_launcher.py` | Subscribe to watcher, add arrival notification + smart policy UI |
| `systems/sd_vram_system.py` | Accept watcher, wire ARRIVED/REMOVED to expand/disconnect |
| `systems/enc_vram_system.py` | Accept watcher, wire ARRIVED/REMOVED to expand/disconnect |
| `systems/usb_vram_system.py` | Accept watcher, wire ARRIVED/REMOVED to expand/disconnect |
| `core/real_boost.py` | Add `expand_to_device(letter)` / `remove_device(letter)` for dynamic pool changes |

## Testing Strategy

- Unit: mock PowerShell subprocess, verify diff logic produces correct ARRIVED/REMOVED events
- Unit: verify fallback activates when subprocess dies
- Unit: verify smart policy thresholds (>= 500, >= 50, < 50)
- Integration: verify HealthMonitor receives instant disconnect via watcher
- Integration: verify hotplug_launcher quits immediately on REMOVED
