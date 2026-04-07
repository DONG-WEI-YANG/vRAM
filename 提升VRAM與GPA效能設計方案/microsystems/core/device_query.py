"""
Device Query Utilities — BusType 驅動的外接裝置偵測
====================================================
所有裝置偵測的共用基礎設施：

  1. PowerShell 查詢 + 自動重試 + 結構化錯誤回報
  2. BusType 驅動的外接裝置辨識（取代 DriveType 判斷）
  3. 磁碟代號 ↔ 實體磁碟映射
  4. 完整診斷輸出

核心原則：
  **BusType（硬體匯流排）是外接裝置的唯一可靠判斷依據。**
  DriveType（Removable/Fixed）取決於裝置韌體，不可信。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Set, Any, Tuple

logger = logging.getLogger(__name__)

# Windows: 隱藏子程序視窗
_NO_WINDOW = 0
if platform.system().lower() == "windows":
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # 0x08000000

# 外接裝置的 BusType 清單
EXTERNAL_BUS_TYPES = {"USB", "SD", "Thunderbolt", "USB4"}
# NVMe 裝置可能是外接盒也可能是內建 — 需要進一步判斷
NVME_BUS_TYPE = "NVMe"
# 排除的系統磁碟代號
SYSTEM_DRIVE_LETTERS = set()  # 動態填入


def _get_system_drive() -> str:
    """取得系統碟代號（通常是 C）"""
    if platform.system().lower() == "windows":
        return os.environ.get("SystemDrive", "C:")[0].upper()
    return ""


# ── PowerShell Query with Retry ──────────────────────────────────────────

def ps_query(
    cmd: str,
    timeout: int = 15,
    retries: int = 2,
    label: str = "",
) -> Optional[Any]:
    """
    執行 PowerShell 查詢，自動重試 + JSON 解析。

    回傳解析後的 JSON 物件，失敗回傳 None。
    所有錯誤都記錄到 logger（不靜默吞掉）。
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            kwargs: Dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
            }
            if _NO_WINDOW:
                kwargs["creationflags"] = _NO_WINDOW

            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                **kwargs,
            )

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                last_error = f"returncode={result.returncode}, stderr={stderr[:200]}"
                logger.debug(
                    "[%s] PS query failed (attempt %d/%d): %s",
                    label or "query", attempt, retries, last_error,
                )
                continue

            stdout = result.stdout.strip()
            if not stdout:
                return None  # 空輸出 = 沒有符合的裝置，不是錯誤

            data = json.loads(stdout)
            return data

        except subprocess.TimeoutExpired:
            last_error = f"timeout after {timeout}s"
            logger.warning(
                "[%s] PS query timeout (attempt %d/%d, %ds)",
                label or "query", attempt, retries, timeout,
            )
        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {e}"
            logger.warning(
                "[%s] PS query invalid JSON (attempt %d/%d): %s",
                label or "query", attempt, retries, e,
            )
        except OSError as e:
            last_error = f"OSError: {e}"
            logger.error(
                "[%s] PS query OS error (attempt %d/%d): %s",
                label or "query", attempt, retries, e,
            )
            break  # OSError 通常是系統問題，不重試

    if last_error:
        logger.warning("[%s] PS query failed after %d attempts: %s",
                       label or "query", retries, last_error)
    return None


def _normalize_list(data: Any) -> List[Dict]:
    """PowerShell 在只有一筆結果時回傳 dict 而非 list，統一處理。"""
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return []


# ── Core Queries ─────────────────────────────────────────────────────────

def get_physical_disks() -> List[Dict]:
    """
    取得所有實體磁碟的完整資訊。

    回傳每顆磁碟的：
      DeviceId, FriendlyName, BusType, MediaType, Size,
      SerialNumber, CanPool, IsSystem, SpindleSpeed
    """
    data = ps_query(
        "Get-PhysicalDisk | Select-Object "
        "DeviceId,FriendlyName,BusType,MediaType,Size,"
        "SerialNumber,CanPool,IsSystem,SpindleSpeed "
        "| ConvertTo-Json",
        label="get_physical_disks",
    )
    return _normalize_list(data)


def get_usb_disk_numbers() -> Set[int]:
    """取得所有 BusType=USB 的磁碟編號。"""
    data = ps_query(
        "Get-Disk | Where-Object {$_.BusType -eq 'USB'} "
        "| Select-Object Number | ConvertTo-Json",
        label="get_usb_disks",
    )
    return {d.get("Number") for d in _normalize_list(data) if d.get("Number") is not None}


