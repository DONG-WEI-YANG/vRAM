@echo off
REM ============================================================
REM  USB-VRAM Booster — Windows 一鍵啟動
REM  製作者：Peter Yang
REM ============================================================

title USB-VRAM Booster by Peter Yang

echo ============================================================
echo   USB-VRAM Booster v0.1.0
echo   製作者：Peter Yang
echo ============================================================
echo.

REM 檢查 Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [!] 未偵測到 Python，正在下載嵌入式 Python...
    echo [!] 請稍候...

    REM 下載嵌入式 Python
    if not exist "python_embed" (
        mkdir python_embed
        curl -L -o python_embed\python.zip https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
        cd python_embed
        tar -xf python.zip
        del python.zip
        cd ..
    )
    set PYTHON=python_embed\python.exe
) else (
    set PYTHON=python
)

echo [*] 正在啟動 USB-VRAM Booster...
echo.

REM 建立虛擬環境（如果不存在）
if not exist ".venv" (
    echo [*] 建立虛擬環境...
    %PYTHON% -m venv .venv 2>nul
)

REM 啟動（純標準庫，無需安裝套件）
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe -m usbvram.main %*
) else (
    %PYTHON% -m usbvram.main %*
)

echo.
echo [*] USB-VRAM Booster 已結束
pause
