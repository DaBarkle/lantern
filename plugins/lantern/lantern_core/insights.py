"""Things the user may not know about their own machine, worked out from what discovery found. Plain checks, no model:
each one is a fact with a reason, published to Stage so the first reply (and every later session) can say it."""
import re
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
        where = d.replace(str(Path.home()), "~")
        if dirty or ahead:
            what = " and ".join(p for p in (f"{dirty} uncommitted change(s)" if dirty else "", "unpushed commits" if ahead else "") if p)
            out.append((f"insight/repo-risk/{d}", f"{c['name']} ({where}) has {what}" + ("" if remote else " and no git remote (check whether a backup job covers the folder before calling it unbacked)"), 5))
        elif not remote and age is not None and age < STALE_DAYS:
            out.append((f"insight/repo-noremote/{d}", f"{c['name']} ({where}) is active but has no git remote (unbacked unless a backup job covers it)", 4))
        elif age is not None and age > STALE_DAYS:
            out.append((f"insight/repo-stale/{d}", f"{c['name']} ({where}) has had no commit for {int(age)} days", 2))
    return out


def _reclaimable() -> str:
    """What `podman system df` says could be freed, as one phrase, or ''."""
    text = discovery._run(["podman", "system", "df", "--format", "{{.Type}} {{.Reclaimable}}"])
    parts = [l.split(" ", 1) for l in text.splitlines() if " " in l]
    freeable = [f"{p[1].split('(')[0].strip()} of {p[0].lower()}" for p in parts if p[1].strip() and not p[1].startswith("0B")]
    return "; podman could free " + ", ".join(freeable) if freeable else ""


