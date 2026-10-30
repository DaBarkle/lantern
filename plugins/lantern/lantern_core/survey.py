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
read files (Read, Glob, Grep); never run commands or change anything. Skip anything that looks like a secret or a key.

What discovery already found:
{inventory}

Read what explains this machine: README files of the main projects, docker-compose and systemd unit files, ~/.ssh/config
(hosts only), shell config, the project folders' structure, package manifests. Then write a brief, in plain English, with
these headings and nothing else:
# What this machine is for
# How it is set up
(services, containers, VMs, how projects relate, where data lives; include a mermaid flowchart of the main services and
what depends on what, if there are at least three)
# Worth knowing
(things the owner may not know or has forgotten: stale or unbacked projects, odd configs, duplicated tools, risks; each
one specific, with the file or folder that shows it)
# What Lantern should take on
(three to six concrete responsibilities, each one line: watch X daily, flag Y, remind about Z)
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


def run(data: Path | None = None) -> str:
    """The survey itself (runs detached): one headless, read-only claude session, then store the brief."""
    data = data or ensure_data_dir()
    inventory = discovery.summary() or "(nothing yet)"
    cwd = data / "survey-cwd"
    cwd.mkdir(exist_ok=True)
    r = subprocess.run([curate.claude_bin(), "-p", "--model", MODEL, "--max-turns", str(MAX_TURNS), "--tools", "Read,Glob,Grep",
                        "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands",
                        "--add-dir", str(Path.home()), "--output-format", "json", PROMPT.format(inventory=inventory)],
                       capture_output=True, text=True, cwd=Path.home(), timeout=TIMEOUT)
    text = ""
    if r.returncode == 0:
        try:
            text = json.loads(r.stdout).get("result", "")
        except ValueError:
            text = ""
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
    (data / "survey.json").write_text(json.dumps({"started": time.time(), "done": True, "ok": bool(text.strip())}))
    return text


def brief(data: Path | None = None, max_chars: int = 6000) -> str:
    data = data or ensure_data_dir()
    try:
        return (data / "survey.md").read_text()[:max_chars]
    except OSError:
        return ""
