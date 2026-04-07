@echo off
chcp 65001 >nul 2>&1
title VRAM Booster - VHD Bridge Quick Test
echo ============================================================
echo   VHD Bridge Quick Test
echo   Verifies: SD card → VHDX → Mount as Fixed → Pagefile
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

:: Find drive
set "DRIVE=%~d0"
set "LETTER=%DRIVE:~0,1%"
echo Source drive: %DRIVE% (%LETTER%:)
echo.

:: Check Python (try system Python first, then embedded)
where python >nul 2>&1
if %errorlevel% equ 0 (
    set "PY=python"
) else (
    echo [!] Python not found in PATH
    echo     Please install Python or run VRAM_Booster.exe directly
    pause
    exit /b 1
)

:: Run the live test
echo Running VHD bridge test...
echo.
%PY% -B "%~dp0..\..\test_vhd_live.py"
if %errorlevel% neq 0 (
    echo.
    echo [!] Test script not found at expected path.
    echo     Trying from current directory...
    %PY% -B "test_vhd_live.py"
)

echo.
pause
