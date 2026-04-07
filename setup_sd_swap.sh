#!/bin/bash
# SD Card Swap Setup for Linux
# Run as root: sudo bash setup_sd_swap.sh /media/sdcard 8
# Creates a swap file on the SD card so Linux uses it as extra memory.
# ANY program benefits automatically.

MOUNT_PATH="${1:-/media/sdcard}"
SIZE_GB="${2:-8}"
SWAP_FILE="${MOUNT_PATH}/vram_boost.swap"

echo "=== SD Card Swap Setup ==="
echo "Mount: ${MOUNT_PATH}"
echo "Size:  ${SIZE_GB} GB"
echo ""

# Check root
if [ "$(id -u)" != "0" ]; then
    echo "ERROR: Must run as root (sudo)"
    exit 1
fi

# Check mount exists
if [ ! -d "$MOUNT_PATH" ]; then
    echo "ERROR: ${MOUNT_PATH} not found"
    exit 1
fi

# Check space
FREE_KB=$(df --output=avail "$MOUNT_PATH" | tail -1)
FREE_GB=$((FREE_KB / 1024 / 1024))
echo "Free space: ${FREE_GB} GB"
if [ "$FREE_GB" -lt "$SIZE_GB" ]; then
    echo "ERROR: Not enough space! Need ${SIZE_GB}GB, have ${FREE_GB}GB"
    exit 1
fi

# Speed test
echo "Testing SD card speed..."
dd if=/dev/urandom of="${MOUNT_PATH}/.speed_test" bs=4M count=1 oflag=direct 2>&1 | tail -1
rm -f "${MOUNT_PATH}/.speed_test"

# Current swaps
echo ""
echo "Current swaps:"
swapon --show
echo ""

# Create swap file
SIZE_BYTES=$((SIZE_GB * 1024 * 1024 * 1024))
echo "Creating swap file: ${SWAP_FILE} (${SIZE_GB}GB)..."

# Use fallocate for fast allocation (no fragmentation)
if command -v fallocate &> /dev/null; then
    fallocate -l ${SIZE_BYTES} "$SWAP_FILE"
else
    dd if=/dev/zero of="$SWAP_FILE" bs=1M count=$((SIZE_GB * 1024)) status=progress
fi

# Set permissions
chmod 600 "$SWAP_FILE"

# Format as swap
mkswap "$SWAP_FILE"

# Enable with high priority (lower = used first when system swap is needed)
# --discard enables TRIM for SD Express/NVMe devices
swapon -d -p 10 "$SWAP_FILE"

echo ""
echo "=== SUCCESS ==="
swapon --show
echo ""
echo "Swap is ACTIVE NOW (no reboot needed)."
echo "Any program benefits automatically."
echo ""
echo "To make persistent across reboots, add to /etc/fstab:"
echo "  ${SWAP_FILE}  none  swap  sw,pri=10  0  0"
echo ""
echo "To remove:"
echo "  sudo swapoff ${SWAP_FILE}"
echo "  sudo rm ${SWAP_FILE}"
