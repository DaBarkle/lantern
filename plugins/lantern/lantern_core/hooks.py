"""Claude Code hook entry points. Each reads the event JSON on stdin and may print a JSON reply."""
import json
import sys
import time

from . import __version__, memory, semantic, transcript
from .paths import ensure_data_dir

SYSTEM_EVENTS = ("<task-notification", "<subagent-notification", "<persisted-output", "<system-reminder")
RECALL_LIMIT = 3
CAPTURE_CHARS = 2000
SEMANTIC_FLOOR = 0.45  # MiniLM cosine; Hermes calibrated 0.47 on its own prompts


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _is_system(text: str) -> bool:
    return text.lstrip().startswith(SYSTEM_EVENTS)


def _log(name: str, record: dict) -> None:
    with open(ensure_data_dir() / f"{name}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **record}) + "\n")


def session_start(payload: dict) -> dict | None:
    d = ensure_data_dir()
    return _context("SessionStart", f"Lantern {__version__} is active. Its data folder is {d}.")


def user_prompt_submit(payload: dict) -> dict | None:
    prompt = payload.get("prompt", "")
    if not prompt.strip() or _is_system(prompt):
        return None
    # The previous message carries the topic for short follow-ups ("are they all direct?").
    query = prompt + "\n" + transcript.previous_user(payload.get("transcript_path", ""))
    want = set(memory.terms(query))
    db = memory.connect()
    try:
        # Keyword hits must share 2 words with the query (or the one word a short query has).
        keyword = [h["id"] for h in memory.search(db, query, limit=RECALL_LIMIT * 5)
                   if len(want & set(memory.terms(h["text"]))) >= min(2, len(want))]
        meaning = [i for i, cos in semantic.search(ensure_data_dir(), query) if cos >= SEMANTIC_FLOOR]
        # Reciprocal rank fusion: items found by both searches rise to the top.
        fused: dict[int, float] = {}
        for ranked in (keyword, meaning):
            for rank, i in enumerate(ranked):
                fused[i] = fused.get(i, 0.0) + 1 / (60 + rank)
        rows = memory.get(db, list(fused))
    finally:
        db.close()
    # This session's turns are already in context.
    hits = [rows[i] for i in sorted(fused, key=fused.get, reverse=True)
            if i in rows and rows[i]["source"] != payload.get("session_id")][:RECALL_LIMIT]
    _log("recall", {"fired": bool(hits), "hits": [h["id"] for h in hits],
                    "keyword": len(keyword), "semantic": len(meaning)})
    if not hits:
        return None
    lines = ["Lantern recalled these past notes as possibly relevant:"]
    for h in hits:
        day = time.strftime("%Y-%m-%d", time.localtime(h["created"]))
        lines.append(f"- [{day}, {h['kind']}] {h['text'][:400]}")
    return _context("UserPromptSubmit", "\n".join(lines))


def stop(payload: dict) -> dict | None:
    user, reply = transcript.last_exchange(payload.get("transcript_path", ""))
    if not user or _is_system(user):
        return None
    text = f"User: {user}\nAssistant: {reply}"[:CAPTURE_CHARS]
    db = memory.connect()
    try:
        memory.add(db, "turn", payload.get("session_id", "?"), text)
    finally:
        db.close()
    return None


HANDLERS = {"session-start": session_start, "user-prompt-submit": user_prompt_submit, "stop": stop}


def run(name: str, stdin=sys.stdin, stdout=sys.stdout) -> int:
    handler = HANDLERS.get(name)
    if handler is None:
        print(f"lantern: unknown hook {name!r}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    reply = handler(payload)
    if reply:
        stdout.write(json.dumps(reply))
    return 0
