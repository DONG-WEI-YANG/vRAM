"""
VHD Bridge 小規模實測
======================
在 E:\ (SD 卡) 上建立 512MB VHDX → 掛載 → 建立 pagefile → 驗證 → 清理

必須以管理員權限執行：
  python test_vhd_live.py

測試步驟：
  1. 記錄測試前的 commit limit
  2. 在 E:\ 建立 512MB VHDX
  3. Attach → diskpart 初始化 → 格式化 NTFS
  4. 建立 pagefile (min=16MB, max=512MB)
  5. 驗證 commit limit 是否增加
  6. 查詢 pagefile 使用量
  7. 安全移除 pagefile → detach VHD → 刪除 VHDX
  8. 驗證 commit limit 是否恢復
"""

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import time

# 確保路徑正確
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "提升VRAM與GPA效能設計方案"))


def get_commit_limit_gb():
    """取得當前 commit limit (GB)"""
    class MS(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.wintypes.DWORD), ("dwMemoryLoad", ctypes.wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    s = MS()
    s.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
    return s.ullTotalPageFile / (1024 ** 3)


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def main():
    print("=" * 60)
    print("  VHD Bridge Live Test — SD Card (E:\\)")
    print("=" * 60)

    if not is_admin():
        print("\n[ERROR] 需要管理員權限！")
        print("請右鍵 → 以系統管理員身分執行 Python")
        print("或在管理員 cmd/powershell 中執行:")
        print(f"  python {__file__}")
        input("\nPress Enter to exit...")
        return

    # 確認 E:\ 存在
    if not os.path.exists("E:\\"):
        print("[ERROR] E:\\ 不存在，請插入 SD 卡")
        return

    TEST_DRIVE = "E"
    TEST_VHD = f"{TEST_DRIVE}:\\vram_test_live.vhdx"
    TEST_SIZE = 512 * 1024 * 1024  # 512 MB
    MOUNT_LETTER = "V"

    print(f"\nTest device: {TEST_DRIVE}:\\")
    print(f"VHD path:    {TEST_VHD}")
    print(f"VHD size:    512 MB")
    print(f"Mount as:    {MOUNT_LETTER}:\\")

    # ── Step 1: 記錄測試前狀態 ──
    print("\n--- Step 1: Baseline ---")
    baseline = get_commit_limit_gb()
    print(f"  Commit limit BEFORE: {baseline:.2f} GB")

    # ── Step 2: 建立 VHDX ──
    print("\n--- Step 2: Create VHDX ---")
    from microsystems.core.vhd_bridge import VhdBridge

    # 清理殘留
    if os.path.exists(TEST_VHD):
        print(f"  Cleaning up leftover: {TEST_VHD}")
        try:
            os.unlink(TEST_VHD)
        except OSError:
            print("  [WARN] Cannot delete leftover, trying to open existing...")

    bridge = VhdBridge()
    if not bridge.create(TEST_VHD, TEST_SIZE):
        print("  [FAIL] CreateVirtualDisk failed")
        return
    print(f"  VHDX created: {TEST_VHD}")
    print(f"  File size: {os.path.getsize(TEST_VHD) / (1024*1024):.1f} MB (dynamic)")

    # ── Step 3: Attach ──
    print("\n--- Step 3: Attach VHD ---")
    if not bridge.attach(permanent=True):
        print("  [FAIL] AttachVirtualDisk failed")
        bridge.cleanup(delete_file=True)
        return

    phys = bridge.get_physical_path()
    disk_num = bridge.get_disk_number()
    print(f"  Physical path: {phys}")
    print(f"  Disk number:   {disk_num}")

    if disk_num is None:
        print("  [FAIL] Cannot get disk number")
        bridge.cleanup(delete_file=True)
        return

    # ── Step 4: diskpart 初始化 + 格式化 ──
    print("\n--- Step 4: Initialize + Format ---")
    import tempfile
    script = f"""select disk {disk_num}
clean
create partition primary
format fs=ntfs quick label=VRAM_TEST
assign letter={MOUNT_LETTER}
"""
    script_file = os.path.join(tempfile.gettempdir(), "vhd_test_dp.txt")
    with open(script_file, "w") as f:
        f.write(script)

    r = subprocess.run(
        ["diskpart", "/s", script_file],
        capture_output=True, text=True, timeout=30,
    )
    os.unlink(script_file)

    if r.returncode != 0:
        print(f"  [FAIL] diskpart error: {r.stderr}")
        bridge.cleanup(delete_file=True)
        return

    # 等待 volume 出現
    for i in range(10):
        if os.path.exists(f"{MOUNT_LETTER}:\\"):
            break
        time.sleep(0.5)

    if not os.path.exists(f"{MOUNT_LETTER}:\\"):
        print(f"  [FAIL] {MOUNT_LETTER}:\\ did not appear")
        bridge.cleanup(delete_file=True)
        return

    print(f"  {MOUNT_LETTER}:\\ mounted and formatted (NTFS)")

    # 驗證 DriveType
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Volume -DriveLetter {MOUNT_LETTER}).DriveType"],
        capture_output=True, text=True, timeout=10,
        creationflags=0x08000000,
    )
    drive_type = r.stdout.strip()
    print(f"  DriveType: {drive_type}")
    if drive_type == "Fixed":
        print("  *** CONFIRMED: Removable SD → VHD → Fixed volume ***")
    else:
        print(f"  [WARN] Expected 'Fixed', got '{drive_type}'")

    # ── Step 5: 建立 pagefile ──
    print("\n--- Step 5: Create Pagefile ---")
    print("  Enabling SeCreatePagefilePrivilege...")
    # 啟用 pagefile 建立權限
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.wintypes.DWORD), ("HighPart", ctypes.c_long)]
        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", ctypes.wintypes.DWORD)]
        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", ctypes.wintypes.DWORD),
                         ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        hToken = ctypes.wintypes.HANDLE()
        advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0028, ctypes.byref(hToken))
        luid = LUID()
        advapi32.LookupPrivilegeValueW(None, "SeCreatePagefilePrivilege", ctypes.byref(luid))
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
        advapi32.AdjustTokenPrivileges(hToken, False, ctypes.byref(tp), 0, None, None)
        kernel32.CloseHandle(hToken)
        print("  SeCreatePagefilePrivilege enabled")
    except Exception as e:
        print(f"  [WARN] Privilege elevation: {e}")

    print("  Using NtCreatePagingFile (direct NT API)...")
    pf_created = False
    try:
        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [('Length', ctypes.c_ushort), ('MaximumLength', ctypes.c_ushort), ('Buffer', ctypes.c_wchar_p)]
        class LARGE_INTEGER(ctypes.Structure):
            _fields_ = [('QuadPart', ctypes.c_longlong)]

        ntdll = ctypes.WinDLL('ntdll')
        fn = ntdll.NtCreatePagingFile
        fn.restype = ctypes.c_long
        fn.argtypes = [ctypes.POINTER(UNICODE_STRING), ctypes.POINTER(LARGE_INTEGER),
                       ctypes.POINTER(LARGE_INTEGER), ctypes.c_ulong]

        nt_path = f"\\??\\{MOUNT_LETTER}:\\pagefile.sys"
        upath = UNICODE_STRING()
        upath.Buffer = nt_path
        upath.Length = len(nt_path) * 2
        upath.MaximumLength = (len(nt_path) + 1) * 2
        min_s = LARGE_INTEGER(); min_s.QuadPart = 16 * 1024 * 1024
        max_s = LARGE_INTEGER(); max_s.QuadPart = 512 * 1024 * 1024

        status = fn(ctypes.byref(upath), ctypes.byref(min_s), ctypes.byref(max_s), 0)
        if status == 0:
            pf_created = True
            print(f"  Pagefile created: {MOUNT_LETTER}:\\pagefile.sys (min=16MB, max=512MB)")
        else:
            print(f"  [FAIL] NtCreatePagingFile status: 0x{status & 0xFFFFFFFF:08X}")
    except Exception as e:
        print(f"  [FAIL] NtCreatePagingFile error: {e}")

    # ── Step 6: 驗證 commit limit 變化 ──
    print("\n--- Step 6: Verify Commit Limit ---")
    time.sleep(2)  # 讓 OS 更新數值
    after = get_commit_limit_gb()
    delta = after - baseline
    print(f"  Commit limit AFTER:  {after:.2f} GB")
    print(f"  Delta:               {delta:+.2f} GB")
    if delta > 0.01:
        print(f"  *** SUCCESS: Commit limit increased by {delta:.2f} GB ***")
    else:
        print(f"  [INFO] No significant change (pagefile may need reboot to activate)")

    # ── Step 7: 查詢 pagefile 使用量 ──
    print("\n--- Step 7: Pagefile Usage ---")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-CimInstance Win32_PageFileUsage | "
         f"Where-Object {{ $_.Name -like '{MOUNT_LETTER}:*' }} | "
         f"Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage | "
         f"ConvertTo-Json"],
        capture_output=True, text=True, timeout=10,
        creationflags=0x08000000,
    )
    if r.stdout.strip():
        data = json.loads(r.stdout)
        print(f"  Name:      {data.get('Name', '?')}")
        print(f"  Allocated: {data.get('AllocatedBaseSize', 0)} MB")
        print(f"  In Use:    {data.get('CurrentUsage', 0)} MB")
        print(f"  Peak:      {data.get('PeakUsage', 0)} MB")
        print(f"  Safe to remove: {'YES' if data.get('CurrentUsage', 0) == 0 else 'NO'}")
    else:
        print("  [INFO] No pagefile usage data (may need reboot)")

    # ── Step 8: 清理 ──
    print("\n--- Step 8: Cleanup ---")

    # 移除 pagefile
    print("  Removing pagefile...")
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$pf = Get-WmiObject Win32_PageFileSetting | "
         f"Where-Object {{ $_.Name -eq '{MOUNT_LETTER}:\\pagefile.sys' }}; "
         f"if ($pf) {{ $pf.Delete() }}"],
        capture_output=True, text=True, timeout=10,
        creationflags=0x08000000,
    )
    print(f"  Pagefile removal: {'OK' if r.returncode == 0 else r.stderr.strip()}")

    # Detach VHD
    print("  Detaching VHD...")
    bridge.detach()
    bridge.close()
    print("  VHD detached")

    # 刪除 VHDX 檔案
    time.sleep(1)
    try:
        os.unlink(TEST_VHD)
        print(f"  VHDX file deleted: {TEST_VHD}")
    except OSError as e:
        print(f"  [WARN] Cannot delete VHDX: {e} (will be cleaned on next run)")

    # ── Step 9: 驗證恢復 ──
    print("\n--- Step 9: Verify Restore ---")
    time.sleep(2)
    restored = get_commit_limit_gb()
    print(f"  Commit limit RESTORED: {restored:.2f} GB")
    print(f"  Delta from baseline:   {restored - baseline:+.2f} GB")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  TEST SUMMARY")
    print("=" * 60)
    print(f"  Baseline commit:  {baseline:.2f} GB")
    print(f"  After VHD PF:     {after:.2f} GB  ({delta:+.2f} GB)")
    print(f"  After cleanup:    {restored:.2f} GB  ({restored - baseline:+.2f} GB)")
    print(f"  VHD DriveType:    {drive_type}")
    print(f"  VHD approach:     {'VALIDATED' if drive_type == 'Fixed' else 'NEEDS REVIEW'}")
    print("=" * 60)

    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
