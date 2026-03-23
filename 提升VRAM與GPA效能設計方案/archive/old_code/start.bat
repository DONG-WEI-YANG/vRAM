@echo off
title SD-VRAM Booster — by Peter Yang
echo ============================================
echo   SD-VRAM Booster v0.1.0
echo   by Peter Yang
echo ============================================
echo.

:: 檢查是否有內嵌 Python
if exist "%~dp0python\python.exe" (
    echo [INFO] 使用內嵌 Python 環境...
    "%~dp0python\python.exe" "%~dp0..\sdvram\main.py"
    goto :end
)

:: 檢查系統 Python
where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo [INFO] 使用系統 Python...
    python "%~dp0..\sdvram\main.py"
    goto :end
)

where python3 >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo [INFO] 使用系統 Python3...
    python3 "%~dp0..\sdvram\main.py"
    goto :end
)

:: 嘗試 PyInstaller 打包的執行檔
if exist "%~dp0SDVRAMBooster.exe" (
    echo [INFO] 使用獨立執行檔...
    "%~dp0SDVRAMBooster.exe"
    goto :end
)

echo [ERROR] 找不到 Python 或執行檔！
echo 請安裝 Python 3.8+ 或使用打包版本。
pause

:end
