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
        discovery.update_settings(disk_series=series)
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


def down(caps: dict) -> list[tuple[str, str, float]]:
    """Known services, containers, VMs and local servers that are not up right now."""
    return [(f"insight/down/{cid}", f"{c['name']} is not running", 5) for cid, c in caps.items()
            if not cid.startswith(("repo:", "project:")) and c.get("check") and discovery.healthy(c["check"]) is False]  # a gone folder is not "down"


def failed_units() -> list[tuple[str, str, float]]:
    """Failed systemd units, user and system scope, with the telling journal lines already read."""
    out = []
    for scope in (["--user"], []):  # the user's units and the system's (an updater or logrotate failing matters too)
        failed = discovery._run(["systemctl", *scope, "--failed", "--no-legend", "--plain"]).splitlines()
        for line in failed[:5]:
            unit = line.split()[0] if line.split() else ""
            if not unit:
                continue
            log = [l for l in discovery._run(["journalctl", *scope, "-u", unit, "-n", "30", "--no-pager", "-o", "cat"]).splitlines() if l.strip()]
            boiler = ("Started ", "Starting ", "Stopped ", "Stopping ", "Consumed ", "Scheduled restart", "Failed with result", "Main process exited")
            telling = [l for l in log if not l.startswith(boiler) and not l.startswith(unit)][-3:]
            exits = [l for l in log if "code=" in l or "status=" in l]
            cause = (f"; journal, already read by Lantern: " + " | ".join(t[:120] for t in telling) if telling else "") + (f"; {exits[-1][:120]}" if exits else "")
            if not telling and not exits:
                cause = "; the journal has no lines for it (the unit may have been created transiently or logs are rotated)"
            show = dict(l.split("=", 1) for l in discovery._run(["systemctl", *scope, "show", "-p", "Result,ExecMainStatus,InactiveEnterTimestamp", unit]).splitlines() if "=" in l)
            when = show.get("InactiveEnterTimestamp", "")
            detail = f" (result {show.get('Result', '?')}, exit status {show.get('ExecMainStatus', '?')}, at {when})" if show else ""
            note = "; exit 137 at a logout or shutdown means systemd stopped it, not a crash" if show.get("ExecMainStatus") == "137" else ""
            kind = "failed" if scope else "failed-system"
            out.append((f"insight/{kind}/{unit}", f"{unit}{'' if scope else ' (a system unit)'} has failed{cause}{detail}{note}", 5 if scope else 4))
    return out


def services(caps: dict) -> list[tuple[str, str, float]]:
    return down(caps) + failed_units()


def timers() -> list[tuple[str, str, float]]:
    """User timers that were not there at the last run: a job the user (or an installer) scheduled since. The first
    run only learns the set; a timer stays a finding for its three days on Stage, then is simply known."""
    lines = discovery._run(["systemctl", "--user", "list-timers", "--all", "--no-legend", "--plain"]).splitlines()
    now = {}
    for l in lines:
        parts = l.split()
        timer = next((p for p in parts if p.endswith(".timer")), "")
        if timer:
            now[timer] = " ".join(parts[:3]) if parts and parts[0] not in ("-", "n/a") else "no next run"
    if not now:
        return []
    s = discovery.settings()
    seen = s.get("timers_seen")
    if seen is None:
        discovery.update_settings(timers_seen=sorted(now))
        return []
    new = sorted(set(now) - set(seen))
    if new:
        discovery.update_settings(timers_seen=sorted(set(seen) | set(now)))
    return [(f"insight/timer-new/{t}", f"a new scheduled job appeared: {t} (next run {now[t]}); say so once, it may be the user's own doing", 2) for t in new]


FRESH_SECONDS = 3600
LIVE_KINDS = ("failed", "failed-system", "disk", "timer-new")  # cheap enough to re-check at every session start


def live() -> list[tuple[str, str, float]]:
    """The checks that run at every session start even when the hourly scan is fresh: a unit that failed minutes
    ago must never be reported from an hour-old cache (the flap week said "Resolved: ... now failed")."""
    return failed_units() + disk() + timers()


