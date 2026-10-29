"""Run Lantern jobs while no session is open, using the OS's own scheduler.

plan() only describes the change (files to write, commands to run); apply() makes it.
"""
import subprocess
import sys
from pathlib import Path


def _launcher(entry: Path, job: str) -> list[str]:
    return ["uv", "run", "--no-project", "--quiet", str(entry), "job", job]


def plan(job: str, hour: int, entry: Path, platform: str = sys.platform, home: Path = Path.home()) -> dict:
    """Daily job at `hour`:00 local time. Returns {"files": {path: text}, "commands": [[argv], ...]}."""
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    argv = _launcher(entry, job)
    name = f"lantern-{job}"
    if platform == "win32":
        return {"files": {}, "commands": [[
            "schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", f"{hour:02d}:00",
            "/TN", name, "/TR", subprocess.list2cmdline(argv),
        ]]}
    if platform == "darwin":
        label = f"dev.lantern.{job}"
        args = "".join(f"<string>{a}</string>" for a in argv)
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict><key>Label</key><string>{label}</string>'
            f"<key>ProgramArguments</key><array>{args}</array>"
            f"<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer>"
            "<key>Minute</key><integer>0</integer></dict></dict></plist>\n"
        )
        path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        return {"files": {path: plist}, "commands": [["launchctl", "load", "-w", str(path)]]}
    unit_dir = home / ".config" / "systemd" / "user"
    service = f"[Unit]\nDescription=Lantern {job}\n\n[Service]\nType=oneshot\nExecStart={subprocess.list2cmdline(argv)}\n"
    timer = f"[Unit]\nDescription=Lantern {job} daily\n\n[Timer]\nOnCalendar=*-*-* {hour:02d}:00:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    return {
        "files": {unit_dir / f"{name}.service": service, unit_dir / f"{name}.timer": timer},
        "commands": [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{name}.timer"]],
    }


def apply(p: dict) -> None:
    for path, text in p["files"].items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for argv in p["commands"]:
        subprocess.run(argv, check=True)