def get_drive_letter_to_disk_map() -> Dict[str, int]:
    """
    取得磁碟代號 → 實體磁碟編號的映射。
    例如 {"C": 0, "E": 2, "F": 3}
    """
    data = ps_query(
        "Get-Partition | Where-Object {$_.DriveLetter} "
        "| Select-Object DriveLetter,DiskNumber | ConvertTo-Json",
        label="drive_to_disk_map",
    )
    result = {}
    for part in _normalize_list(data):
        letter = part.get("DriveLetter")
        disk_num = part.get("DiskNumber")
        if letter and disk_num is not None:
            result[str(letter).upper()] = int(disk_num)
    return result


def get_disk_bustype_map() -> Dict[int, str]:
    """
    取得磁碟編號 → BusType 映射。
    例如 {0: "NVMe", 2: "USB", 3: "SD"}
    """
    data = ps_query(
        "Get-PhysicalDisk | Select-Object DeviceId,BusType | ConvertTo-Json",
        label="disk_bustype_map",
    )
    result = {}
    for disk in _normalize_list(data):
        dev_id = disk.get("DeviceId")
        bus = disk.get("BusType", "")
        if dev_id is not None:
            try:
                result[int(dev_id)] = str(bus)
            except (ValueError, TypeError):
                pass
    return result


def get_system_disk_number() -> Optional[int]:
    """取得系統碟（C: 所在）的磁碟編號。"""
    sys_drive = _get_system_drive()
    if not sys_drive:
        return None
    drive_map = get_drive_letter_to_disk_map()
    return drive_map.get(sys_drive)


# ── High-Level Detection ─────────────────────────────────────────────────

def get_external_drive_letters(
    exclude_system: bool = True,
) -> List[Dict[str, Any]]:
    """
    取得所有外接裝置的磁碟代號及詳細資訊。

    判斷邏輯（BusType 驅動）：
      1. BusType in (USB, SD, Thunderbolt, USB4) → 一定是外接
      2. BusType == NVMe → 檢查是否為系統碟，非系統碟視為外接
      3. 其他 BusType (SATA, RAID, etc.) → 排除

    回傳 list of dicts：
      [{"letter": "E", "disk_number": 2, "bus_type": "USB",
        "friendly_name": "Samsung T5", "media_type": "SSD",
        "size_bytes": 500107862016, "is_system": False}]
    """
    if platform.system().lower() != "windows":
        return _get_external_drive_letters_linux()

    # Step 1: 取得所有實體磁碟資訊
    disks_raw = get_physical_disks()
    if not disks_raw:
        logger.warning("get_external_drive_letters: no physical disks found")
        return []

    # Step 2: 建立 disk_number → disk_info 映射
    disk_info: Dict[int, Dict] = {}
    for d in disks_raw:
        try:
            dev_id = int(d.get("DeviceId", -1))
        except (ValueError, TypeError):
            continue
        if dev_id < 0:
            continue

        bus = str(d.get("BusType", ""))
        is_external = bus in EXTERNAL_BUS_TYPES or bus == NVME_BUS_TYPE
        if not is_external:
            continue

        disk_info[dev_id] = {
            "bus_type": bus,
            "friendly_name": d.get("FriendlyName", ""),
            "media_type": str(d.get("MediaType", "")),
            "size_bytes": int(d.get("Size", 0)),
            "is_system": bool(d.get("IsSystem", False)),
            "can_pool": bool(d.get("CanPool", False)),
            "spindle_speed": d.get("SpindleSpeed", 0),
        }

    if not disk_info:
        logger.info("get_external_drive_letters: no external bus-type disks")
        return []

    # Step 3: 取得磁碟代號映射
    drive_map = get_drive_letter_to_disk_map()
    if not drive_map:
        logger.warning("get_external_drive_letters: no drive letter mapping")
        return []

    # Step 4: 取得系統碟編號
    sys_drive = _get_system_drive()
    sys_disk_num = drive_map.get(sys_drive)

    # Step 5: 組合結果
    results = []
    for letter, disk_num in sorted(drive_map.items()):
        if disk_num not in disk_info:
            continue

        info = disk_info[disk_num]

        # 排除系統碟
        if exclude_system and disk_num == sys_disk_num:
            continue

        # NVMe 額外判斷：如果是系統碟上的 NVMe → 排除
        if info["bus_type"] == NVME_BUS_TYPE and info["is_system"]:
            continue

        results.append({
            "letter": letter,
            "disk_number": disk_num,
            **info,
        })

    logger.info(
        "get_external_drive_letters: found %d external drive(s): %s",
        len(results),
        ", ".join(f"{r['letter']}: ({r['bus_type']}, {r['friendly_name']})" for r in results),
    )
    return results


