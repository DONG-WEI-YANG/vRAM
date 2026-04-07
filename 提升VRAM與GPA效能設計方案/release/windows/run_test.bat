@echo off
chcp 65001 >nul 2>&1
title VRAM Booster - VHD Bridge Test
echo ============================================================
echo   VRAM Booster - VHD Bridge Live Test
echo   SD Card Mode (run from external device)
echo ============================================================
echo.

:: Check admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] Requesting administrator privileges...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo [OK] Running as administrator
echo.

:: Find our drive letter
set "DRIVE=%~d0"
echo Drive: %DRIVE%

:: Run the exe
echo Starting VRAM_Booster.exe...
echo.
"%~dp0VRAM_Booster.exe"

pause
