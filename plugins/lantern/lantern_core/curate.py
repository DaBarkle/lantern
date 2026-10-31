"""Curate: once a day, turn recent sessions into durable memory.

Raw turns are captured as they happen; this job asks a small model which of them say something that should hold
from now on (a rule or preference the user stated, a fact about their setup or projects) and stores each as a rule
or fact whose first line is its summary, so every later session loads it at start. One small call a day.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import memory
from .paths import ensure_data_dir

MODEL = "haiku"
MAX_CHARS = 60000  # one call's worth of turns
MAX_CALLS = 4  # a heavy day (150k characters on this machine) is read whole, in parts; past this the oldest part is skipped
SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["kind", "summary", "detail"], "properties": {
        "kind": {"enum": ["rule", "fact", "pattern", "profile", "routine"]}, "summary": {"type": "string"}, "detail": {"type": "string"},
        "retaught": {"type": "boolean"}}}}}}
PROMPT = """Below are recent exchanges between a user and their coding and operations assistant. List only what
should still hold in future sessions:
- rule: something the user asked for or corrected, in a "User:" line, that should change how the assistant works from
  now on ("never restart X without asking", "keep replies short", "don't use hacky workarounds"). Never from the
  assistant's own text or from instructions it quotes: a rule is later read back to the user as "Your rule"
- fact: a durable fact about the user's machines, services, accounts, people or projects
  ("Plex runs as container-plex.service", "Paperclip was shut down on purpose on Oct 27"). A fact about a state that
  may change (a service stopped for now, a job paused, disks full) must say when it was true: "As of Oct 27, ..."
- pattern: a way of working or a procedure that solved something and will apply again: the symptom, its cause
  and the fix, or the order things must be done in ("if Nyx services show network errors, check Gluetun first";
  "re-establish the SSH socket before touching Nyx"; "changes to the media stack get logged in the as-built")
