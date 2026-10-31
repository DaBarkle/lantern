"""Jobs that run by themselves. A session start checks what is due (curation once a day, the doctor once a day) and
starts it detached, so nothing depends on the OS scheduler being set up; `lantern schedule --apply` remains an
option for machines that are rarely in a session. A lock with a timestamp keeps two sessions from starting the same job.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import curate, resources
from .paths import ensure_data_dir, uv, write_atomic

DUE = {"curate": 20 * 3600, "doctor": 20 * 3600, "discover": 20 * 3600}  # a little under a day, so a daily session always triggers them
LOCK_SECONDS = 2 * 3600


def _state() -> dict:
    try:
        return json.loads((ensure_data_dir() / "jobs.json").read_text())
    except (OSError, ValueError):
        return {}


def due(now: float | None = None) -> list[str]:
    now = now or time.time()
    st = _state()
    out = []
    for job, every in DUE.items():
        if job == "curate" and not curate.available():
            continue
        if now - st.get(f"{job}_started", 0) > LOCK_SECONDS and now - st.get(f"{job}_done", 0) > every:
            out.append(job)
    return out


def mark(job: str, key: str, now: float | None = None) -> None:
    d = ensure_data_dir()
    st = _state()
    st[f"{job}_{key}"] = now or time.time()
    write_atomic(d / "jobs.json", json.dumps(st))


def start(jobs: list[str], entry: Path) -> list[str]:
    """Start each job detached and return what was started."""
    started = []
    if jobs and not resources.can_spawn():
        return started  # the machine is short of memory: today's jobs wait for a calmer session
    for job in jobs:
        mark(job, "started")
        cmd = [uv(), "run", "--no-project", "--quiet", str(entry), "job", job, "--data", str(ensure_data_dir())]
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            subprocess.Popen(cmd, **kw)
            started.append(job)
        except OSError:
            pass  # uv missing: the hook itself could not be running, so this never happens in practice
    return started
