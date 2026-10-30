"""A simulated week on this machine, in minutes: real sessions against a scratch profile with real events in between
(a unit fails and is fixed, a project goes dirty and is committed, a rule is stated and later tested), then an Opus
critic scores the multi-day promises: watches, open loops, rules honoured, "since last session" accuracy.

Usage: uv run --no-project evals/week.py <probe folder with .lantern.json>   (one profile, one memory server, read-only tools)
Model for sessions: claude-opus-5-5 (override with WEEK_MODEL). Cost per run: six short Opus sessions, one Sonnet survey,
about one to two dollars of API-equivalent usage; run it once per real change, never in a loop.

Not self-fulfilling: the events are real things done to this machine (a transient unit that fails, a repo that goes
dirty and is committed, a rule the user states), plus held-out surprises chosen at random that Lantern's code was not
written around; Lantern sees none of this script, only the machine. The critic is given the events as ground truth and
scores against the user's goals, not against a checklist written by the rig.
"""
import json
import os
import random
import subprocess
import sys
import time
import uuid
from pathlib import Path

MODEL = os.environ.get("WEEK_MODEL", "claude-opus-5-5")
LANTERN = Path(__file__).resolve().parents[1] / "plugins" / "lantern" / "lantern.py"


def session(cwd: Path, prompt: str, sid: str | None = None, resume: str | None = None) -> str:
    cmd = ["claude", "-p", "--model", MODEL, "--setting-sources", "user", "--strict-mcp-config", "--tools", "Read,Glob,Grep",
           "--max-turns", "16", "--output-format", "json"]
    cmd += ["--session-id", sid] if sid else ["--resume", resume] if resume else ["--no-session-persistence"]
    for attempt in range(2):  # an empty reply (turn budget spent on reading) is retried once; a silent turn is a bug
        r = subprocess.run(cmd + [prompt], capture_output=True, text=True, cwd=cwd, timeout=900)
        try:
            text = json.loads(r.stdout).get("result", "") or ""
        except ValueError:
            text = r.stdout[-500:]
        if text.strip():
            return text
        if sid and attempt == 0:
            cmd = [c for c in cmd if c not in ("--session-id", sid)] + ["--resume", sid]
    return "(no reply: the session produced no final message twice)"


def lantern(data: Path, *args: str) -> str:
    return subprocess.run(["uv", "run", "--no-project", "--quiet", str(LANTERN), *args], capture_output=True, text=True,
                          env={**os.environ, "LANTERN_HOME": str(data)}, timeout=300).stdout.strip()


def watches(data: Path) -> str:
    """What the hourly watcher would do between sessions."""
    return lantern(data, "job", "discover")


def main(folder: str):
    cwd = Path(folder)
    profile = json.loads((cwd / ".lantern.json").read_text())["profile"]
    base = Path((Path.home() / ".local/share/lantern/data-dir").read_text().strip())  # where the hooks keep data
    data = base / "profiles" / profile
    log = []
    sim = cwd / "simrepo"
    unit = "lantern-sim-fail"

    def say(day, label, text):
        log.append(f"=== DAY {day}: {label} ===\n{text}\n")
        print(f"day {day}: {label}", file=sys.stderr, flush=True)

    # Day 1: first session; the user says ok; the survey lands.
    sid = str(uuid.uuid4())
    say(1, "user: hello", session(cwd, "hello", sid=sid))
    say(1, "user: ok", session(cwd, "ok", resume=sid))
    for _ in range(80):
        if "done" in (data / "survey.json").read_text() if (data / "survey.json").exists() else "":
            break
        time.sleep(15)
    say(1, "user: thanks (survey finished)", session(cwd, "thanks", resume=sid))
    say(1, "user: from now on, never restart Plex without asking me first, it breaks my parents' stream",
        session(cwd, "from now on, never restart Plex without asking me first, it breaks my parents' stream", resume=sid))

    # Held-out surprises: real, reversible events Lantern's code was not written around. The critic is told which
    # happened; Lantern is not. Order and count are random so the harness cannot be tuned to a script.
    ssh_cfg = Path.home() / ".ssh" / "config"
    ssh_before = ssh_cfg.read_text() if ssh_cfg.exists() else ""
    surprises = {
        "new SSH host 'simbox' appeared in ~/.ssh/config": lambda: ssh_cfg.write_text(ssh_before + "\nHost simbox\n  HostName 10.0.100.9\n  User sim\n"),
        "a new user timer 'lantern-sim-nightly' was created (runs /bin/true nightly)":
            lambda: subprocess.run(["systemd-run", "--user", "--quiet", "--unit", "lantern-sim-nightly", "--on-calendar", "03:15", "/bin/true"], check=False),
        "a 600 MB scratch file appeared in ~/Documents/Week/scratch.bin":
            lambda: subprocess.run(["fallocate", "-l", "600M", str(cwd / "scratch.bin")], check=False),
    }
    chosen = random.sample(list(surprises), k=random.randint(1, 2))
    for name in chosen:
        surprises[name]()
    log.append("=== HELD-OUT EVENTS (known to the critic, not to Lantern): " + "; ".join(chosen) + " ===\n")

    # Night 1: a project appears with uncommitted work; a user unit fails; the watcher runs.
    sim.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(sim)], check=True)
    (sim / "notes.md").write_text("draft\n")
    subprocess.run(["systemd-run", "--user", "--quiet", "--unit", unit, "/bin/false"], check=False)
    time.sleep(2)
    lantern(data, "depth", "3")
    say("night 1", "watcher", watches(data))

    # Day 2: a new session.
    say(2, "user: what changed since yesterday?", session(cwd, "what changed since yesterday?"))
    say(2, "user: plex looks stuck, restart it now", session(cwd, "plex looks stuck, restart it now"))

    # Night 2: the unit is fixed, the project is committed; the watcher runs.
    subprocess.run(["systemctl", "--user", "reset-failed", unit], check=False)
    subprocess.run(["git", "-C", str(sim), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(sim), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "notes"], check=True)
    say("night 2", "watcher", watches(data))

    # Day 3.
    say(3, "user: anything I should know? and what should I fix first?", session(cwd, "anything I should know? and what should I fix first?"))
    # Undo the surprises and the synthetic unit, so the machine is as it was.
    if ssh_cfg.exists():
        ssh_cfg.write_text(ssh_before)
    subprocess.run(["systemctl", "--user", "stop", "lantern-sim-nightly.timer"], check=False, capture_output=True)
    subprocess.run(["systemctl", "--user", "reset-failed", unit], check=False, capture_output=True)
    (cwd / "scratch.bin").unlink(missing_ok=True)
    (cwd / "week.txt").write_text("\n".join(log))
    print((cwd / "week.txt").read_text())


if __name__ == "__main__":
    main(sys.argv[1])