def run(db, now: float | None = None, force: bool = False) -> list[str]:
    """Publish every insight to Stage (replacing the last run's) and return their messages, highest first. The slow
    checks (git status across big repos, health probes) are not repeated within the hour, so a session start stays
    quick; the cheap ones (failed units, disk, timers) run live every time."""
    s = discovery.settings()
    caps = s.get("capabilities", {})
    if not caps:
        return []
    now = now or time.time()
    if not force and now - s.get("insights_at", 0) < FRESH_SECONDS:
        cached = [e for e in stage.top(db, k=100, now=now) if e["source"] == "insight" and e["key"].split("/")[1] not in LIVE_KINDS]
        found = live()
        for key, msg, score in found:
            stage.publish(db, key, "insight", msg, score=score, days=3, now=now)
        stage.resolve_source(db, "insight", {k for k, _, _ in found} | {e["key"] for e in cached})
        return [e["message"] for e in stage.top(db, k=100, now=now) if e["source"] == "insight"]
    discovery.update_settings(insights_at=now)
    found = repos(caps, now) + down(caps) + unit_secrets() + backups() + live()
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
        head = _git(rest, "log", "-1", "--format=%h %cs")
        return (f"{Path(rest).name} ({rest.replace(str(Path.home()), '~')}): now {'clean' if not dirty else f'{dirty} uncommitted'} (last commit {head or 'none'}), "
                f"{'has a remote' if remote else 'still no remote, so still unbacked unless a backup job covers it'}"
                + (", nothing unpushed" if status and "ahead" not in status[0] else ""))
    if kind in ("failed", "failed-system"):
        state = discovery._run(["systemctl", *(["--user"] if kind == "failed" else []), "is-active", rest]).strip() or "unknown"
        return f"{rest}: now {state}" + (" (its failed state was cleared; it is not shown running)" if state == "inactive" else " (running again)" if state == "active" else "")
    if kind == "down":
        return f"{rest.split(':', 1)[-1]}: now responding"
    if kind == "disk":
        return "disk: now below the warning level"
    return key


def _present(key: str) -> bool:
    """Is this finding there right now? Asked of a finding the cache calls resolved, because the watcher may have run
    minutes ago and the unit may have failed again since (cheap checks only; the rest trust the cache)."""
    kind, _, rest = key.removeprefix("insight/").partition("/")
    if kind.startswith("repo"):
        return key in {k for k, _, _ in repos({f"repo:{rest}": {"name": Path(rest).name}})}
    if kind in ("failed", "failed-system"):
        return discovery._run(["systemctl", *(["--user"] if kind == "failed" else []), "is-failed", rest]).strip() == "failed"
    return False


def discovered_since_last(db) -> str:
    """What discovery found new since the last session, from its Stage note."""
    for e in stage.top(db, k=200):
        if e["key"] == "discovery/new":
            return e["message"]
    return ""


def _subject(key: str) -> str:
    """The thing a finding is about (a repo path, a unit, a volume), without its kind."""
    rest = key.removeprefix("insight/").partition("/")[2]
    return rest.split(":", 1)[1] if ":" in rest.split("/", 1)[0] else rest  # "repo:/p" and "/p" are one subject


_KIND_WORDS = {"repo-risk": "uncommitted or unpushed work in", "repo-noremote": "no git remote for", "repo-stale": "no recent commit in",
               "failed": "failed unit", "failed-system": "failed system unit", "down": "not running:", "disk": "disk nearly full on",
               "backup": "backup job problem:", "backup-ok": "backup fine:", "secret-in-unit": "a secret written in unit file"}


def _short(key: str) -> str:
    """The one word a reply must contain to count as naming this finding: a folder name, a unit, a service."""
    kind, _, rest = key.removeprefix("insight/").partition("/")
    rest = rest.split(":", 1)[-1]
    return Path(rest).name if "/" in rest else rest


def _name(key: str) -> str:
    """A finding named in words ("failed unit x.service"), never by its internal key."""
    kind, _, rest = key.removeprefix("insight/").partition("/")
    rest = rest.split(":", 1)[-1] if kind == "down" else rest
    return f"{_KIND_WORDS.get(kind, kind)} {rest.replace(str(Path.home()), '~')}".strip()


