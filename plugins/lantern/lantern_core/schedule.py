"""Run Lantern jobs while no session is open, using the OS's own scheduler.

plan() only describes the change (files to write, commands to run); apply() makes it.
"""
import shutil
import subprocess
import sys
from pathlib import Path

from . import discovery


def _uv() -> str:
    """The scheduler runs the job outside Claude Code with a bare PATH, so uv is named by its full path. Inside a
    container (a distrobox, say) the timer still runs on the host, so it must be the host's uv, not the container's."""
    if discovery.HOST_EXEC:
        try:
            r = subprocess.run([discovery.HOST_EXEC, "sh", "-c", "command -v uv"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return shutil.which("uv") or "uv"


def _launcher(entry: Path, job: str, data: Path | None = None) -> list[str]:
    return [_uv(), "run", "--no-project", "--quiet", str(entry), "job", job] + (["--data", str(data)] if data else [])


def _host(argv: list[str]) -> list[str]:
    return [discovery.HOST_EXEC, *argv] if discovery.HOST_EXEC and argv[0] in discovery.HOST_TOOLS else argv


def linux_scheduler() -> str | None:
    """"systemd" when this user has a systemd session to talk to, else "cron" when crontab is here, else None (a container
    or a minimal box: nothing to schedule with; callers fall back to in-session checks)."""
    try:
        if subprocess.run(_host(["systemctl", "--user", "show-environment"]), capture_output=True, timeout=10).returncode == 0:
            return "systemd"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "cron" if discovery._which("crontab") else None


def _cron_plan(job: str, hour: int | None, argv: list[str], data: Path) -> dict:
    """The user's crontab with this job's line replaced (marked by a trailing comment), written to a file that
    `crontab FILE` installs: argv only, no stdin, so apply() runs it like any other command."""
    mark = f"# lantern-{job}"
    try:
        r = subprocess.run(_host(["crontab", "-l"]), capture_output=True, text=True, timeout=10)
        current = [l for l in r.stdout.splitlines() if not l.rstrip().endswith(mark)] if r.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        current = []
    when = "0 * * * *" if hour is None else f"0 {hour} * * *"
    line = f"{when} {subprocess.list2cmdline(argv)} >/dev/null 2>&1 {mark}"  # cron would otherwise mail every run's output
    path = data / "crontab"
    return {"files": {path: "\n".join(current + [line]) + "\n"}, "commands": [["crontab", str(path)]]}


def plan(job: str, hour: int | None, entry: Path, platform: str = sys.platform, home: Path = Path.home(),
         data: Path | None = None, scheduler: str | None = None) -> dict:
    """Daily job at `hour`:00 local time, or hourly when `hour` is None. Returns {"files": {path: text}, "commands": [[argv], ...]}.
    On Linux the scheduler is probed unless given: systemd user timers where this user has a session, else cron."""
    if hour is not None and not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")
    argv = _launcher(entry, job, data)
    name = f"lantern-{job}"
    every = "hourly" if hour is None else "daily"
    if platform not in ("win32", "darwin"):
        scheduler = scheduler or linux_scheduler()
        if scheduler == "cron":
            return _cron_plan(job, hour, argv, data or home / ".local" / "share" / "lantern")
        if scheduler is None:
            raise OSError("no scheduler on this machine: no systemd user session and no crontab")
    if platform == "win32":
        return {"files": {}, "commands": [[
            "schtasks", "/Create", "/F", "/SC", *(["HOURLY"] if hour is None else ["DAILY", "/ST", f"{hour:02d}:00"]),
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
            + ("<key>StartInterval</key><integer>3600</integer>" if hour is None else
               f"<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>0</integer></dict>")
            + "</dict></plist>\n"
        )
        path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        return {"files": {path: plist}, "commands": [["launchctl", "load", "-w", str(path)]]}
    unit_dir = home / ".config" / "systemd" / "user"
    service = f"[Unit]\nDescription=Lantern {job}\n\n[Service]\nType=oneshot\nExecStart={subprocess.list2cmdline(argv)}\n"
    on = "hourly" if hour is None else f"*-*-* {hour:02d}:00:00"
    timer = f"[Unit]\nDescription=Lantern {job} {every}\n\n[Timer]\nOnCalendar={on}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    return {
        "files": {unit_dir / f"{name}.service": service, unit_dir / f"{name}.timer": timer},
        "commands": [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{name}.timer"]],
    }


def apply(p: dict) -> None:
    """Write the files and run the commands; raises CalledProcessError or OSError when this box has no scheduler to
    talk to (a container without a user systemd, a locked-down host), so callers can fall back to in-session checks."""
    for path, text in p["files"].items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for argv in p["commands"]:
        subprocess.run(_host(argv), check=True)  # the container's systemctl or crontab cannot reach the host's; ask the host