def _get_external_drive_letters_linux() -> List[Dict[str, Any]]:
    """Linux: 用 lsblk 偵測外接磁碟。"""
    results = []
    try:
        kwargs: Dict[str, Any] = {"capture_output": True, "text": True, "timeout": 10}
        r = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,MOUNTPOINT,RM,TRAN,MODEL,SIZE,ROTA,TYPE"],
            **kwargs,
        )
        if r.returncode != 0:
            return results

        data = json.loads(r.stdout)
        for dev in data.get("blockdevices", []):
            tran = (dev.get("tran") or "").lower()
            is_removable = bool(dev.get("rm"))
            if tran not in ("usb", "nvme", "mmc") and not is_removable:
                continue

            children = dev.get("children", [])
            if not children:
                children = [dev]

            for part in children:
                mp = part.get("mountpoint")
                if not mp or mp in ("/", "/boot", "/home"):
                    continue
                results.append({
                    "letter": mp,
                    "disk_number": -1,
                    "bus_type": tran.upper() or ("Removable" if is_removable else "Unknown"),
                    "friendly_name": dev.get("model", "") or "",
                    "media_type": "HDD" if dev.get("rota") else "SSD",
                    "size_bytes": 0,
                    "is_system": False,
                })
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        logger.warning("Linux external drive detection failed: %s", e)

    return results


# ── Device Type Classification ───────────────────────────────────────────

def classify_device(
    bus_type: str,
    media_type: str,
    friendly_name: str,
    spindle_speed: Any = 0,
    capacity_gb: float = 0,
) -> str:
    """
    根據 BusType + 輔助資訊分類裝置類型。

    回傳六種類型之一：
      sd_express, nvme_enclosure, usb_ssd, sd_card, hdd, usb_drive
    """
    name_lower = (friendly_name or "").lower()
    bus = (bus_type or "").strip()
    media = (media_type or "").strip()

    # SD 匯流排
    if bus == "SD" or "card reader" in name_lower:
        return "sd_card"

    # NVMe 匯流排 — 區分 SD Express vs 外接盒
    if bus == NVME_BUS_TYPE:
        # SD Express 特徵：名稱含 SD/Express/Card 關鍵字
        sd_keywords = ("sd express", "sdexpress", "sd card", "card reader",
                       "realtek rts5261", "genesys gl9767", "bayhub o2",
                       "rts5261", "gl9767")
        if any(kw in name_lower for kw in sd_keywords):
            return "sd_express"
        # 否則視為 NVMe 外接盒（最常見情境）
        return "nvme_enclosure"

    # Thunderbolt / USB4 → 一定是 NVMe 外接盒
    if bus.lower() in ("thunderbolt", "usb4"):
        return "nvme_enclosure"

    # USB 匯流排
    if bus == "USB":
        # 旋轉磁碟
        is_rotational = (spindle_speed and int(spindle_speed) > 0) or media == "HDD"
        if is_rotational or "hdd" in name_lower:
            return "hdd"
        # SSD 特徵
        if media == "SSD" or "ssd" in name_lower or capacity_gb > 200:
            return "usb_ssd"
        # SD 卡讀卡器
        if "sd" in name_lower or "card" in name_lower or "mmc" in name_lower:
            return "sd_card"
        return "usb_drive"

    # 未知匯流排 — 根據 media/name 猜測
    if "hdd" in name_lower or media == "HDD":
        return "hdd"
    return "usb_drive"


# ── Diagnostic Tool ──────────────────────────────────────────────────────