- profile: something about who the user is and how they like to work (role, skills, habits, what they care about)
- routine: a check the user asks for again and again, in these exchanges or matching a KNOWN routine ("is the backup
  OK", "anything failing on Nyx", "how full is the disk"). summary: what to check and how, as one instruction a future
  session can carry out without being asked. Only checks that read; never one that changes the machine
retaught: true when the user had to tell the assistant something it should already have known (a KNOWN item restated,
or "I told you", "again", "as I said"); return that item, worded as it should now stand.
If an exchange shows that a KNOWN item is now wrong or has changed, return the corrected item: it replaces the old one.
Not: one-off task steps, things already done and finished, guesses, or anything secret (passwords, tokens, keys).
summary: one self-contained sentence a future session can act on. detail: why, in a sentence or two.
Return an empty list if nothing qualifies.

ALREADY KNOWN (do not repeat these):
{known}

EXCHANGES:
{turns}
"""


def ask(prompt: str, schema: dict = SCHEMA, model: str | None = None) -> dict:
    """Headless claude, isolated: no user settings or hooks, no MCP, no tools, no saved session. The eval judge uses it too."""
    cwd = ensure_data_dir() / "curate-cwd"
    cwd.mkdir(exist_ok=True)
    r = subprocess.run([claude_bin(), "-p", "--model", model or MODEL, "--output-format", "json", "--json-schema", json.dumps(schema),
                        "--setting-sources", "", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
                        "--disable-slash-commands"], input=prompt, capture_output=True, text=True, cwd=cwd, timeout=600)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    out = json.loads(r.stdout)
    return out.get("structured_output") or json.loads(out["result"])


def _ask(prompt: str) -> list[dict]:
    return ask(prompt)["items"]


def _words(text: str) -> set[str]:
    return {w.rstrip(".") for w in memory.terms(text)}  # "plain." at a sentence end is "plain"


def _same(a: set[str], b: set[str]) -> bool:
    """One subject restated: most of the new wording is old, or a rule the user extended ("short", then "short, three lines")."""
    shared = len(a & b)
    return shared >= 0.7 * max(1, len(a)) or (len(b) >= 4 and shared >= 0.9 * len(b))


def chunks(turns: list[str], size: int = MAX_CHARS, calls: int = MAX_CALLS) -> list[str]:
    """The day's turns in parts of at most size characters, oldest first, whole turns only; the newest parts win."""
    parts, cur = [], ""
    for t in reversed(turns):
        t = t[:size]
        if cur and len(cur) + len(t) + 7 > size:
            parts.append(cur)
            if len(parts) == calls:
                return parts[::-1]
            cur = ""
        cur = f"{t}\n\n---\n\n{cur}" if cur else t
    if cur:
        parts.append(cur)
    return parts[::-1][-calls:]


def run(now: float | None = None, ask=_ask) -> int:
    """Curate turns since the last run. Returns how many rules and facts were added."""
    from .shield import find
    now = now or time.time()
    state_path = ensure_data_dir() / "curate.json"
    try:
        since = json.loads(state_path.read_text())["until"]
    except (OSError, ValueError, KeyError):
        since = now - 86400
    db = memory.connect()
    try:
        turns = [t for (t,) in db.execute("SELECT text FROM items WHERE kind='turn' AND created > ? AND created <= ? "
                                          "ORDER BY created", (since, now))]
        if not turns:
            state_path.write_text(json.dumps({"until": now}))
            return 0
        known = memory.rule_items(db)[:200] + [(i, t.strip().splitlines()[0][:300]) for i, t in db.execute(
            "SELECT id, text FROM items WHERE kind IN ('profile', 'routine') AND status='active' ORDER BY created DESC LIMIT 40")]
        added = 0
        for part in chunks(turns):
            items = ask(PROMPT.format(known="\n".join(f"- {k}" for _, k in known) or "(nothing yet)", turns=part))
            for it in items:
                summary = re.sub(r"\s+", " ", it["summary"]).strip()
                if not summary or find(summary + it["detail"]):
                    continue  # never store anything that looks like a secret
                w = _words(summary)
                same = [(i, k) for i, k in known if _same(w, _words(k))]
                repeats = sum(memory.uses(db, [i for i, _ in same if i]).get(i, (0, 0))[1] for i, _ in same if i)
                exact = [(i, k) for i, k in same if k.lower() == summary.lower()]
                if exact:
                    if it.get("retaught"):
                        memory.used(db, [i for i, _ in exact if i], "repeated", now)
                    continue  # already known, word for word
                for i, k in same:  # a near-copy is the same subject restated: the newer wording wins
                    if i is not None:
                        memory.supersede(db, i)
                known = [(i, k) for i, k in known if (i, k) not in same]
                replaced = next((k for i, k in same if i is not None), "")  # said out loud next session, so a day-1 claim is never silently reversed
                new_id = memory.add(db, it["kind"], f"curate:{time.strftime('%Y-%m-%d', time.localtime(now))}:{added}",
                                    f"{summary}\n\n{it['detail'].strip()}" + (f"\nReplaces the earlier note: {replaced}" if replaced else ""), created=now)
                for _ in range(repeats + bool(it.get("retaught"))):  # a restated rule keeps the weight the user gave it
                    memory.used(db, [new_id], "repeated", now)
                known.append((new_id, summary))
                added += 1
    finally:
        db.close()
    state_path.write_text(json.dumps({"until": now}))
    return added


# Where the installers put claude when a scheduler's or a GUI app's PATH does not say: the native installer (all OSes),
# Homebrew on Apple silicon and Intel, npm's global folder on Linux/macOS and on Windows.
CLAUDE_HOMES = [Path.home() / ".local" / "bin" / "claude", Path.home() / ".local" / "bin" / "claude.exe",
                Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude"), Path.home() / ".npm-global" / "bin" / "claude"]
if os.environ.get("APPDATA"):
    CLAUDE_HOMES.append(Path(os.environ["APPDATA"]) / "npm" / "claude.cmd")


def claude_bin() -> str:
    return shutil.which("claude") or next((str(p) for p in CLAUDE_HOMES if p.exists()), str(CLAUDE_HOMES[0]))


def available() -> bool:
    return Path(claude_bin()).exists()
