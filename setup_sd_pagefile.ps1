# SD Card Pagefile Setup Script
# Run as Administrator: Right-click → Run as Administrator
# This creates a pagefile on the SD card so Windows OS can use it as extra memory.
# ANY program (not just Python) benefits automatically.

param(
    [string]$DriveLetter = "E",
    [int]$SizeGB = 8
)

$ErrorActionPreference = "Stop"

Write-Host "=== SD Card Pagefile Setup ===" -ForegroundColor Cyan
Write-Host "Drive: ${DriveLetter}:\"
Write-Host "Size:  ${SizeGB} GB"
Write-Host ""

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: Must run as Administrator!" -ForegroundColor Red
    Write-Host "Right-click this script -> Run as Administrator"
    pause
    exit 1
}

# Check drive exists
if (-not (Test-Path "${DriveLetter}:\")) {
    Write-Host "ERROR: Drive ${DriveLetter}:\ not found!" -ForegroundColor Red
    pause
    exit 1
}

# Check drive has enough space
$drive = Get-PSDrive $DriveLetter
$freeGB = [math]::Round($drive.Free / 1GB, 1)
Write-Host "Free space: ${freeGB} GB"
if ($freeGB -lt $SizeGB) {
    Write-Host "ERROR: Not enough space! Need ${SizeGB} GB, have ${freeGB} GB" -ForegroundColor Red
    pause
    exit 1
}

# Show current pagefiles
Write-Host ""
Write-Host "Current pagefiles:" -ForegroundColor Yellow
Get-CimInstance Win32_PageFileUsage | Format-Table Name, AllocatedBaseSize, CurrentUsage -AutoSize

# Speed test
Write-Host "Testing SD card speed..." -ForegroundColor Yellow
$testFile = "${DriveLetter}:\.speed_test"
$data = New-Object byte[] (4MB)
(New-Object Random).NextBytes($data)

$sw = [System.Diagnostics.Stopwatch]::StartNew()
[IO.File]::WriteAllBytes($testFile, $data)
$sw.Stop()
$speedMB = 4 / ($sw.ElapsedMilliseconds / 1000)
Remove-Item $testFile -ErrorAction SilentlyContinue
Write-Host "SD card write speed: $([math]::Round($speedMB, 1)) MB/s"

# Create pagefile
$sizeMB = $SizeGB * 1024
$pagefilePath = "${DriveLetter}:\pagefile.sys"

Write-Host ""
Write-Host "Creating pagefile: $pagefilePath ($sizeMB MB)..." -ForegroundColor Green

try {
    # Method 1: WMI (works on most Windows)
    $pf = [wmiclass]"Win32_PageFileSetting"
    $new = $pf.CreateInstance()
    $new.Name = $pagefilePath
    $new.InitialSize = $sizeMB
    $new.MaximumSize = $sizeMB
    $new.Put() | Out-Null
    Write-Host "SUCCESS: Pagefile created!" -ForegroundColor Green
} catch {
    Write-Host "WMI method failed: $_" -ForegroundColor Yellow
    Write-Host "Trying alternative method..." -ForegroundColor Yellow

    try {
        # Method 2: wmic (legacy but reliable)
        $cmd = "wmic pagefileset create name=`"${pagefilePath}`""
        Invoke-Expression $cmd
        $cmd2 = "wmic pagefileset where name=`"${DriveLetter}:\\pagefile.sys`" set InitialSize=${sizeMB},MaximumSize=${sizeMB}"
        Invoke-Expression $cmd2
        Write-Host "SUCCESS: Pagefile created (wmic)!" -ForegroundColor Green
    } catch {
        Write-Host "ERROR: Failed to create pagefile: $_" -ForegroundColor Red
        pause
        exit 1
    }
}

# Verify
Write-Host ""
Write-Host "Verifying..." -ForegroundColor Yellow
Get-CimInstance Win32_PageFileSetting | Format-Table Name, InitialSize, MaximumSize -AutoSize

Write-Host ""
Write-Host "=== IMPORTANT ===" -ForegroundColor Cyan
Write-Host "The pagefile will be ACTIVE after REBOOT."
Write-Host "After reboot, Windows will automatically use the SD card as extra memory."
Write-Host "Any program (CUDA, PyTorch, Chrome, etc.) benefits automatically."
Write-Host ""
Write-Host "To verify after reboot:"
Write-Host "  Get-CimInstance Win32_PageFileUsage | Format-Table Name, AllocatedBaseSize, CurrentUsage"
Write-Host ""
Write-Host "To remove later:"
Write-Host "  Run: wmic pagefileset where name='${DriveLetter}:\\pagefile.sys' delete"
Write-Host ""
pause
