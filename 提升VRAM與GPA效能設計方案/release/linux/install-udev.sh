#!/bin/bash
#
# VRAM Booster — Linux udev 自動觸發安裝
#
# 執行一次後，插入任何可移除式儲存裝置都會自動啟動 VRAM Booster。
# 這是 Linux 原生的「隨插即用」機制，比 Windows autorun 更可靠。
#
# 用法：sudo ./install-udev.sh
# 移除：sudo ./install-udev.sh --remove
#

RULE_FILE="/etc/udev/rules.d/99-vram-booster.rules"
SCRIPT_FILE="/usr/local/bin/vram-booster-trigger"

if [ "$1" = "--remove" ]; then
    echo "Removing VRAM Booster udev rule..."
    sudo rm -f "$RULE_FILE" "$SCRIPT_FILE"
    sudo udevadm control --reload-rules
    echo "Done. VRAM Booster auto-trigger removed."
    exit 0
fi

if [ "$EUID" -ne 0 ]; then
    echo "需要 root 權限。請用 sudo 執行："
    echo "  sudo $0"
    exit 1
fi

echo "Installing VRAM Booster udev rule..."

# 1. 建立觸發腳本
cat > "$SCRIPT_FILE" << 'TRIGGER_EOF'
#!/bin/bash
# VRAM Booster — udev 觸發腳本
# 當可移除式儲存裝置掛載後，檢查是否有 vram-booster 執行檔

sleep 2  # 等待掛載完成

# 找到新掛載的裝置
MOUNT_POINT=""
for mp in /media/$USER/* /run/media/$USER/* /mnt/*; do
    if [ -d "$mp" ] && [ -f "$mp/booster/vram-booster" ]; then
        MOUNT_POINT="$mp"
        break
    fi
done

if [ -n "$MOUNT_POINT" ]; then
    # 找到 VRAM Booster 卡 — 啟動 GUI
    export DISPLAY=:0
    export XAUTHORITY=/home/$USER/.Xauthority

    # 嘗試用 Wayland 或 X11
    if [ -n "$WAYLAND_DISPLAY" ] || [ -n "$DISPLAY" ]; then
        su $USER -c "python3 '$MOUNT_POINT/booster/vram-booster'" &
    fi
fi
TRIGGER_EOF

chmod +x "$SCRIPT_FILE"

# 2. 建立 udev rule
# 觸發條件：任何 USB/SD 區塊裝置出現時
cat > "$RULE_FILE" << 'RULE_EOF'
# VRAM Booster — 可移除式儲存裝置自動偵測
# 當 USB/SD 裝置的分區出現時觸發

# USB 儲存裝置
ACTION=="add", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", ENV{ID_FS_USAGE}=="filesystem", RUN+="/usr/local/bin/vram-booster-trigger"

# SD 卡
ACTION=="add", SUBSYSTEM=="block", ENV{ID_BUS}=="memstick", ENV{ID_FS_USAGE}=="filesystem", RUN+="/usr/local/bin/vram-booster-trigger"
ACTION=="add", SUBSYSTEM=="block", ATTRS{removable}=="1", ENV{ID_FS_USAGE}=="filesystem", RUN+="/usr/local/bin/vram-booster-trigger"
RULE_EOF

# 3. 重新載入 udev 規則
udevadm control --reload-rules
udevadm trigger

echo ""
echo "✓ 安裝完成！"
echo ""
echo "  從現在起，插入含有 VRAM Booster 的儲存裝置"
echo "  系統會自動偵測並啟動。"
echo ""
echo "  移除：sudo $0 --remove"
echo ""
