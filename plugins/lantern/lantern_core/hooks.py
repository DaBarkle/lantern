"""Claude Code hook entry points. Each reads the event JSON on stdin and may print a JSON reply."""
import json
import math
import sys
import time

from . import __version__, discovery, memory, semantic, shield, stage, transcript
from .paths import ensure_data_dir

SYSTEM_EVENTS = ("<task-notification", "<subagent-notification", "<persisted-output", "<system-reminder")
RECALL_LIMIT = 3
CAPTURE_CHARS = 2000
SEMANTIC_FLOOR = 0.45  # MiniLM cosine for an item to be a candidate
SEMANTIC_GATE = 0.5  # best cosine below this = memory has nothing on the topic (eval: absent facts top out at 0.41)
KEYWORD_SHARE = 0.5
MIN_CHARS = 40  # log lines like "User requested to proceed." carry nothing
PROFILE_CHARS = 3000
RULES_CHARS = 12000  # Hermes loaded a ~23k-char memory index every session; one line per rule is leaner
RESUME_HOURS = 48
KIND_WEIGHT = {"fact": 1.5, "note": 1.2}  # curated facts and why-notes outrank raw turn logs


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _is_system(text: str) -> bool:
    return text.lstrip().startswith(SYSTEM_EVENTS)


def _log(name: str, record: dict) -> None:
    with open(ensure_data_dir() / f"{name}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **record}) + "\n")


def session_start(payload: dict) -> dict | None:
    d = ensure_data_dir()
    lines = [f"Lantern {__version__} is active. Its data folder is {d}."]
    db = memory.connect()
    try:
        profile = memory.profile(db, PROFILE_CHARS)
        rules = memory.rules(db, RULES_CHARS)
        session = payload.get("session_id", "?")
        previous = memory.last_session(db, session, RESUME_HOURS * 3600)
        # After a compaction or resume the details of this session are no longer in view: bring back its working memory.
        own = memory.session_turns(db, session) if payload.get("source") in ("compact", "resume") else []
        attention = stage.delta(db, payload.get("session_id", "?"))  # marks it seen, so prompt 1 doesn't repeat it
    finally:
        db.close()
    if profile:
        # Who the user is matters on every turn, so it is loaded once rather than searched for.
        lines += ["", "What Lantern knows about the user:", profile]
    if rules:
        # Standing rules apply everywhere, so like the profile they are loaded, not searched for.
        lines += ["", "The user's standing rules and preferences (from past sessions):"] + [f"- {r}" for r in rules]
    caps = discovery.settings().get("capabilities", {})
    if caps:
        lines += ["", "On this machine (accepted by the user in discovery):",
                  ", ".join(sorted(c["name"] for c in caps.values()))]
    if own:
        lines += ["", "This session's working memory (from before the compaction):",
                  f"- Started with: {own[0][:400]}",
                  f"- Names in play: {', '.join(memory.identifiers(own))}"] + [f"- Recent: {t[:500]}" for t in own[-3:]]
    elif previous:
        lines += ["", "Where the user's last session left off:"] + [f"- {p['text'][:600]}" for p in previous]
    if attention:
        lines += ["", "On Lantern's Stage (open findings, most pressing first):", attention]
    return _context("SessionStart", "\n".join(lines))


def user_prompt_submit(payload: dict) -> dict | None:
    prompt = payload.get("prompt", "")
    if not prompt.strip() or _is_system(prompt):
        return None
    # The previous message carries the topic for short follow-ups ("are they all direct?").
    query = prompt + "\n" + transcript.previous_user(payload.get("transcript_path", ""))
    want = set(memory.terms(query))
    # A keyword hit must share most of the query's words; common words alone ("operator", "server") match everything.
    need = max(2, math.ceil(KEYWORD_SHARE * len(want))) if len(want) > 1 else 1
    db = memory.connect()
    try:
        keyword = [h["id"] for h in memory.search(db, query, limit=RECALL_LIMIT * 5)
                   if len(want & set(memory.terms(h["text"]))) >= need]
        found = semantic.search(ensure_data_dir(), query)
        meaning = [i for i, cos in found or [] if cos >= SEMANTIC_FLOOR]
        # With the server up, its best cosine decides whether memory has anything on this at all.
        if found is not None and (not found or found[0][1] < SEMANTIC_GATE):
            keyword, meaning = [], []
        if found is not None:  # eval: keyword-only hits with no meaning match were mostly unrelated
            meaning_ids = set(meaning)
            keyword = [i for i in keyword if i in meaning_ids]
        # Reciprocal rank fusion: items found by both searches rise to the top.
        fused: dict[int, float] = {}
        for ranked in (keyword, meaning):
            for rank, i in enumerate(ranked):
                fused[i] = fused.get(i, 0.0) + 1 / (60 + rank)
        rows = memory.get(db, list(fused))
        attention = stage.delta(db, payload.get("session_id", "?"))
    finally:
        db.close()
    # This session's turns are already in context.
    order = sorted(fused, key=lambda i: fused[i] * KIND_WEIGHT.get(rows.get(i, {}).get("kind"), 1.0), reverse=True)
    hits, seen = [], set()
    for i in order:
        h = rows.get(i)
        if not h or h["source"] == payload.get("session_id") or len(h["text"]) < MIN_CHARS:
            continue
        if h["text"][:200] in seen:  # the same note stored twice
            continue
        seen.add(h["text"][:200])
        hits.append(h)
        if len(hits) == RECALL_LIMIT:
            break
    _log("recall", {"fired": bool(hits), "hits": [h["id"] for h in hits],
                    "keyword": len(keyword), "semantic": len(meaning)})
    lines = []
    if hits:
        lines.append("Lantern recalled these past notes as possibly relevant:")
        for h in hits:
            day = time.strftime("%Y-%m-%d", time.localtime(h["created"]))
            lines.append(f"- [{day}, {h['kind']}] {h['text'][:400]}")
    if attention:
        lines += ["Lantern's Stage changed since your last turn:", attention]
    return _context("UserPromptSubmit", "\n".join(lines)) if lines else None


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


HANDLERS = {"session-start": session_start, "user-prompt-submit": user_prompt_submit, "stop": stop,
            "pre-tool-use": shield.pre_tool_use}


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
