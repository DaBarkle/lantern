"""Claude Code hook entry points. Each reads the event JSON on stdin and may print a JSON reply."""
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, discovery, jobs, memory, semantic, shield, stage, transcript
from .paths import ensure_data_dir

SYSTEM_EVENTS = ("<task-notification", "<subagent-notification", "<persisted-output", "<system-reminder")
RECALL_LIMIT = 3
CAPTURE_CHARS = 1200  # per half: a pasted log must not crowd out the reply
SEMANTIC_FLOOR = 0.45  # MiniLM cosine for an item to be a candidate
SEMANTIC_GATE = 0.5  # best cosine below this = memory has nothing on the topic (eval: absent facts top out at 0.41)
KEYWORD_SHARE = 0.5
MIN_CHARS = 40  # log lines like "User requested to proceed." carry nothing
PROFILE_CHARS = 3000
RULES_CHARS = 12000
INDEX_CHARS = 20000  # one short line per curated note, so a session knows what memory holds before it asks
TOP_CHARS = 1500
TITLE_LIMIT = 7  # after the full notes, further matches as one-line titles  # Hermes loaded a ~23k-char memory index every session; one line per rule is leaner
RESUME_HOURS = 48
PROJECT_RESUME_DAYS = 14  # a session in the same folder is worth showing for longer than one anywhere
AGENT_CHARS = 1500
SHORT_PROMPT_TERMS = 8  # below this, the session's own names join the recall query
# A correction holds from the moment it is said, not from tomorrow's curation.
# Only openings that state a standing preference. "Stop the container" or "make sure you restart it" are tasks for now.
CORRECTION = re.compile(r"^\s*(?:please\s+)?(?:do not|don't|dont|never|always|from now on|in future|going forward|"
                        r"stop (?:doing|using|making|trying|asking|adding|suggesting)|i(?:'ve| have)? told you|i said|i asked you|"
                        r"remember that)\b", re.I)
KIND_WEIGHT = {"fact": 1.5, "pattern": 1.5, "note": 1.2}  # curated facts and why-notes outrank raw turn logs
CURATED = {"fact", "rule", "pattern", "doc"}
CURATED_MIN_TERMS = 9


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _is_system(text: str) -> bool:
    return text.lstrip().startswith(SYSTEM_EVENTS)


LOG_BYTES = 1_000_000  # a log is a window on recent behaviour, not an archive: keep the newest part


