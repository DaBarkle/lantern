"""Lantern must never push the machine into swap. Background work checks free memory first and stands down under pressure."""
import sys

MIN_FREE_MB = 1500  # below this, no new background process; a session's hooks still run (they are small)


def available_mb() -> int | None:
    """Free memory the OS could hand out now, or None where it cannot be read cheaply."""
    if sys.platform.startswith("linux"):
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
        except (OSError, ValueError):
            return None
    return None  # shortcut: macOS and Windows have no cheap stdlib read; add psutil-free probes when a user there needs it


def can_spawn() -> bool:
    free = available_mb()
    return free is None or free >= MIN_FREE_MB