def disk() -> list[tuple[str, str, float]]:
    out, seen = [], set()
    for mount in (str(Path.home()), "/var"):  # on an immutable OS the root is read-only and always "full"
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            continue
        dev = (u.total, u.free // (1 << 20))
        if dev in seen:
            continue  # /home and /var are one volume on Fedora Atomic (subvolumes share size and free space): one finding
        seen.add(dev)
        pct = 100 * (u.total - u.free) / u.total if u.total else 0
        if pct >= DISK_PCT:
            out.append((f"insight/disk/{u.total}", f"the volume holding {mount} is {pct:.0f}% full ({u.free / 1e9:.0f} GB of "
                        f"{u.total / 1e9:.0f} GB left{_trend(u)}{_reclaimable()})", 6 if pct >= 92 else 4))
    return out


def _trend(u) -> str:
    """Growth per day from a daily record of used bytes, and days until full; '' until there are two days of data."""
    s = discovery.settings()
    series = s.get("disk_series", [])
    today = time.strftime("%Y-%m-%d")
    if not series or series[-1][0] != today:
        series = (series + [[today, u.total - u.free]])[-60:]
        discovery.save_settings({**discovery.settings(), "disk_series": series})
    if len(series) < 2:
        return ""
    days = max(1, (time.mktime(time.strptime(series[-1][0], "%Y-%m-%d")) - time.mktime(time.strptime(series[0][0], "%Y-%m-%d"))) / 86400)
    per_day = (series[-1][1] - series[0][1]) / days
    if per_day <= 0:
        return "; not growing lately"
    return f"; growing {per_day / 1e9:.1f} GB/day, about {int(u.free / per_day)} days to full"


SECRET_NAMES = re.compile(r"^\s*Environment=.*\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|CLAIM)[A-Z0-9_]*)=", re.M)


def unit_secrets() -> list[tuple[str, str, float]]:
    """Unit files that carry a credential value in plain text (names only; values are never read into memory)."""
    out = []
    for unit in sorted((Path.home() / ".config" / "systemd" / "user").glob("*.service"))[:200]:
        try:
            names = sorted(set(SECRET_NAMES.findall(unit.read_text(errors="replace"))))
        except OSError:
            continue
        if names:
            out.append((f"insight/secret-in-unit/{unit.name}", f"{unit.name} has {', '.join(names)} written in plain text in the unit "
                        "file (a claim token expires in minutes and is harmless; a long-lived token should move to the keychain)", 3))
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
            log = [l for l in discovery._run(["journalctl", "--user", "-u", unit, "-n", "30", "--no-pager", "-o", "cat"]).splitlines() if l.strip()]
            boiler = ("Started ", "Starting ", "Stopped ", "Stopping ", "Consumed ", "Scheduled restart", "Failed with result", "Main process exited")
            telling = [l for l in log if not l.startswith(boiler) and not l.startswith(unit)][-3:]
            exits = [l for l in log if "code=" in l or "status=" in l]
            cause = (f"; last lines: " + " | ".join(t[:120] for t in telling) if telling else "") + (f"; {exits[-1][:120]}" if exits else "")
            show = dict(l.split("=", 1) for l in discovery._run(["systemctl", "--user", "show", "-p", "Result,ExecMainStatus,InactiveEnterTimestamp", unit]).splitlines() if "=" in l)
            when = show.get("InactiveEnterTimestamp", "")
            detail = f" (result {show.get('Result', '?')}, exit status {show.get('ExecMainStatus', '?')}, at {when})" if show else ""
            note = "; exit 137 at a logout or shutdown means systemd stopped it, not a crash" if show.get("ExecMainStatus") == "137" else ""
            out.append((f"insight/failed/{unit}", f"{unit} has failed{cause}{detail}{note}", 5))
    return out


FRESH_SECONDS = 3600


def run(db, now: float | None = None, force: bool = False) -> list[str]:
    """Publish every insight to Stage (replacing the last run's) and return their messages, highest first. Checks that
    ran within the hour are not repeated: a session start must stay quick (git status across big repos takes seconds)."""
    s = discovery.settings()
    caps = s.get("capabilities", {})
    if not caps:
        return []
    now = now or time.time()
    if not force and now - s.get("insights_at", 0) < FRESH_SECONDS:
        return [e["message"] for e in stage.top(db, k=50, now=now) if e["source"] == "insight"]
    discovery.save_settings({**discovery.settings(), "insights_at": now})
    found = repos(caps, now) + disk() + services(caps) + unit_secrets() + backups()
    found.sort(key=lambda f: -f[2])
    for key, msg, score in found:
        stage.publish(db, key, "insight", msg, score=score, days=3, now=now)
    stage.resolve_source(db, "insight", {k for k, _, _ in found})
    return [m for _, m, _ in found]


def _why_resolved(key: str) -> str:
    """What a finding that no longer appears looks like now, so Lantern never just says it dropped off."""
    kind, _, rest = key.removeprefix("insight/").partition("/")
    if kind.startswith("repo"):
        status = _git(rest, "status", "--porcelain", "--branch").splitlines()
        remote = _git(rest, "remote")
        dirty = max(0, len(status) - 1)
        return (f"{Path(rest).name}: now {'clean' if not dirty else f'{dirty} uncommitted'}, "
                f"{'has a remote' if remote else 'still no remote'}" + (", nothing unpushed" if status and "ahead" not in status[0] else ""))
    if kind == "failed":
        state = discovery._run(["systemctl", "--user", "is-active", rest]).strip() or "unknown"
        return f"{rest}: now {state}"
    if kind == "down":
        return f"{rest.split(':', 1)[-1]}: now responding"
    if kind == "disk":
        return "disk: now below the warning level"
    return key


def diff_since_last(found_keys: set[str]) -> str:
    """How the findings moved since the last session, with what each resolved one looks like now; remembers them."""
    s = discovery.settings()
    before = set(s.get("insights", []))
    s["insights"] = sorted(found_keys)
    discovery.save_settings(s)
    if not before:
        return ""
    resolved, new = before - found_keys, found_keys - before
    line = (f"Since the last session: {len(resolved)} finding(s) resolved, {len(new)} new, "
            f"{len(found_keys & before)} unchanged (all checked just now).")
    if resolved:
        line += " Resolved: " + "; ".join(_why_resolved(k) for k in sorted(resolved)[:6]) + "."
    return line


def _missed(next_run: str, now: float | None = None) -> bool:
    """systemd's NextElapse is in the past only when a scheduled run did not happen."""
    try:
        t = time.mktime(time.strptime(" ".join(next_run.split()[1:3]), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, IndexError):
        return False
    return (now or time.time()) - t > 15 * 60


def backups() -> list[tuple[str, str, float]]:
    """User timers that look like backup jobs: when they last ran and whether that run succeeded."""
    out = []
    timers = [l.split()[0] for l in discovery._run(["systemctl", "--user", "list-timers", "--all", "--no-legend", "--plain"]).splitlines()
              if l.split() and any(w in l.lower() for w in ("backup", "restic", "borg", "rsync", "snapshot"))]
    for timer in timers[:5]:
        unit = timer.replace(".timer", ".service")
        show = dict(l.split("=", 1) for l in discovery._run(["systemctl", "--user", "show", "-p", "Result,ExecMainStatus,ExecMainExitTimestamp", unit]).splitlines() if "=" in l)
        tshow = dict(l.split("=", 1) for l in discovery._run(["systemctl", "--user", "show", "-p", "LastTriggerUSec,NextElapseUSecRealtime", timer]).splitlines() if "=" in l)
        when, result = show.get("ExecMainExitTimestamp", ""), show.get("Result", "")
        last_trigger, next_run = tshow.get("LastTriggerUSec", ""), tshow.get("NextElapseUSecRealtime", "")
        # Clock arithmetic is done here, not by the model: a scheduled run whose time has passed without a trigger is missed.
        missed = _missed(next_run)
        if result and result != "success":
            out.append((f"insight/backup/{unit}", f"backup job {unit} last ended with result {result} (exit {show.get('ExecMainStatus', '?')}) at {when or 'unknown time'}", 7))
        elif missed:
            out.append((f"insight/backup/{unit}", f"backup job {unit} MISSED its scheduled run ({next_run} has passed with no trigger; last trigger {last_trigger or 'unknown'})", 6))
        elif when:
            out.append((f"insight/backup-ok/{unit}", f"backup job {unit} last succeeded at {when}; next scheduled {next_run or 'unknown'}", 1))
    return out


def open_block(db) -> str:
    """Every open finding by category, so a session states counts from here and never miscounts or drops one."""
    entries = [e for e in stage.top(db, k=200) if e["source"] == "insight" and not e["key"].startswith("insight/backup-ok")]
    if not entries:
        return "Open findings: none."
    groups: dict[str, list[str]] = {}
    for e in entries:
        groups.setdefault(e["key"].split("/")[1], []).append(e["message"].split(" (")[0].split(" has ")[0].split(" is ")[0][:60])
    parts = [f"{len(v)} {k.replace('repo-', 'project ')}: " + ", ".join(v[:12]) for k, v in sorted(groups.items())]
    return f"Open findings ({len(entries)}; state counts only from this line): " + " | ".join(parts) + "."


def watch_summary(db) -> str:
    """What the watches currently see, in one line: counts by kind (failed units by name), backups, and the disk level."""
    entries = [e for e in stage.top(db, k=100) if e["source"] == "insight"]
    kinds = {}
    for e in entries:
        kinds[e["key"].split("/")[1]] = kinds.get(e["key"].split("/")[1], 0) + 1
    parts = [f"{n} {k.replace('repo-', 'project ')}" for k, n in sorted(kinds.items()) if k != "backup-ok"]
    failed = [e["key"].split("/", 2)[2] for e in entries if e["key"].startswith("insight/failed/")]
    if failed:
        parts.append("failed: " + ", ".join(failed))
    for e in entries:
        if e["key"].startswith("insight/backup"):
            parts.append(e["message"])
    try:
        u = shutil.disk_usage(str(Path.home()))
        parts.append(f"home volume {100 * (u.total - u.free) / u.total:.0f}% used, {u.free / 1e9:.0f} GB free")
    except OSError:
        pass
    return "; ".join(parts) if parts else "nothing to report"
