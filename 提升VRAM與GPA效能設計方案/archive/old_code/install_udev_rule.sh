#!/bin/bash
# SD-VRAM Booster — Linux udev 自動偵測安裝腳本
# 製作者：Peter Yang
#
# 安裝 udev 規則，讓 SD 卡插入時自動啟動 SD-VRAM Booster
# 使用方式: sudo bash install_udev_rule.sh

RULE_FILE="/etc/udev/rules.d/99-sdvram-booster.rules"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "SD-VRAM Booster — udev 規則安裝"
echo "by Peter Yang"
echo "─────────────────────────────────"

if [ "$EUID" -ne 0 ]; then
    echo "[ERROR] 請使用 sudo 執行此腳本"
    exit 1
fi

# 建立 udev 規則
cat > "$RULE_FILE" << 'EOF'
# SD-VRAM Booster — 自動偵測 SD 卡插入
# 當 SD 卡 (mmcblk) 或 NVMe SD Express 卡插入時觸發

# 傳統 SD 卡模式
ACTION=="add", SUBSYSTEM=="block", KERNEL=="mmcblk[0-9]*", \
    RUN+="/usr/local/bin/sdvram-notify"

# SD Express NVMe 模式
ACTION=="add", SUBSYSTEM=="block", KERNEL=="nvme[0-9]*n[0-9]*", \
    ATTRS{model}=="*SD*", \
    RUN+="/usr/local/bin/sdvram-notify"
EOF

# 建立通知腳本
cat > /usr/local/bin/sdvram-notify << SCRIPT
#!/bin/bash
# 通知桌面環境啟動 SD-VRAM Booster
DISPLAY=:0 XAUTHORITY=/home/\$(logname)/.Xauthority \
    su \$(logname) -c "python3 $PROJECT_DIR/sdvram/main.py &"
SCRIPT

chmod +x /usr/local/bin/sdvram-notify

# 重新載入 udev 規則
udevadm control --reload-rules
udevadm trigger

echo "[OK] udev 規則已安裝: $RULE_FILE"
echo "[OK] SD 卡插入時將自動啟動 SD-VRAM Booster"
