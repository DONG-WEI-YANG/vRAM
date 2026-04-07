"""
Install vram-boost as a system service.
Windows: NSSM-based Windows Service (or Task Scheduler fallback)
Linux:   systemd unit file
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "vram-boost"
DESCRIPTION = "Automatic memory expansion via external storage"


def install():
    system = platform.system()
    if system == "Windows":
        return _install_windows()
    elif system == "Linux":
        return _install_linux()
    print(f"Unsupported OS: {system}")
    return False


def uninstall():
    system = platform.system()
    if system == "Windows":
        return _uninstall_windows()
    elif system == "Linux":
        return _uninstall_linux()
    return False


def _install_windows() -> bool:
    """Install as Windows Task Scheduler task (runs at logon with admin)."""
    daemon_path = str(Path(__file__).parent / "daemon.py")
    python_path = sys.executable

    # Use Task Scheduler (doesn't require NSSM)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{DESCRIPTION}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python_path}</Command>
      <Arguments>"{daemon_path}"</Arguments>
    </Exec>
  </Actions>
</Task>"""

    xml_path = os.path.join(os.environ.get("TEMP", "."), f"{SERVICE_NAME}.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml)

    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", SERVICE_NAME,
             "/XML", xml_path, "/F"],
            capture_output=True, text=True,
        )
        os.unlink(xml_path)
        if r.returncode == 0:
            print(f"Installed Windows task: {SERVICE_NAME}")
            print(f"  Starts at logon with admin privileges")
            print(f"  Start now: schtasks /Run /TN {SERVICE_NAME}")
            print(f"  Remove:    schtasks /Delete /TN {SERVICE_NAME} /F")
            return True
        print(f"Failed: {r.stderr}")
        return False
    except Exception as e:
        print(f"Failed: {e}")
        return False


def _uninstall_windows() -> bool:
    try:
        r = subprocess.run(
            ["schtasks", "/Delete", "/TN", SERVICE_NAME, "/F"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def _install_linux() -> bool:
    """Install as systemd service."""
    daemon_path = str(Path(__file__).parent / "daemon.py")
    python_path = sys.executable

    unit = f"""[Unit]
Description={DESCRIPTION}
After=local-fs.target

[Service]
Type=simple
ExecStart={python_path} {daemon_path}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""

    unit_path = f"/etc/systemd/system/{SERVICE_NAME}.service"
    try:
        with open(unit_path, "w") as f:
            f.write(unit)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True)
        subprocess.run(["systemctl", "start", SERVICE_NAME], check=True)
        print(f"Installed systemd service: {SERVICE_NAME}")
        print(f"  Status:  systemctl status {SERVICE_NAME}")
        print(f"  Logs:    journalctl -u {SERVICE_NAME}")
        print(f"  Remove:  systemctl disable --now {SERVICE_NAME}")
        return True
    except Exception as e:
        print(f"Failed: {e}")
        return False


def _uninstall_linux() -> bool:
    unit_path = f"/etc/systemd/system/{SERVICE_NAME}.service"
    try:
        subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
        subprocess.run(["systemctl", "disable", SERVICE_NAME], check=False)
        if os.path.exists(unit_path):
            os.unlink(unit_path)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "uninstall"])
    args = parser.parse_args()

    if args.action == "install":
        install()
    else:
        uninstall()
