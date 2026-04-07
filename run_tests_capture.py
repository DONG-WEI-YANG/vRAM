"""Run tests and capture output to a file."""
import subprocess
import sys

result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/test_safety_policy.py", "-v"],
    capture_output=True, text=True, timeout=30,
    cwd=r"D:\product\vRAM\提升VRAM與GPA效能設計方案"
)

with open(r"D:\product\vRAM\test_results.txt", "w") as f:
    f.write("STDOUT:\n")
    f.write(result.stdout)
    f.write("\nSTDERR:\n")
    f.write(result.stderr)
    f.write(f"\nRETURN CODE: {result.returncode}\n")
