"""The survey: once, after the first look, a bounded background agent reads the machine's own files (configs, units,
compose files, READMEs, dotfiles) and writes a short brief: what this machine is for, how it is set up, what is notable
or risky, and what Lantern should take on. It only reads. One process, capped turns, a time limit, and it stands down
under memory pressure. The brief becomes memory and is announced on Stage when done."""
import json
import subprocess
import sys
import time
from pathlib import Path

from . import curate, discovery, memory, resources, stage
from .paths import ensure_data_dir

MODEL = "sonnet"
MAX_TURNS = 40
TIMEOUT = 900
PROMPT = """You are surveying a machine for Lantern, a harness that will remember this for every later session. You can only
read files (Read, Glob, Grep); never run commands or change anything. Never open ~/.claude, ~/.ssh (except the host
names in config), browser profiles, mail, or anything named credentials, token, key or password; skip anything that
looks like a secret and never quote one.

What discovery already found:
{inventory}

Read what explains this machine: README files of the main projects, docker-compose and systemd unit files, ~/.ssh/config
(hosts only), shell config, the project folders' structure, package manifests. Then write a brief, in plain English, with
these headings and nothing else:
# What this machine is for
# How it is set up
(services, containers, VMs, how projects relate, where data lives; include a markdown table "component | runs on |
depends on" for the main services, and an indented text tree of how they connect; no mermaid, it does not render here)
# Existing monitoring and backups
(what already watches what and where alerts go: uptime checkers, timers, SIEM, notification services; and what the backup
jobs cover, from their scripts; a table "what | watched or backed up by | alerts to")
# Worth knowing
(at most five things the owner may not know, each verified from a file and specific, ranked: data at risk of loss first,
then secrets with a real exposure path (in a repo, a backup or a sync; say which), then services down, then drift. A
setting that is clearly deliberate, such as an explicit AutoUpdate line, is a choice, not a finding.)
# What Lantern should take on
(three to five responsibilities that nothing above already covers, each one line, saying through which existing alert
channel Lantern would tell the owner; skip anything the existing monitoring already does)
# Profile
(two or three sentences about the owner as a user of this machine, inferred from what is here; say "inferred")
Keep it under 700 words."""


def _running(data: Path) -> bool:
    try:
        st = json.loads((data / "survey.json").read_text())
    except (OSError, ValueError):
        return False
    return st.get("started", 0) > time.time() - TIMEOUT and not st.get("done")


def start(data: Path | None = None) -> bool:
    """Begin the survey in the background. Returns whether it started."""
    data = data or ensure_data_dir()
    if (data / "survey.md").exists() or _running(data) or not curate.available() or not resources.can_spawn():
        return False
    (data / "survey.json").write_text(json.dumps({"started": time.time()}))
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "lantern.py"), "job", "survey", "--data", str(data)]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    kw["creationflags" if sys.platform == "win32" else "start_new_session"] = 0x00000208 if sys.platform == "win32" else True
    subprocess.Popen(cmd, **kw)
    return True


def _progress(data: Path, started: float, steps: int, last: str) -> None:
    (data / "survey.json").write_text(json.dumps({"started": started, "steps": steps, "last": last[:120]}))


def run(data: Path | None = None) -> str:
    """The survey itself (runs detached): one headless, read-only claude session, then store the brief. Progress (files
    read so far) is written as it goes, so a session can say what the survey is doing."""
    data = data or ensure_data_dir()
    inventory = discovery.summary() or "(nothing yet)"
    started = time.time()
    proc = subprocess.Popen([curate.claude_bin(), "-p", "--model", MODEL, "--max-turns", str(MAX_TURNS), "--tools", "Read,Glob,Grep",
                             "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands",
                             "--add-dir", str(Path.home()), "--output-format", "stream-json", "--verbose",
                             PROMPT.format(inventory=inventory)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=Path.home())
    text, steps = "", 0
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if time.time() - started > TIMEOUT:
                proc.kill()
                break
            if ev.get("type") == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        steps += 1
                        inp = b.get("input") or {}
                        _progress(data, started, steps, f"{b.get('name')} {inp.get('file_path') or inp.get('pattern') or ''}")
            elif ev.get("type") == "result":
                text = ev.get("result", "") or ""
    finally:
        proc.wait(timeout=30) if proc.poll() is None else None
    from .shield import find, redact
    text = redact(text) if text and not find(text[:0]) else text
    db = memory.connect()
    try:
        if text.strip():
            (data / "survey.md").write_text(text)
            memory.add_document(db, "doc", "survey", "Lantern's survey of this machine\n\n" + text)
            prof = text.split("# Profile", 1)[-1].strip() if "# Profile" in text else ""
            if prof:
                memory.add(db, "profile", "survey", prof[:1200])
            stage.publish(db, "survey/done", "survey", "Lantern finished its survey of this machine: its understanding, what is "
                          "worth knowing and what it proposes to take on are in the session context now.", score=7, days=2)
        else:
            stage.publish(db, "survey/failed", "survey", "The background survey of this machine did not finish; it will try again "
                          "in a later session.", score=3, days=1)
    finally:
        db.close()
    (data / "survey.json").write_text(json.dumps({"started": started, "done": True, "ok": bool(text.strip()), "steps": steps}))
    return text


def status(data: Path | None = None) -> str:
    """One line on the survey for a session: running (how long, how far), done, or nothing."""
    data = data or ensure_data_dir()
    try:
        st = json.loads((data / "survey.json").read_text())
    except (OSError, ValueError):
        return ""
    if st.get("done"):
        return ""
    mins = (time.time() - st.get("started", time.time())) / 60
    if mins > TIMEOUT / 60:
        return ""
    return (f"Background survey of this machine: running for {mins:.0f} min (usually under 10), {st.get('steps', 0)} files looked at"
            + (f", now at {st['last']}" if st.get("last") else "") + ". It only reads. Say so in one line if the user asks what is happening.")


def brief(data: Path | None = None, max_chars: int = 6000) -> str:
    data = data or ensure_data_dir()
    try:
        return (data / "survey.md").read_text()[:max_chars]
    except OSError:
        return ""
