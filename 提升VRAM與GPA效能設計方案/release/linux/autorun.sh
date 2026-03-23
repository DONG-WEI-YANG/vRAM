#!/bin/bash
# 插入裝置後雙擊此檔案即可啟動
# 需要 root 權限建立 swap（會用 pkexec 提示密碼）
cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
    exec pkexec "$0" "$@" || exec sudo "$0" "$@"
fi
./VRAM_Booster &
