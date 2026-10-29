"""Curate: once a day, turn recent sessions into durable memory.

Raw turns are captured as they happen; this job asks a small model which of them say something that should hold
from now on (a rule or preference the user stated, a fact about their setup or projects) and stores each as a rule
or fact whose first line is its summary, so every later session loads it at start. One small call a day.
"""
import json
import re
import shutil
import subprocess
import time

from . import memory
from .paths import ensure_data_dir

MODEL = "haiku"
MAX_CHARS = 60000  # about a day of turns; older turns were curated by earlier runs
SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["kind", "summary", "detail"], "properties": {
        "kind": {"enum": ["rule", "fact"]}, "summary": {"type": "string"}, "detail": {"type": "string"}}}}}}
PROMPT = """Below are recent exchanges between a user and their coding and operations assistant. List only what
should still hold in future sessions:
- rule: something the user asked for or corrected that should change how the assistant works from now on
  ("never restart X without asking", "keep replies short", "don't use hacky workarounds")
- fact: a durable fact about the user's machines, services, accounts, people or projects
  ("Plex runs as container-plex.service", "Paperclip was shut down on purpose on Oct 27")
Not: one-off task steps, things already done and finished, guesses, or anything secret (passwords, tokens, keys).
summary: one self-contained sentence a future session can act on. detail: why, in a sentence or two.
Return an empty list if nothing qualifies.

ALREADY KNOWN (do not repeat these):
{known}

EXCHANGES:
{turns}
"""


def _ask(prompt: str) -> list[dict]:
    """Headless claude, isolated: no user settings or hooks, no MCP, no tools, no saved session."""
    cwd = ensure_data_dir() / "curate-cwd"
    cwd.mkdir(exist_ok=True)
    r = subprocess.run(["claude", "-p", "--model", MODEL, "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
                        "--setting-sources", "", "--strict-mcp-config", "--tools", "", "--no-session-persistence",
                        "--disable-slash-commands"], input=prompt, capture_output=True, text=True, cwd=cwd, timeout=600)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    out = json.loads(r.stdout)
    return (out.get("structured_output") or json.loads(out["result"]))["items"]


def _words(text: str) -> set[str]:
    return set(memory.terms(text))


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
        text = "\n\n---\n\n".join(turns)[-MAX_CHARS:]
        known = memory.rules(db, 6000) + memory.rules(db, 6000, kind="fact")
        items = ask(PROMPT.format(known="\n".join(f"- {k}" for k in known) or "(nothing yet)", turns=text))
        added = 0
        for it in items:
            summary = re.sub(r"\s+", " ", it["summary"]).strip()
            if not summary or find(summary + it["detail"]):
                continue  # never store anything that looks like a secret
            # A near-copy of something already known (most of its words) would only add noise.
            w = _words(summary)
            if any(len(w & _words(k)) >= 0.7 * max(1, len(w)) for k in known):
                continue
            memory.add(db, it["kind"], f"curate:{time.strftime('%Y-%m-%d', time.localtime(now))}:{added}",
                       f"{summary}\n\n{it['detail'].strip()}", created=now)
            known.append(summary)
            added += 1
    finally:
        db.close()
    state_path.write_text(json.dumps({"until": now}))
    return added


def available() -> bool:
    return shutil.which("claude") is not None
