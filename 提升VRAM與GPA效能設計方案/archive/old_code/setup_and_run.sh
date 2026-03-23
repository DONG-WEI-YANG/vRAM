#!/bin/bash
# ============================================================
#  USB-VRAM Booster — Linux 一鍵啟動
#  製作者：Peter Yang
# ============================================================

echo "============================================================"
echo "  USB-VRAM Booster v0.1.0"
echo "  製作者：Peter Yang"
echo "============================================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 檢查 Python 3
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "[!] 未偵測到 Python 3，請先安裝："
    echo "    sudo apt install python3  (Ubuntu/Debian)"
    echo "    sudo dnf install python3  (Fedora)"
    exit 1
fi

echo "[*] 使用 Python: $($PYTHON --version)"
echo "[*] 正在啟動 USB-VRAM Booster..."
echo ""

# 啟動（純標準庫，無需安裝套件）
$PYTHON -m usbvram.main "$@"
