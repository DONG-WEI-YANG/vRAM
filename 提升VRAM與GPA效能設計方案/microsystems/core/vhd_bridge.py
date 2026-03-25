"""
VHD Bridge -- Low-level ctypes wrapper for Windows virtdisk.dll
================================================================
建立、掛載、管理 VHD/VHDX 虛擬磁碟。

用途：
  在可卸除式裝置 (SD 卡/USB) 上建立 VHD 檔案，掛載後讓 Windows
  視為「固定磁碟」(Fixed disk)，從而允許在上面建立 pagefile。

  Windows 原生不允許在 Removable 類型磁碟上建立 pagefile，
  但透過 VHD 掛載後磁碟類型變成 Fixed，就能繞過此限制。

支援格式：
  - VHDX (Win8+ / Server 2012+) — 預設
  - VHD  (Win7 / Server 2008 R2)

典型流程::

    bridge = VhdBridge()
    bridge.create(r"E:\\vram_boost.vhdx", 4 * 1024**3)  # 4 GB
    bridge.attach(permanent=True)
    disk_num = bridge.get_disk_number()
    # ... 用 diskpart 初始化磁碟、建立分割區 ...
    # ... 在新磁碟區上建立 pagefile ...
    bridge.cleanup()  # 卸載 + 關閉
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import re
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# ── GUID Structure ────────────────────────────────────────────────────────


class GUID(ctypes.Structure):
    """Windows GUID / UUID 結構"""

    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __repr__(self) -> str:
        d4 = self.Data4
        return (
            f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-"
            f"{d4[0]:02X}{d4[1]:02X}-"
            f"{d4[2]:02X}{d4[3]:02X}{d4[4]:02X}{d4[5]:02X}{d4[6]:02X}{d4[7]:02X}}}"
        )


GUID_NULL = GUID(0, 0, 0, (ctypes.c_ubyte * 8)(0, 0, 0, 0, 0, 0, 0, 0))

# Microsoft VHD Vendor GUID: {EC984AEC-A0F9-47e9-901F-71415A66345B}
VIRTUAL_STORAGE_TYPE_VENDOR_MICROSOFT = GUID(
    0xEC984AEC,
    0xA0F9,
    0x47E9,
    (ctypes.c_ubyte * 8)(0x90, 0x1F, 0x71, 0x41, 0x5A, 0x66, 0x34, 0x5B),
)

# ── Constants ─────────────────────────────────────────────────────────────

# VIRTUAL_STORAGE_TYPE DeviceId
VIRTUAL_STORAGE_TYPE_DEVICE_UNKNOWN = 0
VIRTUAL_STORAGE_TYPE_DEVICE_VHD = 2
VIRTUAL_STORAGE_TYPE_DEVICE_VHDX = 3

# CREATE_VIRTUAL_DISK_VERSION
CREATE_VIRTUAL_DISK_VERSION_2 = 2

# CREATE_VIRTUAL_DISK_FLAG
CREATE_VIRTUAL_DISK_FLAG_NONE = 0x00000000
CREATE_VIRTUAL_DISK_FLAG_FULL_PHYSICAL_ALLOCATION = 0x00000001

# VIRTUAL_DISK_ACCESS_MASK
VIRTUAL_DISK_ACCESS_NONE = 0x00000000
VIRTUAL_DISK_ACCESS_ALL = 0x003F0000
VIRTUAL_DISK_ACCESS_CREATE = 0x00100000

# ATTACH_VIRTUAL_DISK_FLAG
ATTACH_VIRTUAL_DISK_FLAG_NONE = 0x00000000
ATTACH_VIRTUAL_DISK_FLAG_NO_DRIVE_LETTER = 0x00000002
ATTACH_VIRTUAL_DISK_FLAG_PERMANENT_LIFETIME = 0x00000004

# ATTACH_VIRTUAL_DISK_VERSION
ATTACH_VIRTUAL_DISK_VERSION_1 = 1

# OPEN_VIRTUAL_DISK_FLAG
OPEN_VIRTUAL_DISK_FLAG_NONE = 0x00000000

# OPEN_VIRTUAL_DISK_VERSION
OPEN_VIRTUAL_DISK_VERSION_2 = 2

# DETACH_VIRTUAL_DISK_FLAG
DETACH_VIRTUAL_DISK_FLAG_NONE = 0x00000000

# Win32 constants
INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
ERROR_SUCCESS = 0

# ── ctypes Structures ─────────────────────────────────────────────────────


class VIRTUAL_STORAGE_TYPE(ctypes.Structure):
    """
    typedef struct _VIRTUAL_STORAGE_TYPE {
        ULONG DeviceId;
        GUID  VendorId;
    } VIRTUAL_STORAGE_TYPE;
    """

    _fields_ = [
        ("DeviceId", ctypes.c_ulong),
        ("VendorId", GUID),
    ]


class CREATE_VIRTUAL_DISK_PARAMETERS_V2(ctypes.Structure):
    """
    CREATE_VIRTUAL_DISK_PARAMETERS Version2 內部結構。

    對應 C 結構體的 Version2 union member。
    """

    _fields_ = [
        ("UniqueId", GUID),
        ("MaximumSize", ctypes.c_ulonglong),
        ("BlockSizeInBytes", ctypes.c_ulong),
        ("SectorSizeInBytes", ctypes.c_ulong),
        ("PhysicalSectorSizeInBytes", ctypes.c_ulong),
        ("ParentPath", ctypes.c_wchar_p),
        ("SourcePath", ctypes.c_wchar_p),
        ("OpenFlags", ctypes.c_ulong),
        ("ParentVirtualStorageType", VIRTUAL_STORAGE_TYPE),
        ("SourceVirtualStorageType", VIRTUAL_STORAGE_TYPE),
        ("ResiliencyGuid", GUID),
    ]


class CREATE_VIRTUAL_DISK_PARAMETERS(ctypes.Structure):
    """
    typedef struct _CREATE_VIRTUAL_DISK_PARAMETERS {
        CREATE_VIRTUAL_DISK_VERSION Version;
        union { ... Version2; };
    } CREATE_VIRTUAL_DISK_PARAMETERS;
    """

    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Version2", CREATE_VIRTUAL_DISK_PARAMETERS_V2),
    ]


class OPEN_VIRTUAL_DISK_PARAMETERS_V2(ctypes.Structure):
    """OPEN_VIRTUAL_DISK_PARAMETERS Version2 內部結構。"""

    _fields_ = [
        ("GetInfoOnly", ctypes.c_int),  # BOOL
        ("ReadOnly", ctypes.c_int),     # BOOL
        ("ResiliencyGuid", GUID),
    ]


class OPEN_VIRTUAL_DISK_PARAMETERS(ctypes.Structure):
    """
    typedef struct _OPEN_VIRTUAL_DISK_PARAMETERS {
        OPEN_VIRTUAL_DISK_VERSION Version;
        union { ... Version2; };
    } OPEN_VIRTUAL_DISK_PARAMETERS;
    """

    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Version2", OPEN_VIRTUAL_DISK_PARAMETERS_V2),
    ]


class ATTACH_VIRTUAL_DISK_PARAMETERS(ctypes.Structure):
    """
    typedef struct _ATTACH_VIRTUAL_DISK_PARAMETERS {
        ATTACH_VIRTUAL_DISK_VERSION Version;  // = 1
        // Version1 is empty
    } ATTACH_VIRTUAL_DISK_PARAMETERS;

    Version1 沒有任何欄位，所以只需要 Version。
    """

    _fields_ = [
        ("Version", ctypes.c_ulong),
    ]


# ── Helpers ───────────────────────────────────────────────────────────────


def _win32_error_message(error_code: int) -> str:
    """把 Win32 error code 轉成人類可讀的錯誤訊息。"""
    try:
        return ctypes.FormatError(error_code)
    except Exception:
        return f"Win32 error {error_code} (0x{error_code:08X})"


def _is_vhdx_supported() -> bool:
    """
    檢查是否支援 VHDX 格式 (Windows 8+ / Server 2012+)。

    VHDX 在 NT 6.2+ 才支援：
      - Win7   = 6.1 → VHD only
      - Win8   = 6.2 → VHDX
      - Win10  = 10.0 → VHDX
      - Win11  = 10.0 → VHDX
    """
    ver = sys.getwindowsversion()
    return (ver.major > 6) or (ver.major == 6 and ver.minor >= 2)


# ── VhdBridge Class ───────────────────────────────────────────────────────


class VhdBridge:
    """
    VHD/VHDX 虛擬磁碟管理器。

    透過 ctypes 直接呼叫 Windows virtdisk.dll API，
    建立、開啟、掛載、卸載虛擬磁碟。

    主要用途：把 SD 卡/USB 上的 VHD 掛載成 Fixed disk，
    讓 Windows 允許在上面建立 pagefile。

    Usage::

        bridge = VhdBridge()
        if bridge.create(r"E:\\boost.vhdx", 4 * 1024**3):
            if bridge.attach(permanent=True):
                print(f"Disk: {bridge.get_physical_path()}")
                print(f"Disk#: {bridge.get_disk_number()}")
        # ... 使用完畢 ...
        bridge.cleanup()
    """

    def __init__(self) -> None:
        try:
            self._vdisk = ctypes.WinDLL("virtdisk", use_last_error=True)
        except OSError as exc:
            logger.error("無法載入 virtdisk.dll: %s", exc)
            raise RuntimeError(
                "virtdisk.dll 不可用。請確認 Windows 版本 >= Windows 7 SP1。"
            ) from exc

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle: Optional[ctypes.wintypes.HANDLE] = None
        self._path: Optional[str] = None
        self._attached: bool = False

        self._setup_prototypes()

    # ── Function Prototypes ───────────────────────────────────────────

    def _setup_prototypes(self) -> None:
        """
        設定 virtdisk.dll 函式的參數和回傳型別。

        這讓 ctypes 能正確做型別檢查和參數轉換。
        """
        HANDLE = ctypes.wintypes.HANDLE
        DWORD = ctypes.wintypes.DWORD
        ULONG = ctypes.c_ulong
        BOOL = ctypes.wintypes.BOOL
        LPVOID = ctypes.c_void_p

        # CreateVirtualDisk
        self._vdisk.CreateVirtualDisk.restype = DWORD
        self._vdisk.CreateVirtualDisk.argtypes = [
            ctypes.POINTER(VIRTUAL_STORAGE_TYPE),          # VirtualStorageType
            ctypes.c_wchar_p,                              # Path
            ULONG,                                         # VirtualDiskAccessMask
            ctypes.c_void_p,                               # SecurityDescriptor
            ULONG,                                         # Flags
            ULONG,                                         # ProviderSpecificFlags
            ctypes.POINTER(CREATE_VIRTUAL_DISK_PARAMETERS),  # Parameters
            ctypes.c_void_p,                               # Overlapped
            ctypes.POINTER(HANDLE),                        # Handle (out)
        ]

        # OpenVirtualDisk
        self._vdisk.OpenVirtualDisk.restype = DWORD
        self._vdisk.OpenVirtualDisk.argtypes = [
            ctypes.POINTER(VIRTUAL_STORAGE_TYPE),          # VirtualStorageType
            ctypes.c_wchar_p,                              # Path
            ULONG,                                         # VirtualDiskAccessMask
            ULONG,                                         # Flags
            ctypes.POINTER(OPEN_VIRTUAL_DISK_PARAMETERS),  # Parameters
            ctypes.POINTER(HANDLE),                        # Handle (out)
        ]

        # AttachVirtualDisk
        self._vdisk.AttachVirtualDisk.restype = DWORD
        self._vdisk.AttachVirtualDisk.argtypes = [
            HANDLE,                                         # VirtualDiskHandle
            ctypes.c_void_p,                               # SecurityDescriptor
            ULONG,                                         # Flags
            ULONG,                                         # ProviderSpecificFlags
            ctypes.POINTER(ATTACH_VIRTUAL_DISK_PARAMETERS),  # Parameters
            ctypes.c_void_p,                               # Overlapped
        ]

        # DetachVirtualDisk
        self._vdisk.DetachVirtualDisk.restype = DWORD
        self._vdisk.DetachVirtualDisk.argtypes = [
            HANDLE,  # VirtualDiskHandle
            ULONG,   # Flags
            ULONG,   # ProviderSpecificFlags
        ]

        # GetVirtualDiskPhysicalPath
        self._vdisk.GetVirtualDiskPhysicalPath.restype = DWORD
        self._vdisk.GetVirtualDiskPhysicalPath.argtypes = [
            HANDLE,                           # VirtualDiskHandle
            ctypes.POINTER(ULONG),            # DiskPathSizeInBytes (in/out)
            ctypes.c_wchar_p,                 # DiskPath (out)
        ]

        # kernel32.CloseHandle
        self._kernel32.CloseHandle.restype = BOOL
        self._kernel32.CloseHandle.argtypes = [HANDLE]

    # ── Public API ────────────────────────────────────────────────────

    def create(self, path: str, size_bytes: int, dynamic: bool = True) -> bool:
        """
        建立 VHD/VHDX 虛擬磁碟檔案。

        自動偵測 Windows 版本選擇格式：
          - Win8+  → VHDX
          - Win7   → VHD

        Args:
            path:       VHD/VHDX 檔案路徑（建議放在 SD 卡/USB 上）
            size_bytes: 虛擬磁碟最大容量（bytes）
            dynamic:    True = 動態擴展（省空間）；
                        False = 完全預先配置（效能較好）

        Returns:
            True 表示建立成功。
        """
        if self._handle is not None:
            logger.warning("已有開啟的 VHD handle，請先 close()")
            return False

        # 選擇格式
        use_vhdx = _is_vhdx_supported()
        device_id = (
            VIRTUAL_STORAGE_TYPE_DEVICE_VHDX if use_vhdx
            else VIRTUAL_STORAGE_TYPE_DEVICE_VHD
        )
        fmt_name = "VHDX" if use_vhdx else "VHD"

        # 確認副檔名正確
        expected_ext = ".vhdx" if use_vhdx else ".vhd"
        if not path.lower().endswith(expected_ext):
            base, _ = os.path.splitext(path)
            path = base + expected_ext
            logger.info("自動修正副檔名: %s", path)

        logger.info(
            "建立 %s: path=%s, size=%d bytes (%.1f GB), dynamic=%s",
            fmt_name, path, size_bytes, size_bytes / (1024 ** 3), dynamic,
        )

        # 準備 VIRTUAL_STORAGE_TYPE
        storage_type = VIRTUAL_STORAGE_TYPE()
        storage_type.DeviceId = device_id
        storage_type.VendorId = VIRTUAL_STORAGE_TYPE_VENDOR_MICROSOFT

        # 準備 CREATE_VIRTUAL_DISK_PARAMETERS (Version 2)
        params = CREATE_VIRTUAL_DISK_PARAMETERS()
        params.Version = CREATE_VIRTUAL_DISK_VERSION_2
        params.Version2.UniqueId = GUID_NULL  # 自動產生
        params.Version2.MaximumSize = size_bytes
        params.Version2.BlockSizeInBytes = 0  # 使用預設值
        params.Version2.SectorSizeInBytes = 512
        params.Version2.PhysicalSectorSizeInBytes = 4096
        params.Version2.ParentPath = None
        params.Version2.SourcePath = None
        params.Version2.OpenFlags = OPEN_VIRTUAL_DISK_FLAG_NONE
        params.Version2.ParentVirtualStorageType = VIRTUAL_STORAGE_TYPE()
        params.Version2.SourceVirtualStorageType = VIRTUAL_STORAGE_TYPE()
        params.Version2.ResiliencyGuid = GUID_NULL

        # 建立旗標
        create_flags = (
            CREATE_VIRTUAL_DISK_FLAG_NONE if dynamic
            else CREATE_VIRTUAL_DISK_FLAG_FULL_PHYSICAL_ALLOCATION
        )

        # Output handle
        handle = ctypes.wintypes.HANDLE()

        # 確保父目錄存在
        parent_dir = os.path.dirname(path)
        if parent_dir and not os.path.isdir(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError as exc:
                logger.error("無法建立目錄 %s: %s", parent_dir, exc)
                return False

        # 呼叫 CreateVirtualDisk
        # 注意：Version 2 parameters 必須用 AccessMask=0，
        # 否則 Win10+ 回傳 ERROR_INVALID_PARAMETER (87)
        ret = self._vdisk.CreateVirtualDisk(
            ctypes.byref(storage_type),     # VirtualStorageType
            path,                           # Path
            0,                              # VirtualDiskAccessMask (must be 0 for V2)
            None,                           # SecurityDescriptor
            create_flags,                   # Flags
            0,                              # ProviderSpecificFlags
            ctypes.byref(params),           # Parameters
            None,                           # Overlapped (synchronous)
            ctypes.byref(handle),           # Handle (out)
        )

        if ret != ERROR_SUCCESS:
            logger.error(
                "CreateVirtualDisk 失敗: error=%d (0x%08X) — %s",
                ret, ret, _win32_error_message(ret),
            )
            return False

        self._handle = handle
        self._path = path
        logger.info("成功建立 %s: %s", fmt_name, path)
        return True

    def open(self, path: str) -> bool:
        """
        開啟已存在的 VHD/VHDX 檔案。

        Args:
            path: VHD/VHDX 檔案路徑。

        Returns:
            True 表示開啟成功。
        """
        if self._handle is not None:
            logger.warning("已有開啟的 VHD handle，請先 close()")
            return False

        if not os.path.isfile(path):
            logger.error("檔案不存在: %s", path)
            return False

        # 根據副檔名決定格式
        lower_path = path.lower()
        if lower_path.endswith(".vhdx"):
            device_id = VIRTUAL_STORAGE_TYPE_DEVICE_VHDX
        elif lower_path.endswith(".vhd"):
            device_id = VIRTUAL_STORAGE_TYPE_DEVICE_VHD
        else:
            logger.error("不支援的副檔名: %s (需要 .vhd 或 .vhdx)", path)
            return False

        logger.info("開啟 VHD: %s", path)

        # 準備 VIRTUAL_STORAGE_TYPE
        storage_type = VIRTUAL_STORAGE_TYPE()
        storage_type.DeviceId = device_id
        storage_type.VendorId = VIRTUAL_STORAGE_TYPE_VENDOR_MICROSOFT

        # 準備 OPEN_VIRTUAL_DISK_PARAMETERS (Version 2)
        params = OPEN_VIRTUAL_DISK_PARAMETERS()
        params.Version = OPEN_VIRTUAL_DISK_VERSION_2
        params.Version2.GetInfoOnly = 0    # FALSE
        params.Version2.ReadOnly = 0       # FALSE
        params.Version2.ResiliencyGuid = GUID_NULL

        # Output handle
        handle = ctypes.wintypes.HANDLE()

        ret = self._vdisk.OpenVirtualDisk(
            ctypes.byref(storage_type),     # VirtualStorageType
            path,                           # Path
            0,                              # VirtualDiskAccessMask (0 for V2)
            OPEN_VIRTUAL_DISK_FLAG_NONE,    # Flags
            ctypes.byref(params),           # Parameters
            ctypes.byref(handle),           # Handle (out)
        )

        if ret != ERROR_SUCCESS:
            logger.error(
                "OpenVirtualDisk 失敗: error=%d (0x%08X) — %s",
                ret, ret, _win32_error_message(ret),
            )
            return False

        self._handle = handle
        self._path = path
        logger.info("成功開啟 VHD: %s", path)
        return True

    def attach(self, permanent: bool = True) -> bool:
        """
        掛載 (Attach) 虛擬磁碟。

        掛載後虛擬磁碟會出現在系統中，就像一個實體磁碟。
        磁碟類型為 Fixed，允許 Windows 在上面建立 pagefile。

        Args:
            permanent: True = 即使本程序結束，磁碟仍保持掛載狀態。
                       這對 pagefile 用途是必要的。

        Returns:
            True 表示掛載成功。
        """
        if self._handle is None:
            logger.error("尚未開啟 VHD，請先呼叫 create() 或 open()")
            return False

        if self._attached:
            logger.warning("虛擬磁碟已經掛載")
            return True

        # 掛載旗標
        flags = ATTACH_VIRTUAL_DISK_FLAG_NONE
        if permanent:
            flags |= ATTACH_VIRTUAL_DISK_FLAG_PERMANENT_LIFETIME

        # 準備 ATTACH_VIRTUAL_DISK_PARAMETERS
        params = ATTACH_VIRTUAL_DISK_PARAMETERS()
        params.Version = ATTACH_VIRTUAL_DISK_VERSION_1

        logger.info("掛載虛擬磁碟: permanent=%s", permanent)

        ret = self._vdisk.AttachVirtualDisk(
            self._handle.value if hasattr(self._handle, "value") else self._handle,
            None,                           # SecurityDescriptor
            flags,                          # Flags
            0,                              # ProviderSpecificFlags
            ctypes.byref(params),           # Parameters
            None,                           # Overlapped (synchronous)
        )

        if ret != ERROR_SUCCESS:
            logger.error(
                "AttachVirtualDisk 失敗: error=%d (0x%08X) — %s",
                ret, ret, _win32_error_message(ret),
            )
            return False

        self._attached = True
        logger.info("虛擬磁碟已掛載")
        return True

    def get_physical_path(self) -> Optional[str]:
        """
        取得掛載後的實體磁碟路徑。

        例如: ``\\\\.\\PhysicalDrive4``

        Returns:
            磁碟路徑字串，失敗時回傳 None。
        """
        if self._handle is None:
            logger.error("尚未開啟 VHD")
            return None

        if not self._attached:
            logger.error("虛擬磁碟尚未掛載，請先呼叫 attach()")
            return None

        # 配置足夠大的 buffer
        buf_size = ctypes.c_ulong(1024)  # bytes
        buf = ctypes.create_unicode_buffer(buf_size.value // 2)

        handle_val = (
            self._handle.value if hasattr(self._handle, "value") else self._handle
        )

        ret = self._vdisk.GetVirtualDiskPhysicalPath(
            handle_val,
            ctypes.byref(buf_size),
            buf,
        )

        if ret != ERROR_SUCCESS:
            logger.error(
                "GetVirtualDiskPhysicalPath 失敗: error=%d (0x%08X) — %s",
                ret, ret, _win32_error_message(ret),
            )
            return None

        physical_path = buf.value
        logger.info("實體磁碟路徑: %s", physical_path)
        return physical_path

    def get_disk_number(self) -> Optional[int]:
        """
        從實體磁碟路徑中解析磁碟編號。

        例如: ``\\\\.\\PhysicalDrive4`` → ``4``

        Returns:
            磁碟編號 (int)，解析失敗時回傳 None。
        """
        phys_path = self.get_physical_path()
        if phys_path is None:
            return None

        # 從路徑中提取數字，例如 \\.\PhysicalDrive4 → 4
        match = re.search(r"PhysicalDrive(\d+)", phys_path, re.IGNORECASE)
        if match:
            disk_num = int(match.group(1))
            logger.info("磁碟編號: %d", disk_num)
            return disk_num

        logger.warning("無法從路徑解析磁碟編號: %s", phys_path)
        return None

    def detach(self) -> bool:
        """
        卸載 (Detach) 虛擬磁碟。

        卸載後磁碟從系統中消失，上面的所有磁碟區也會一起消失。
        如果磁碟上有 pagefile，Windows 會在下次重開機時停止使用它。

        Returns:
            True 表示卸載成功。
        """
        if self._handle is None:
            logger.warning("尚未開啟 VHD，不需要卸載")
            return True

        if not self._attached:
            logger.info("虛擬磁碟未掛載，不需要卸載")
            return True

        handle_val = (
            self._handle.value if hasattr(self._handle, "value") else self._handle
        )

        logger.info("卸載虛擬磁碟...")

        ret = self._vdisk.DetachVirtualDisk(
            handle_val,
            DETACH_VIRTUAL_DISK_FLAG_NONE,  # Flags
            0,                              # ProviderSpecificFlags
        )

        if ret != ERROR_SUCCESS:
            logger.error(
                "DetachVirtualDisk 失敗: error=%d (0x%08X) — %s",
                ret, ret, _win32_error_message(ret),
            )
            return False

        self._attached = False
        logger.info("虛擬磁碟已卸載")
        return True

    def close(self) -> None:
        """
        關閉 VHD handle，但不卸載磁碟。

        如果是用 PERMANENT_LIFETIME 掛載的，磁碟仍然保持掛載狀態。
        """
        if self._handle is None:
            return

        handle_val = (
            self._handle.value if hasattr(self._handle, "value") else self._handle
        )

        logger.debug("關閉 VHD handle")
        self._kernel32.CloseHandle(handle_val)
        self._handle = None

    def cleanup(self, delete_file: bool = False) -> None:
        """
        完整清理：卸載 → 關閉 handle → (選擇性) 刪除 VHD 檔案。

        Args:
            delete_file: 若為 True，同時刪除 VHD/VHDX 檔案。
        """
        logger.info("清理 VHD 資源...")

        # 1. 卸載
        if self._attached:
            self.detach()

        # 2. 關閉 handle
        self.close()

        # 3. 刪除檔案
        if delete_file and self._path:
            try:
                if os.path.isfile(self._path):
                    os.remove(self._path)
                    logger.info("已刪除 VHD 檔案: %s", self._path)
            except OSError as exc:
                logger.warning("刪除 VHD 檔案失敗: %s — %s", self._path, exc)

        self._path = None
        self._attached = False
        logger.info("VHD 清理完成")

    # ── Static / Class Methods ────────────────────────────────────────

    @staticmethod
    def is_vhdx_supported() -> bool:
        """
        檢查目前 Windows 版本是否支援 VHDX 格式。

        - Windows 7  (NT 6.1) → False (只支援 VHD)
        - Windows 8+ (NT 6.2+) → True

        Returns:
            True 表示支援 VHDX。
        """
        return _is_vhdx_supported()

    # ── Properties ────────────────────────────────────────────────────

    @property
    def is_attached(self) -> bool:
        """虛擬磁碟是否已掛載。"""
        return self._attached

    @property
    def path(self) -> Optional[str]:
        """目前開啟/建立的 VHD/VHDX 檔案路徑。"""
        return self._path

    # ── Context Manager ───────────────────────────────────────────────

    def __enter__(self) -> "VhdBridge":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()

    # ── Repr ──────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        state = "attached" if self._attached else "detached"
        handle_info = "open" if self._handle is not None else "closed"
        return (
            f"<VhdBridge path={self._path!r} "
            f"state={state} handle={handle_info}>"
        )


# ── Standalone Test ───────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=== VhdBridge 自我測試 ===")
    print(f"VHDX 支援: {VhdBridge.is_vhdx_supported()}")
    print(f"Windows 版本: {sys.getwindowsversion()}")
    print()

    # 只做基本的結構體大小檢查，不實際建立 VHD
    print("結構體大小檢查:")
    print(f"  GUID:                          {ctypes.sizeof(GUID):4d} bytes")
    print(f"  VIRTUAL_STORAGE_TYPE:          {ctypes.sizeof(VIRTUAL_STORAGE_TYPE):4d} bytes")
    print(f"  CREATE_VIRTUAL_DISK_PARAMETERS:{ctypes.sizeof(CREATE_VIRTUAL_DISK_PARAMETERS):4d} bytes")
    print(f"  OPEN_VIRTUAL_DISK_PARAMETERS:  {ctypes.sizeof(OPEN_VIRTUAL_DISK_PARAMETERS):4d} bytes")
    print(f"  ATTACH_VIRTUAL_DISK_PARAMETERS:{ctypes.sizeof(ATTACH_VIRTUAL_DISK_PARAMETERS):4d} bytes")
    print()
    print(f"MS Vendor GUID: {VIRTUAL_STORAGE_TYPE_VENDOR_MICROSOFT!r}")
    print()

    # 測試 VhdBridge 物件建立
    try:
        bridge = VhdBridge()
        print(f"VhdBridge 建立成功: {bridge!r}")
    except RuntimeError as e:
        print(f"VhdBridge 建立失敗: {e}")

    print()
    print("=== 測試完成 ===")