def diagnose_devices() -> Dict[str, Any]:
    """
    完整的裝置偵測診斷。

    跑一次就能看到：
      1. 所有實體磁碟（含 BusType, MediaType, Size）
      2. 所有分區映射（DriveLetter ↔ DiskNumber）
      3. 所有 Volume 資訊（DriveType 對比）
      4. 外接裝置偵測結果
      5. 每個步驟的錯誤/警告
    """
    report: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": platform.system(),
        "system_drive": _get_system_drive(),
        "steps": {},
        "errors": [],
        "external_drives": [],
    }

    if platform.system().lower() != "windows":
        report["steps"]["note"] = "Linux mode — using lsblk"
        report["external_drives"] = _get_external_drive_letters_linux()
        return report

    # Step 1: All physical disks
    disks = get_physical_disks()
    report["steps"]["physical_disks"] = disks or []
    if not disks:
        report["errors"].append("No physical disks returned by Get-PhysicalDisk")

    # Step 2: Drive letter mapping
    drive_map = get_drive_letter_to_disk_map()
    report["steps"]["drive_letter_map"] = drive_map or {}
    if not drive_map:
        report["errors"].append("No drive letter mapping from Get-Partition")

    # Step 3: Volume info (for comparison)
    vol_data = ps_query(
        "Get-Volume | Where-Object {$_.DriveLetter} "
        "| Select-Object DriveLetter,DriveType,FileSystemLabel,Size,SizeRemaining "
        "| ConvertTo-Json",
        label="diagnose_volumes",
    )
    report["steps"]["volumes"] = _normalize_list(vol_data)

    # Step 4: External drive detection
    ext_drives = get_external_drive_letters()
    report["external_drives"] = ext_drives

    # Step 5: Classification
    for drive in ext_drives:
        drive["classified_type"] = classify_device(
            bus_type=drive["bus_type"],
            media_type=drive["media_type"],
            friendly_name=drive["friendly_name"],
            spindle_speed=drive.get("spindle_speed", 0),
            capacity_gb=drive["size_bytes"] / (1024**3) if drive["size_bytes"] else 0,
        )

    # Step 6: DriveType vs BusType comparison (highlight mismatches)
    mismatches = []
    vol_types = {}
    for v in _normalize_list(vol_data):
        letter = v.get("DriveLetter")
        if letter:
            vol_types[str(letter).upper()] = v.get("DriveType", "")

    for drive in ext_drives:
        letter = drive["letter"]
        vol_dtype = vol_types.get(letter, "N/A")
        if vol_dtype == "Fixed":
            mismatches.append({
                "letter": letter,
                "bus_type": drive["bus_type"],
                "drive_type": vol_dtype,
                "friendly_name": drive["friendly_name"],
                "note": "BusType 表示外接裝置，但 DriveType 回報 Fixed — 舊邏輯會漏掉此裝置",
            })

    report["steps"]["bus_vs_drive_type_mismatches"] = mismatches

    return report


def diagnose_devices_text() -> str:
    """diagnose_devices() 的文字格式版本，適合 CLI 輸出。"""
    diag = diagnose_devices()
    lines = [
        "═══ vRAM Device Detection Diagnostic ═══",
        f"Time: {diag['timestamp']}",
        f"Platform: {diag['platform']}",
        f"System Drive: {diag['system_drive']}:",
        "",
    ]

    # Physical Disks
    lines.append("── Physical Disks ──")
    for d in diag["steps"].get("physical_disks", []):
        size_gb = int(d.get("Size", 0)) / (1024**3)
        lines.append(
            f"  Disk {d.get('DeviceId', '?')}: "
            f"{d.get('FriendlyName', '?')} | "
            f"Bus={d.get('BusType', '?')} | "
            f"Media={d.get('MediaType', '?')} | "
            f"Size={size_gb:.1f}GB | "
            f"System={d.get('IsSystem', '?')} | "
            f"CanPool={d.get('CanPool', '?')}"
        )

    # Drive Mapping
    lines.append("\n── Drive Letter → Disk Mapping ──")
    for letter, num in sorted(diag["steps"].get("drive_letter_map", {}).items()):
        lines.append(f"  {letter}: → Disk {num}")

    # Volumes
    lines.append("\n── Volumes (DriveType comparison) ──")
    for v in diag["steps"].get("volumes", []):
        size_gb = (v.get("Size") or 0) / (1024**3)
        lines.append(
            f"  {v.get('DriveLetter', '?')}: "
            f"DriveType={v.get('DriveType', '?')} | "
            f"Label={v.get('FileSystemLabel', '')} | "
            f"Size={size_gb:.1f}GB"
        )

    # External Drives Found
    lines.append("\n── External Drives Detected (BusType-based) ──")
    if diag["external_drives"]:
        for d in diag["external_drives"]:
            size_gb = d.get("size_bytes", 0) / (1024**3)
            lines.append(
                f"  ✓ {d['letter']}: | "
                f"Bus={d['bus_type']} | "
                f"{d['friendly_name']} | "
                f"Media={d['media_type']} | "
                f"{size_gb:.1f}GB | "
                f"Type={d.get('classified_type', '?')}"
            )
    else:
        lines.append("  (none found)")

    # Mismatches
    mismatches = diag["steps"].get("bus_vs_drive_type_mismatches", [])
    if mismatches:
        lines.append("\n── ⚠ BusType vs DriveType Mismatches ──")
        for m in mismatches:
            lines.append(
                f"  {m['letter']}: Bus={m['bus_type']} but DriveType={m['drive_type']} "
                f"({m['friendly_name']}) — {m['note']}"
            )

    # Errors
    if diag["errors"]:
        lines.append("\n── ❌ Errors ──")
        for e in diag["errors"]:
            lines.append(f"  {e}")

    lines.append("\n═══════════════════════════════════════")
    return "\n".join(lines)
