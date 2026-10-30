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
    if sys.platform == "darwin":
        return _darwin_mb()
    if sys.platform == "win32":
        return _windows_mb()
    return None


def _darwin_mb() -> int | None:
    """vm_stat: free, inactive and speculative pages are what the kernel would hand out."""
    import re
    import subprocess
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    page = re.search(r"page size of (\d+) bytes", out)
    pages = {m.group(1): int(m.group(2)) for m in re.finditer(r"^Pages (free|inactive|speculative):\s+(\d+)", out, re.M)}
    if not page or not pages:
        return None
    return sum(pages.values()) * int(page.group(1)) // (1024 * 1024)


def _windows_mb() -> int | None:
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = Status()
    st.dwLength = ctypes.sizeof(st)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
    except (AttributeError, OSError):
        return None
    return int(st.ullAvailPhys) // (1024 * 1024)


def can_spawn() -> bool:
    free = available_mb()
    return free is None or free >= MIN_FREE_MB
