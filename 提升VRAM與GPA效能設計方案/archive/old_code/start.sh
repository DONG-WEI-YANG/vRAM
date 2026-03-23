#!/bin/bash
# SD-VRAM Booster — by Peter Yang
# Linux 一鍵啟動腳本

echo "============================================"
echo "  SD-VRAM Booster v0.1.0"
echo "  by Peter Yang"
echo "============================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# 檢查是否有內嵌 Python
if [ -x "$SCRIPT_DIR/python/bin/python3" ]; then
    echo "[INFO] 使用內嵌 Python 環境..."
    "$SCRIPT_DIR/python/bin/python3" "$PROJECT_DIR/sdvram/main.py"
    exit 0
fi

# 檢查系統 Python
if command -v python3 &>/dev/null; then
    echo "[INFO] 使用系統 Python3..."
    python3 "$PROJECT_DIR/sdvram/main.py"
    exit 0
fi

if command -v python &>/dev/null; then
    echo "[INFO] 使用系統 Python..."
    python "$PROJECT_DIR/sdvram/main.py"
    exit 0
fi

# 嘗試 PyInstaller 打包的執行檔
if [ -x "$SCRIPT_DIR/SDVRAMBooster" ]; then
    echo "[INFO] 使用獨立執行檔..."
    "$SCRIPT_DIR/SDVRAMBooster"
    exit 0
fi

echo "[ERROR] 找不到 Python 或執行檔！"
echo "請安裝 Python 3.8+ 或使用打包版本。"
read -p "按 Enter 結束..."
