"""Test all connected external devices side by side"""
import os, time, random, shutil

MB = 1024 * 1024

def test_device(drive, label):
    print(f"\n{'=' * 60}")
    print(f"  {label}: {drive}")
    total, used, free = shutil.disk_usage(drive)
    print(f"  Capacity: {total/1024**3:.1f} GB, Free: {free/1024**3:.1f} GB")
    print(f"{'=' * 60}")

    test_dir = os.path.join(drive, ".vram_speedtest")
    os.makedirs(test_dir, exist_ok=True)
    test_file = os.path.join(test_dir, "bench.bin")

    block = os.urandom(4 * MB)
    results = {}

    # Sequential Write 256MB
    fd = os.open(test_file, os.O_CREAT | os.O_RDWR | os.O_BINARY | os.O_TRUNC)
    t0 = time.perf_counter()
    for _ in range(64):
        os.write(fd, block)
    os.fsync(fd)
    t1 = time.perf_counter()
    os.close(fd)
    seq_w = 256 / (t1 - t0)
    results["seq_write_mbs"] = seq_w
    print(f"  Seq Write (256MB):  {seq_w:>8.1f} MB/s")

    # Sequential Read 256MB (first read may be cached)
    # Force re-read by closing and reopening
    fd = os.open(test_file, os.O_RDONLY | os.O_BINARY)
    t0 = time.perf_counter()
    for _ in range(64):
        os.read(fd, 4 * MB)
    t1 = time.perf_counter()
    os.close(fd)
    seq_r = 256 / (t1 - t0)
    results["seq_read_mbs"] = seq_r
    print(f"  Seq Read  (256MB):  {seq_r:>8.1f} MB/s")

    # Random 4K Write
    small = os.urandom(4096)
    fd = os.open(test_file, os.O_RDWR | os.O_BINARY)
    fs = os.fstat(fd).st_size
    count = 256
    t0 = time.perf_counter()
    for _ in range(count):
        pos = random.randint(0, max(0, fs - 4096)) & ~4095
        os.lseek(fd, pos, 0)
        os.write(fd, small)
    os.fsync(fd)
    t1 = time.perf_counter()
    os.close(fd)
    r4k_w = count / (t1 - t0)
    results["rand_4k_w_iops"] = r4k_w
    print(f"  Rand 4K Write:      {r4k_w:>8.0f} IOPS ({r4k_w*4/1024:.1f} MB/s)")

    # Random 4K Read
    fd = os.open(test_file, os.O_RDONLY | os.O_BINARY)
    t0 = time.perf_counter()
    for _ in range(count):
        pos = random.randint(0, max(0, fs - 4096)) & ~4095
        os.lseek(fd, pos, 0)
        os.read(fd, 4096)
    t1 = time.perf_counter()
    os.close(fd)
    r4k_r = count / (t1 - t0)
    results["rand_4k_r_iops"] = r4k_r
    print(f"  Rand 4K Read:       {r4k_r:>8.0f} IOPS ({r4k_r*4/1024:.1f} MB/s)")

    # QATC effective
    qatc = seq_w * 30
    results["qatc_effective_mbs"] = qatc
    print(f"  QATC Effective:     {qatc:>8.1f} MB/s (30x GPTQ)")

    # Auto-tune
    from importlib import import_module
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                    "提升VRAM與GPA效能設計方案"))
    from microsystems.systems._transfer_mixin import TransferHandlerMixin
    params = TransferHandlerMixin._auto_tune_params(seq_w, free)
    results["block_mb"] = params["block_size"] // MB
    results["workers"] = params["workers"]
    results["compress"] = params["compress"]
    print(f"  Auto-tune: block={results['block_mb']}MB, workers={results['workers']}, compress={results['compress']}")

    # Cleanup
    try:
        os.unlink(test_file)
        os.rmdir(test_dir)
    except:
        pass

    return results


# Test all devices
all_results = {}

devices = [
    ("E:/", "SD Card (58GB, Realtek PCIE)"),
    ("G:/", "USB Drive (117GB, VendorCo)"),
]

for drive, label in devices:
    if os.path.isdir(drive):
        all_results[label] = test_device(drive, label)

# Summary comparison
print(f"\n{'=' * 60}")
print(f"  COMPARISON TABLE")
print(f"{'=' * 60}")
print(f"  {'Metric':<22}", end="")
for label in all_results:
    short = label.split("(")[0].strip()
    print(f" {short:>15}", end="")
print()
print(f"  {'-'*22}", end="")
for _ in all_results:
    print(f" {'-'*15}", end="")
print()

metrics = [
    ("Seq Write (MB/s)", "seq_write_mbs", ".1f"),
    ("Seq Read (MB/s)", "seq_read_mbs", ".1f"),
    ("4K Write (IOPS)", "rand_4k_w_iops", ".0f"),
    ("4K Read (IOPS)", "rand_4k_r_iops", ".0f"),
    ("QATC Eff (MB/s)", "qatc_effective_mbs", ".1f"),
    ("Block Size (MB)", "block_mb", ".0f"),
    ("Workers", "workers", ".0f"),
    ("Compress", "compress", ""),
]

for label, key, fmt in metrics:
    print(f"  {label:<22}", end="")
    for dev_results in all_results.values():
        val = dev_results.get(key, "N/A")
        if fmt and isinstance(val, (int, float)):
            print(f" {val:>15{fmt}}", end="")
        else:
            print(f" {str(val):>15}", end="")
    print()
