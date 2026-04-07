"""Quick SD card speed test"""
import os, time, random, shutil

MB = 1024 * 1024
drive = "E:/"

test_dir = os.path.join(drive, ".vram_speedtest")
os.makedirs(test_dir, exist_ok=True)

total, used, free = shutil.disk_usage(drive)
print(f"SD Card: {drive}")
print(f"Capacity: {total/1024**3:.1f} GB, Free: {free/1024**3:.1f} GB")
print()

test_file = os.path.join(test_dir, "seq_write.bin")

# === Sequential Write ===
print("=== Sequential Write (4MB blocks) ===")
block = os.urandom(4 * MB)
for size_mb in [16, 64, 256]:
    count = size_mb // 4
    fd = os.open(test_file, os.O_CREAT | os.O_RDWR | os.O_BINARY | os.O_TRUNC)
    t0 = time.perf_counter()
    for _ in range(count):
        os.write(fd, block)
    os.fsync(fd)
    t1 = time.perf_counter()
    os.close(fd)
    speed = size_mb / (t1 - t0)
    print(f"  {size_mb:>4}MB: {speed:>7.1f} MB/s  ({(t1-t0)*1000:.0f}ms)")

# === Sequential Read ===
print()
print("=== Sequential Read (4MB blocks) ===")
for size_mb in [16, 64, 256]:
    count = size_mb // 4
    # Clear OS cache by re-opening
    fd = os.open(test_file, os.O_RDONLY | os.O_BINARY)
    t0 = time.perf_counter()
    for _ in range(count):
        data = os.read(fd, 4 * MB)
    t1 = time.perf_counter()
    os.close(fd)
    speed = size_mb / (t1 - t0)
    print(f"  {size_mb:>4}MB: {speed:>7.1f} MB/s  ({(t1-t0)*1000:.0f}ms)")

# === Random 4K Write ===
print()
print("=== Random 4K Write ===")
small = os.urandom(4096)
fd = os.open(test_file, os.O_RDWR | os.O_BINARY)
file_size = os.fstat(fd).st_size
count = 256
t0 = time.perf_counter()
for _ in range(count):
    pos = random.randint(0, max(0, file_size - 4096)) & ~4095
    os.lseek(fd, pos, 0)
    os.write(fd, small)
os.fsync(fd)
t1 = time.perf_counter()
os.close(fd)
iops = count / (t1 - t0)
rnd_speed = (count * 4096 / MB) / (t1 - t0)
print(f"  {count} ops: {iops:.0f} IOPS ({rnd_speed:.2f} MB/s)")

# === Random 4K Read ===
print()
print("=== Random 4K Read ===")
fd = os.open(test_file, os.O_RDONLY | os.O_BINARY)
t0 = time.perf_counter()
for _ in range(count):
    pos = random.randint(0, max(0, file_size - 4096)) & ~4095
    os.lseek(fd, pos, 0)
    data = os.read(fd, 4096)
t1 = time.perf_counter()
os.close(fd)
iops = count / (t1 - t0)
rnd_speed = (count * 4096 / MB) / (t1 - t0)
print(f"  {count} ops: {iops:.0f} IOPS ({rnd_speed:.2f} MB/s)")

# === QATC Impact ===
print()
print("=== QATC Impact (GPTQ-INT4, 30x compression) ===")
# Use the sequential write speed for calculation
fd = os.open(test_file, os.O_CREAT | os.O_RDWR | os.O_BINARY | os.O_TRUNC)
t0 = time.perf_counter()
for _ in range(64):
    os.write(fd, block)  # 256MB
os.fsync(fd)
t1 = time.perf_counter()
os.close(fd)
raw_speed = 256 / (t1 - t0)
effective = raw_speed * 30
print(f"  Raw SD speed:      {raw_speed:>8.1f} MB/s")
print(f"  QATC effective:    {effective:>8.1f} MB/s  (30x GPTQ compression)")
print(f"  DDR4 RAM:          ~25,000 MB/s")
print(f"  Ratio SD+QATC/RAM: {effective/25000*100:.1f}%")

# Cleanup
try:
    os.unlink(test_file)
    os.rmdir(test_dir)
except:
    pass
