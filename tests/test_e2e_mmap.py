"""
End-to-end test: RealBoostEngine with MmapSwapEngine on real device.
Must run elevated. Requires SD card or USB SSD connected.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                "..", "提升VRAM與GPA效能設計方案"))


def main():
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("ERROR: Must run as Administrator!")
        return

    from microsystems.core.real_boost import RealBoostEngine

    engine = RealBoostEngine()

    print("=" * 60)
    print("E2E Test: MmapSwapEngine on Real Device")
    print("=" * 60)

    # Find external drive
    drive = None
    for letter in "EFGHIJKLMNOPQRSTUVWXYZ":
        path = f"{letter}:\\"
        if os.path.exists(path) and letter != "C":
            drive = letter
            break

    if not drive:
        print("ERROR: No external drive found")
        return

    print(f"\n[1] Drive: {drive}:\\")

    # Memory before
    mem_before = engine.get_system_memory()
    print(f"    RAM: {mem_before['physical_total']/(1024**3):.1f}GB")

    # Activate
    print(f"\n[2] Activating on {drive}:\\...")
    msgs = []
    result = engine.activate(drive, use_percent=80.0,
                             on_progress=lambda m: (msgs.append(m), print(f"    >> {m}")))

    print(f"\n    Result:")
    for k, v in result.items():
        print(f"      {k}: {v}")

    if not result.get("success"):
        print(f"\n    FAILED: {result.get('error')}")
        return

    # Verify swap file on device
    swap_path = result.get("swap_path", "")
    print(f"\n[3] Swap file:")
    print(f"    Path: {swap_path}")
    on_device = swap_path.startswith(f"{drive}:")

    if os.path.exists(swap_path):
        fsize_mb = os.path.getsize(swap_path) / (1024**2)
        print(f"    Size: {fsize_mb:.0f} MB")
        print(f"    On device: {'YES' if on_device else 'NO (on C:)'}")
    else:
        print(f"    MISSING!")

    # Status
    status = engine.status()
    print(f"\n[4] Status:")
    for k, v in status.items():
        if isinstance(v, float):
            print(f"    {k}: {v:.2f}")
        else:
            print(f"    {k}: {v}")

    # GPU
    gpu = RealBoostEngine.get_gpu_info()
    print(f"\n[5] GPU: {gpu['name']} ({gpu['vram_total_mb']}MB)")

    # Read/write test on swap blocks
    print(f"\n[6] Block I/O test:")
    if hasattr(engine, '_mmap_engine') and engine._mmap_engine:
        me = engine._mmap_engine
        if hasattr(me, '_swap') and me._swap:
            # Write to block 0
            test_data = b"VRAM_BOOSTER_E2E_TEST_" + os.urandom(100)
            ok = me._swap.write_block(0, test_data)
            print(f"    Write block 0: {'OK' if ok else 'FAIL'}")

            # Read back
            readback = me._swap.read_block(0, offset=0, size=len(test_data))
            match = readback == test_data
            print(f"    Read block 0:  {'MATCH' if match else 'MISMATCH'}")
        else:
            print(f"    No swap manager available")
    else:
        print(f"    No mmap engine available")

    # Deactivate
    print(f"\n[7] Deactivating...")
    engine.deactivate()
    print(f"    Active: {engine.status().get('active', 'unknown')}")

    # Summary
    print(f"\n{'='*60}")
    checks = [
        ("Swap file on external device", on_device),
        ("Method is mmap_swap", result.get("method") == "mmap_swap"),
        ("No reboot needed", result.get("needs_reboot") == False),
        ("Deactivate clean", not engine.status().get("active", True)),
    ]
    all_pass = True
    for name, ok in checks:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name}")
        if not ok:
            all_pass = False

    print(f"\n  RESULT: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    print(f"  Swap: +{result.get('added_gb', 0):.1f} GB on {swap_path}")
    print(f"  Speed: {result.get('rand_write_mbs', 0):.1f} MB/s")
    print(f"{'='*60}")


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "test_output.txt")
    sys.stdout = open(out, "w", encoding="utf-8")
    sys.stderr = sys.stdout
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\nEXCEPTION: {e}")
        traceback.print_exc()
    finally:
        sys.stdout.close()