def diff_since_last(found_keys: set[str], db=None) -> str:
    """How the findings moved since the last session, with what each resolved one looks like now; remembers them.
    Findings are matched by subject, so a repo that was committed but still has no remote is CHANGED, never one
    resolved plus one new; the counts and the lists come from the same sets."""
    s = discovery.settings()
    before = set(s.get("insights", []))
    discovery.update_settings(insights=sorted(found_keys))
    if not before:
        return ""
    old: dict[str, set[str]] = {}
    cur: dict[str, set[str]] = {}
    for k in before:
        old.setdefault(_subject(k), set()).add(k)
    for k in found_keys:
        cur.setdefault(_subject(k), set()).add(k)
    resolved = sorted(k for subj in old.keys() - cur.keys() for k in old[subj])
    back = [k for k in resolved if _present(k)]  # the cache says gone, the machine says it is back
    resolved = [k for k in resolved if k not in back]
    if back:
        discovery.update_settings(insights=sorted(found_keys | set(back)))
    new = sorted(k for subj in cur.keys() - old.keys() for k in cur[subj])
    changed = sorted(k for subj in old.keys() & cur.keys() if old[subj] != cur[subj] for k in old[subj])
    unchanged = len(old.keys() & cur.keys()) - len(changed)
    plural = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"
    counts = [f"{plural(len(resolved), 'finding')} resolved", f"{len(new)} new"]
    if changed:
        counts.append(f"{len(changed)} changed")
    if back:
        counts.append(f"{len(back)} back again")
    counts.append(f"{unchanged} unchanged")
    at = s.get("insights_at")
    discovery.update_settings(since_last={"names": sorted({_short(k) for k in new + resolved + changed + back}), "at": time.time()})
    line = "Since the last session: " + ", ".join(counts) + (f" (watches last ran at {time.strftime('%H:%M', time.localtime(at))})." if at else ".")
    if back:
        line += " Back again (cleared since last session, present again now): " + "; ".join(_why_resolved(k) for k in back) + "."
    if new:
        line += " New: " + "; ".join(_name(k) for k in new) + "."  # every one: the count and the list must agree
    if changed:
        line += " Changed: " + "; ".join(_why_resolved(k) for k in changed) + "."
    if resolved:
        line += " Resolved: " + "; ".join(_why_resolved(k) for k in resolved) + "."
    return line


def _age(stamp: str, now: float | None = None) -> str:
    """'27 h ago' from a systemd timestamp, computed here so the model never does clock arithmetic."""
    try:
        t = time.mktime(time.strptime(" ".join(stamp.split()[1:3]), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, IndexError):
        return "age unknown"
    h = ((now or time.time()) - t) / 3600
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} days ago"


def findings_touching(db, prompt: str, limit: int = 3) -> list[str]:
    """Open findings and Stage notes that mention a distinctive word from the request, so a question about Plex sees
    the Plex findings without the user asking for them."""
    words = {w for w in discovery.memory.terms(prompt) if len(w) >= 4}
    if not words:
        return []
    hits = []
    for e in stage.top(db, k=200):
        text = e["message"].lower()
        if any(w in text for w in words):
            hits.append(e["message"][:300])
    return hits[:limit]


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
            out.append((f"insight/backup-ok/{unit}", f"backup job {unit} last succeeded at {when} ({_age(when)}); next scheduled {next_run or 'unknown'}", 1))
    return out


def open_block(db) -> str:
    """Every open finding by category, so a session states counts from here and never miscounts or drops one."""
    entries = [e for e in stage.top(db, k=200) if e["source"] == "insight" and not e["key"].startswith("insight/backup-ok")]
    worth = [e["message"][:80] for e in stage.top(db, k=200) if e["key"].startswith("survey/worth/")]
    if not entries and not worth:
        return "Open findings: none."
    groups: dict[str, list[str]] = {}
    for e in entries:
        groups.setdefault(e["key"].split("/")[1], []).append(e["message"].split(" (")[0].split(" has ")[0].split(" is ")[0][:60])
    parts = [f"{len(v)} {k.replace('repo-', 'project ')}: " + ", ".join(v[:12]) for k, v in sorted(groups.items())]
    line = f"Open findings ({len(entries)}; state counts only from this line): " + (" | ".join(parts) or "none") + "."
    if worth:
        line += f" Survey risks still open ({len(worth)}): " + " | ".join(worth) + "."
    return line


def watch_summary(db) -> str:
    """What the watches currently see, in one line: counts by kind (failed units by name), backups, and the disk level."""
    entries = [e for e in stage.top(db, k=100) if e["source"] == "insight"]
    kinds = {}
    for e in entries:
        kinds[e["key"].split("/")[1]] = kinds.get(e["key"].split("/")[1], 0) + 1
    parts = [f"{n} {k.replace('repo-', 'project ')}" for k, n in sorted(kinds.items()) if k != "backup-ok"]
    failed = [e["key"].split("/", 2)[2] for e in entries if e["key"].startswith(("insight/failed/", "insight/failed-system/"))]
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
