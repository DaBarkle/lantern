"""Things the user may not know about their own machine, worked out from what discovery found. Plain checks, no model:
each one is a fact with a reason, published to Stage so the first reply (and every later session) can say it."""
import shutil
import subprocess
import time
from pathlib import Path

from . import discovery, stage

STALE_DAYS = 120
DISK_PCT = 85


def _git(d: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def repos(caps: dict, now: float | None = None) -> list[tuple[str, str, float]]:
    """(key, message, score) for projects with work at risk or long untouched."""
    now = now or time.time()
    out = []
    for cid, c in caps.items():
        if not cid.startswith("repo:"):
            continue
        d = cid[5:]
        if not Path(d, ".git").exists():
            continue
        status = _git(d, "status", "--porcelain", "--branch").splitlines()
        dirty = max(0, len(status) - 1)
        ahead = "ahead" in (status[0] if status else "")
        remote = _git(d, "remote")
        last = _git(d, "log", "-1", "--format=%ct")
        age = (now - int(last)) / 86400 if last.isdigit() else None
        if dirty or ahead:
            what = " and ".join(p for p in (f"{dirty} uncommitted change(s)" if dirty else "", "unpushed commits" if ahead else "") if p)
            out.append((f"insight/repo-risk/{d}", f"{c['name']} has {what}" + ("" if remote else " and no remote, so nothing backs it up"), 5))
        elif not remote and age is not None and age < STALE_DAYS:
            out.append((f"insight/repo-noremote/{d}", f"{c['name']} is active but has no remote: nothing backs it up", 4))
        elif age is not None and age > STALE_DAYS:
            out.append((f"insight/repo-stale/{d}", f"{c['name']} has had no commit for {int(age)} days", 2))
    return out


def disk() -> list[tuple[str, str, float]]:
    out = []
    for mount in {str(Path.home()), "/var"}:  # on an immutable OS the root is read-only and always "full"
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            continue
        pct = 100 * (u.total - u.free) / u.total if u.total else 0
        if pct >= DISK_PCT:
            out.append((f"insight/disk/{mount}", f"{mount} is {pct:.0f}% full ({u.free / 1e9:.0f} GB left)", 6))
    return out


def services(caps: dict) -> list[tuple[str, str, float]]:
    """Known services, containers, VMs and local servers that are not up right now."""
    out = []
    for cid, c in caps.items():
        if c.get("check") and discovery.healthy(c["check"]) is False:
            out.append((f"insight/down/{cid}", f"{c['name']} is not running", 5))
    failed = discovery._run(["systemctl", "--user", "--failed", "--no-legend", "--plain"]).splitlines()
    for line in failed[:5]:
        unit = line.split()[0] if line.split() else ""
        if unit:
            out.append((f"insight/failed/{unit}", f"{unit} has failed (systemd reports it)", 5))
    return out


def run(db, now: float | None = None) -> list[str]:
    """Publish every insight to Stage (replacing the last run's) and return their messages, highest first."""
    caps = discovery.settings().get("capabilities", {})
    found = repos(caps, now) + disk() + services(caps)
    found.sort(key=lambda f: -f[2])
    for key, msg, score in found:
        stage.publish(db, key, "insight", msg, score=score, days=3, now=now)
    stage.resolve_source(db, "insight", {k for k, _, _ in found})
    return [m for _, m, _ in found]