def _log(name: str, record: dict) -> None:
    path = ensure_data_dir() / f"{name}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), **record}) + "\n")
    try:
        if path.stat().st_size > LOG_BYTES:
            lines = path.read_bytes()[-LOG_BYTES // 2:].split(b"\n", 1)[-1]
            path.write_bytes(lines)
    except OSError:
        pass


def session_start(payload: dict) -> dict | None:
    d = ensure_data_dir()
    if payload.get("source") not in ("compact", "resume"):  # learning and health run by themselves, once a day
        jobs.start(jobs.due(), Path(__file__).resolve().parents[1] / "lantern.py")
    lines = [f"Lantern {__version__} is active. Its data folder is {d}."]
    if not discovery.settings_path().exists():  # first run: the session itself is the onboarding
        lines += ["This is Lantern's first session on this machine. Answer the user's message first, as normal. Then, at the end "
                  "of that first reply only, add one line offering the two-minute setup (/lantern:setup)."]
    db = memory.connect()
    try:
        profile = memory.profile(db, PROFILE_CHARS)
        rules = memory.rules(db, RULES_CHARS)
        index = memory.rules(db, INDEX_CHARS, kind="fact", dated=True)
        patterns = memory.rules(db, INDEX_CHARS // 2, kind="pattern", dated=True)
        session = payload.get("session_id", "?")
        cwd = payload.get("cwd") or ""
        previous = memory.ledger_last(db, session, PROJECT_RESUME_DAYS * 86400, cwd=cwd) if cwd else None
        if previous and previous["cwd"] != cwd and time.time() - previous["updated"] > RESUME_HOURS * 3600:
            previous = None  # another folder's session only counts while it is fresh
        if not previous:
            previous = memory.ledger_last(db, session, RESUME_HOURS * 3600)
        tail = memory.last_session(db, session, RESUME_HOURS * 3600)
        # After a compaction or resume the details of this session are no longer in view: bring back its working memory.
        own = memory.session_turns(db, session) if payload.get("source") in ("compact", "resume") else []
        own_ledger = memory.ledger_get(db, session) if own else None
        git_state(db, cwd)
        attention = stage.delta(db, payload.get("session_id", "?"))  # marks it seen, so prompt 1 doesn't repeat it
    finally:
        db.close()
    if profile:
        # Who the user is matters on every turn, so it is loaded once rather than searched for.
        lines += ["", "What Lantern knows about the user:", profile]
    if rules:
        # Standing rules apply everywhere, so like the profile they are loaded, not searched for.
        lines += ["", "The user's standing rules and preferences (from past sessions):"] + [f"- {r}" for r in rules]
    if patterns:
        lines += ["", "How the user's systems are worked on (patterns learned from past sessions):"] + [f"- {p}" for p in patterns]
    if index:
        lines += ["", "Notes Lantern holds, newest first (one line each, dated; recall brings in detail when a prompt matches; "
                  "an older note may have been overtaken by events):"] + [f"- {i}" for i in index]
    caps = discovery.settings().get("capabilities", {})
    if caps:
        lines += ["", "On this machine (accepted by the user in discovery):",
                  ", ".join(sorted(c["name"] for c in caps.values()))]
    if own:
        lines += ["", "This session's working memory (from before the compaction):",
                  f"- Started with: {own[0][:400]}",
                  f"- Names in play: {', '.join(memory.identifiers(own))}"]
        lines += memory.ledger_lines(own_ledger)[2:] if own_ledger else []
        lines += [f"- Recent: {t[:500]}" for t in own[-3:]]
    elif previous:
        where = "in this folder" if previous["cwd"] == cwd else f"in {previous['cwd']}"
        lines += ["", f"Where the user's last session {where} left off ({time.strftime('%Y-%m-%d', time.localtime(previous['updated']))}):"]
        lines += memory.ledger_lines(previous)
        if tail:
            lines += [f"- Last exchange: {tail[-1]['text'][:600]}"]
    elif tail:
        lines += ["", "Where the user's last session left off:"] + [f"- {p['text'][:600]}" for p in tail]
    if attention:
        lines += ["", "On Lantern's Stage (open findings, most pressing first):", attention]
    return _context("SessionStart", "\n".join(lines))


def user_prompt_submit(payload: dict) -> dict | None:
    prompt = payload.get("prompt", "")
    if not prompt.strip() or _is_system(prompt):
        return None
    session = payload.get("session_id", "?")
    query = prompt
    want = set(memory.terms(query))
    if len(want) < SHORT_PROMPT_TERMS:  # "are they all direct?": the previous message carries the topic
        query += "\n" + transcript.previous_user(payload.get("transcript_path", ""))
        want = set(memory.terms(query))
    noted = None
    # A keyword hit must share most of the query's words; common words alone ("operator", "server") match everything.
    need = max(2, math.ceil(KEYWORD_SHARE * len(want))) if len(want) > 1 else 1
    db = memory.connect()
    try:
        if len(want) < SHORT_PROMPT_TERMS:  # "is it still failing?": the session's own names say what "it" is
            query += "\n" + " ".join(memory.identifiers(memory.session_turns(db, session), limit=10))
            want = set(memory.terms(query))
        noted = note_correction(db, session, prompt)
        kw_hits = [h for h in memory.search(db, query, limit=(RECALL_LIMIT + TITLE_LIMIT) * 3)
                   if len(want & set(memory.terms(h["text"]))) >= need]
        keyword = [h["id"] for h in kw_hits]
        found = semantic.search(ensure_data_dir(), query)
        meaning = [i for i, cos in found or [] if cos >= SEMANTIC_FLOOR]
        if found is not None:
            # With the server up, its best cosine decides whether memory has anything on this at all, and keyword-only
            # hits must also match on meaning (eval: those were mostly unrelated turn logs). Curated notes and documents
            # that share most of the prompt's words are kept either way: they are written to be precise (eval: an
            # as-built section on quality profiles scored 0.47 and was dropped).
            # Short questions ("the operator's shoe size?") share two words with almost anything, so only longer
            # prompts can vouch for a curated match on words alone.
            curated = [h["id"] for h in kw_hits if h["kind"] in CURATED] if len(want) >= CURATED_MIN_TERMS else []
            if not found or found[0][1] < SEMANTIC_GATE:
                meaning = []
            keyword = [i for i in keyword if i in set(meaning)] + [i for i in curated if i not in set(meaning)]
        # Reciprocal rank fusion: items found by both searches rise to the top.
        fused: dict[int, float] = {}
        for ranked in (keyword, meaning):
            for rank, i in enumerate(ranked):
                fused[i] = fused.get(i, 0.0) + 1 / (60 + rank)
        rows = memory.get(db, list(fused))
        attention = stage.delta(db, payload.get("session_id", "?"))
    finally:
        db.close()
    # This session's turns are already in context. Newer items on the same topic outrank older ones (stale state was
    # the main source of wrong statements about the user's systems), with the same gentle half-life as keyword search.
    now = time.time()

    def rank(i: int) -> float:
        row = rows.get(i, {})
        age = max(0.0, (now - row.get("created", now)) / 86400)
        return fused[i] * KIND_WEIGHT.get(row.get("kind"), 1.0) * (0.5 + 0.5 * math.exp(-age * math.log(2) / memory.HALF_LIFE_DAYS))

    order = sorted(fused, key=rank, reverse=True)
    hits, seen = [], set()
    for i in order:
        h = rows.get(i)
        if not h or h["source"] in (session, f"said:{session}") or len(h["text"]) < MIN_CHARS:
            continue
        if h["text"][:200] in seen:  # the same note stored twice
            continue
        seen.add(h["text"][:200])
        hits.append(h)
        if len(hits) == RECALL_LIMIT + TITLE_LIMIT:
            break
    hits, titles = hits[:RECALL_LIMIT], hits[RECALL_LIMIT:]
    _log("recall", {"fired": bool(hits), "hits": [h["id"] for h in hits],
                    "keyword": len(keyword), "semantic": len(meaning)})
    lines = []
    if hits:
        lines.append("Lantern recalled these past notes as possibly relevant:")
        for n, h in enumerate(hits):
            day = time.strftime("%Y-%m-%d", time.localtime(h["created"]))
            # The best match is shown at length: a note's fix or root cause often comes after its symptom.
            lines.append(f"- [{day}, {h['kind']}] {h['text'][:TOP_CHARS if n == 0 else 400]}")
        if titles:
            lines.append("Also possibly relevant:")
            lines += [f"- [{time.strftime('%Y-%m-%d', time.localtime(h['created']))}] {h['text'].strip().splitlines()[0][:160]}"
                      for h in titles]
    if attention:
        lines += ["Lantern's Stage changed since your last turn:", attention]
    if noted:
        lines += ["Lantern stored this message as a standing rule for future sessions (the user can drop it with /lantern:forget)."]
    reply = _context("UserPromptSubmit", "\n".join(lines)) if lines else {}
    if noted:
        reply["systemMessage"] = "Lantern noted a standing rule from your message. /lantern:forget removes it."
    return reply or None


def note_correction(db, session: str, prompt: str) -> int | None:
    """A correction ("don't X", "always Y") becomes a rule at once; curation merges or rewrites it tonight."""
    text = prompt.strip()
    if not (15 <= len(text) <= 400) or "?" in text or not CORRECTION.match(text) or shield.find(text):
        return None
    words = set(memory.terms(text))
    for _, line in memory.rule_items(db):
        if len(words & set(memory.terms(line))) >= 0.7 * max(1, len(words)):
            return None  # already a rule
    return memory.add(db, "rule", f"said:{session}", text)


def stop(payload: dict) -> dict | None:
    turn = transcript.last_turn(payload.get("transcript_path", ""))
    if not turn["user"] or _is_system(turn["user"]):
        return None
    text = f"User: {turn['user'][:CAPTURE_CHARS]}\nAssistant: {turn['reply'][:CAPTURE_CHARS]}"
    acts = [f"edited {', '.join(turn['files'][-6:])}" if turn["files"] else "",
            f"ran {'; '.join(turn['commands'][-4:])}" if turn["commands"] else "",
            f"{turn['failed']} tool call(s) failed" if turn["failed"] else ""]
    if any(acts):
        text += "\nActions: " + "; ".join(a for a in acts if a)
    db = memory.connect()
    try:
        memory.add(db, "turn", payload.get("session_id", "?"), shield.redact(text))
        memory.ledger_update(db, payload.get("session_id", "?"), payload.get("cwd") or "", turn)
    finally:
        db.close()
    return None


def subagent_stop(payload: dict) -> dict | None:
    """What an agent found is worth as much as what the main session said: keep its final text."""
    path = payload.get("agent_transcript_path") or ""
    msgs = transcript.messages(path)
    reply = next((t for r, t in reversed(msgs) if r == "assistant"), "")
    if not reply.strip():
        return None
    db = memory.connect()
    try:
        memory.add(db, "turn", payload.get("session_id", "?"), shield.redact("Agent result: " + reply[:AGENT_CHARS]))
    finally:
        db.close()
    return None


def git_state(db, cwd: str) -> None:
    """Uncommitted or unpushed work left in this folder goes on Stage; a clean tree takes it off again."""
    key = f"git/{cwd}"
    if not cwd:
        return
    try:
        status = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "--branch"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return
    if status.returncode:
        return  # not a repository
    lines = status.stdout.splitlines()
    dirty = len(lines) - 1
    ahead = re.search(r"ahead (\d+)", lines[0]) if lines else None
    parts = [f"{dirty} uncommitted change(s)" if dirty else "", f"{ahead.group(1)} unpushed commit(s)" if ahead else ""]
    if any(parts):
        stage.publish(db, key, "git", f"{cwd}: " + ", ".join(p for p in parts if p), score=4, days=3)
    else:
        stage.resolve(db, key)


HANDLERS = {"session-start": session_start, "user-prompt-submit": user_prompt_submit, "stop": stop,
            "subagent-stop": subagent_stop, "pre-tool-use": shield.pre_tool_use}


def run(name: str, stdin=sys.stdin, stdout=sys.stdout) -> int:
    handler = HANDLERS.get(name)
    if handler is None:
        print(f"lantern: unknown hook {name!r}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    t0 = time.monotonic()
    reply = handler(payload)
    if name != "pre-tool-use":  # every prompt-path hook reports how long it took; the doctor watches the trend
        _log("hooks", {"hook": name, "ms": round((time.monotonic() - t0) * 1000)})
    if reply:
        stdout.write(json.dumps(reply))
    return 0
