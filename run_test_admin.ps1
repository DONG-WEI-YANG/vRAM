$logFile = "D:\product\vRAM\test_vhd_result.txt"
$pythonExe = "C:\Python314\python.exe"
$testScript = "D:\product\vRAM\test_vhd_live.py"

# Redirect both stdout and stderr to log file
& $pythonExe -B $testScript *> $logFile 2>&1

# Also append exit code
Add-Content $logFile "`nExit code: $LASTEXITCODE"
