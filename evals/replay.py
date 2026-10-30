#!/usr/bin/env -S uv run --no-project --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Does Claude need less re-teaching with Lantern? Scored on your own transcripts.

Takes your newest prompts from ~/.claude/projects, builds a scratch Lantern memory from everything you said before
their sessions began (turns captured as the hooks capture them, then curated as the nightly job does), and has the same
model answer each prompt twice: once with nothing (vanilla), once with what Lantern's hooks would have loaded (profile,
rules, notes, recall). A blind judge sees both replies in random order, with what you actually said next as ground truth.

Usage: uv run --no-project evals/replay.py [N]      (default 30 prompts; replies and judge use claude-opus-5-5, REPLAY_MODEL overrides)
Cost: about 3 N short Opus calls, plus one curation call (the nightly job's model) per 60k characters of history.
Run it once per real change. Your transcripts are only read; the scratch memory lives under evals/data and your real
Lantern data is never touched. Starts one memory server on the scratch memory and proves it is gone afterwards.
"""
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "lantern"
sys.path.insert(0, str(ROOT))
from lantern_core import curate, discovery, hooks, insights, jobs, memory, resources, semantic, shield, survey, transcript  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"
DATA = Path(__file__).resolve().parent / "data"
MODEL = os.environ.get("REPLAY_MODEL", "claude-opus-5-5")
HISTORY = 20  # every arm sees the conversation so far, as a real session would
MIN_PROMPT = 40  # "yes", "ok", "do it" carry nothing to judge
PER_SESSION = 4  # thirty prompts from one long session would all test the same topic
INJECTED = ("<", "[", "Caveat:", "This session is being continued")  # text Claude Code put in the user slot, not the user
MIN_FREE_MB = 6000  # a run holds one memory server and two claude processes; never start into a machine under pressure

REPLY = """You are Claude Code, the user's coding and operations assistant. Below is the context your harness loaded for this
session, then the user's message. Reply as you would on your first turn. You cannot run tools here: say what you would
do, and state any assumption you make about their machines, rules or ongoing work.

<harness-context>
{context}
</harness-context>

<conversation-so-far>
{history}
</conversation-so-far>

User: {prompt}"""

JUDGE = """You grade two assistant replies to the same message from a user with long-running projects on their own machines.
You also see what actually happened next: the assistant's real reply at the time and the user's next message, which
shows the context the assistant needed (corrections, facts the user had to supply).

First say whether this message needed personal memory at all: needs_memory is false when the message is self-contained
(a general question, pure coding on pasted content) and true when knowing the user's machines, rules, preferences or
ongoing work would change a good reply.
Then for each reply give:
- reteach: 0 = the user would not need to explain anything; 1 = a little; 2 = they would need to re-explain things
  their assistant should already know about them, their machines, rules or projects
- wrong: true if the reply states something about the user or their setup that is false or out of date
- score: 1-5 overall usefulness to this user
Judge only what each reply says. Replies are labelled X and Y in random order.

MESSAGE: {prompt}

WHAT HAPPENED NEXT:
assistant then: {then}
user next: {next}

X: {x}

Y: {y}
"""
GRADE = {"type": "object", "required": ["reteach", "wrong", "score"], "properties": {
    "reteach": {"enum": [0, 1, 2]}, "wrong": {"type": "boolean"}, "score": {"type": "integer"}}}
SCHEMA = {"type": "object", "required": ["needs_memory", "X", "Y"],
          "properties": {"needs_memory": {"type": "boolean"}, "X": GRADE, "Y": GRADE}}
TEXT = {"type": "object", "required": ["reply"], "properties": {"reply": {"type": "string"}}}


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def mine(projects: Path = PROJECTS) -> dict[str, list[dict]]:
    """Each session's user turns in order: {ts, text, reply, ok}; ok marks text the user typed (not injected)."""
    sessions = {}
    for f in sorted(projects.glob("*/*.jsonl")):
        turns = []
        try:
            lines = open(f, errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
                    continue
                text = transcript._text((e.get("message") or {}).get("content")).strip()
                if not text:
                    continue  # tool results and tool calls
                if e["type"] == "assistant":
                    if turns:
                        turns[-1]["reply"] = (turns[-1]["reply"] + "\n" + text).strip()
                    continue
                turns.append({"ts": _ts(e.get("timestamp", "")), "text": text, "reply": "",
                              "ok": not text.startswith(INJECTED) and len(text) < 4000})
        if turns:
            sessions[f.stem] = turns
    return sessions


def history(earlier: list[dict]) -> str:
    """The last turns kept whole, as a real session has them; older ones trimmed."""
    n = len(earlier)
    return "\n\n".join(f"User: {u['text'][:800 if k < n - 6 else 4000]}\nClaude: {u['reply'][-(600 if k < n - 6 else 3000):]}"
                       for k, u in enumerate(earlier)) or "(this is the first message)"


def pick(sessions: dict[str, list[dict]], n: int) -> tuple[list[dict], float]:
    """The newest n prompts that have a follow-up (the ground truth), a few per session, oldest first, and the cutoff: memory may only
    hold turns from before the earliest of their sessions began, so nothing is answered from its own session."""
    cands = []
    for session, turns in sessions.items():
        typed = [t for t in turns if t["ok"]]
        for i, t in enumerate(typed):
            if len(t["text"]) < MIN_PROMPT or i + 1 == len(typed):
                continue
            cands.append({**t, "session": session, "first": i == 0, "start": turns[0]["ts"], "then": t["reply"],
                          "next": typed[i + 1]["text"], "earlier": typed[max(0, i - HISTORY):i]})
    cands.sort(key=lambda c: c["ts"], reverse=True)
    items, per = [], {}
    for c in cands:
        if per.get(c["session"], 0) < PER_SESSION and len(items) < n:
            per[c["session"]] = per.get(c["session"], 0) + 1
            items.append(c)
    items.reverse()
    for it in items:
        it["history"] = history(it["earlier"])
    return items, min((it["start"] for it in items), default=0.0)


def build_memory(sessions: dict[str, list[dict]], cutoff: float, ask) -> tuple[int, int]:
    """The scratch data folder holds what the hooks would have captured before the cutoff, curated as the nightly
    job does it, window by window, each note dated at its window's end. Returns (turns stored, notes curated)."""
    home = ensure_scratch()
    (home / "settings.json").write_text(json.dumps({"depth": 2, "first_session": "replay"}))  # a set-up user: no first-run tour
    rows = []
    db = memory.connect()
    try:
        for session, turns in sessions.items():
            for t in turns:
                if t["ok"] and t["reply"] and 0 < t["ts"] < cutoff:
                    text = f"User: {t['text'][:hooks.CAPTURE_CHARS]}\nAssistant: {t['reply'][:hooks.CAPTURE_CHARS]}"
                    memory.add(db, "turn", session, shield.redact(text), created=t["ts"])
                    rows.append((t["ts"], len(text)))
    finally:
        db.close()
    rows.sort()
    windows, start, chars = [], 0.0, 0
    for ts, n in rows:
        if chars + n > curate.MAX_CHARS - 2000:
            windows.append((start, ts - 0.001))
            start, chars = ts - 0.001, 0
        chars += n
    windows.append((start, (rows[-1][0] if rows else 0) + 1))
    state = home / "curate.json"
    curated = 0
    for i, (a, b) in enumerate(windows, 1):
        state.write_text(json.dumps({"until": a}))
        try:
            curated += curate.run(now=b, ask=ask)
        except RuntimeError as e:
            print(f"curation window {i} failed: {e}", file=sys.stderr)
        print(f"curated {i}/{len(windows)}", file=sys.stderr, flush=True)
    return len(rows), curated


def ensure_scratch() -> Path:
    home = Path(os.environ["LANTERN_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    return home


def start_server(home: Path):
    """Lantern's memory server on the scratch memory, so recall runs with semantic search as in a real session.
    Returns the process, or None (keyword-only recall) when the machine is short of memory or it does not come up."""
    free = resources.available_mb()
    if free is not None and free < MIN_FREE_MB:
        print(f"only {free} MB free: recall runs keyword-only", file=sys.stderr)
        return None
    kw = {"start_new_session": True} if sys.platform != "win32" else {}  # own group: uv runs serve.py as a child
    proc = subprocess.Popen(["uv", "run", "--no-project", "--quiet", str(ROOT / "serve.py")],
                            env={**os.environ, "LANTERN_HOME": str(home)}, stderr=subprocess.DEVNULL, **kw)
    for _ in range(900):  # a fresh model embeds all of memory before it is useful
        if semantic.search(home, "warm up") is not None:
            db = memory.connect()
            try:
                if db.execute("SELECT count(*) FROM items WHERE vec IS NULL").fetchone()[0] == 0:
                    return proc
            finally:
                db.close()
        time.sleep(1)
    stop_server(home, proc)
    print("memory server did not come up: recall runs keyword-only", file=sys.stderr)
    return None


def stop_server(home: Path, proc) -> None:
    """Kill the server and prove its port is closed: a leaked server costs 300 MB for as long as it lives."""
    if proc is None:
        return
    try:
        os.killpg(proc.pid, 9) if hasattr(os, "killpg") else proc.kill()
    except ProcessLookupError:
        pass
    proc.wait()
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", semantic.port_for(home)), timeout=0.2).close()
            time.sleep(0.1)
        except OSError:
            return
    raise RuntimeError(f"memory server for {home} is still running; stop it by hand (pgrep -f serve.py)")


def lantern_contexts(items: list[dict]) -> None:
    """What the hooks would have loaded for each prompt: the session-start block, then recall for the prompt."""
    # A replay must not touch this machine: no background jobs, survey, discovery or watches, and the history is not
    # captured into the scratch memory (it is already in the conversation).
    quiet = [mock.patch.object(jobs, "start", lambda *a, **k: []), mock.patch.object(survey, "start", lambda *a, **k: False),
             mock.patch.object(insights, "run", lambda *a, **k: []), mock.patch.object(discovery, "auto", lambda *a, **k: []),
             mock.patch.object(discovery, "refresh_frontiers", lambda: []),
             mock.patch.object(hooks, "capture_previous", lambda payload: None),
             mock.patch.object(hooks, "capture_last_session", lambda cwd, current: None)]
    tp = ensure_scratch() / "replay-transcript.jsonl"
    with ExitStack() as stack:
        for q in quiet:
            stack.enter_context(q)
        start = hooks.session_start({"session_id": "replay"})["hookSpecificOutput"]["additionalContext"]
        for it in items:  # oldest first, so a rule a prompt states can only reach later prompts, as in real use
            tp.write_text("".join(json.dumps({"type": "user", "message": {"content": u["text"]}}) + "\n" for u in it["earlier"]))
            r = hooks.user_prompt_submit({"prompt": it["text"], "session_id": it["session"], "transcript_path": str(tp)})
            it["lantern"] = start + (("\n\n" + r["hookSpecificOutput"]["additionalContext"]) if r else "")


def grade(items: list[dict], ask) -> list[dict]:
    rng = random.Random(7)
    results = []
    with ThreadPoolExecutor(2) as pool:  # both arms' replies at once
        for k, it in enumerate(items, 1):
            futs = {arm: pool.submit(ask, REPLY.format(context=ctx or "(none)", history=it["history"], prompt=it["text"]), TEXT)
                    for arm, ctx in (("vanilla", ""), ("lantern", it["lantern"]))}
            replies = {arm: f.result()["reply"] for arm, f in futs.items()}
            order = list(replies)
            rng.shuffle(order)
            g = ask(JUDGE.format(prompt=it["text"], then=it["then"][-800:], next=it["next"][:800],
                                 **{x: replies[a][:3000] for x, a in zip("xy", order)}), SCHEMA)
            results.append({"session": it["session"], "prompt": it["text"], "first": it["first"], "needs_memory": g["needs_memory"],
                            "replies": replies, "lantern_context": it["lantern"], "grades": {a: g[x] for x, a in zip("XY", order)}})
            print(f"{k}/{len(items)}", file=sys.stderr, flush=True)
    return results


def report(results: list[dict], stored: int, curated: int, semantic_on: bool) -> str:
    lines = [f"{len(results)} of your prompts, one reply per arm, blind judge ({MODEL}). Memory: {stored} earlier turns, "
             f"{curated} curated notes, {'semantic and keyword' if semantic_on else 'keyword-only'} recall."]
    for name, sub in (("all prompts", results), ("prompts that needed personal memory", [r for r in results if r["needs_memory"]]),
                      ("session openers", [r for r in results if r["first"]])):
        if not sub:
            continue
        lines.append(f"\n{name} ({len(sub)}):")
        for arm in ("vanilla", "lantern"):
            gs = [r["grades"][arm] for r in sub]
            lines.append(f"  {arm:<8} no re-teach {sum(g['reteach'] == 0 for g in gs)}/{len(gs)}  needs re-teach {sum(g['reteach'] == 2 for g in gs)}"
                         f"  wrong about you {sum(g['wrong'] for g in gs)}  mean score {sum(g['score'] for g in gs) / len(gs):.2f}")
        wins = sum(r["grades"]["lantern"]["score"] > r["grades"]["vanilla"]["score"] for r in sub)
        losses = sum(r["grades"]["lantern"]["score"] < r["grades"]["vanilla"]["score"] for r in sub)
        lines.append(f"  lantern vs vanilla: {wins} better, {losses} worse, {len(sub) - wins - losses} tied")
    return "\n".join(lines)


def main(n: int = 30, projects: Path = PROJECTS, ask=None, curate_ask=None, server: bool = True) -> str:
    home = DATA / "replay-memory"
    shutil.rmtree(home, ignore_errors=True)  # the previous run's scratch memory, nothing else lives there
    os.environ.pop("CLAUDE_PLUGIN_DATA", None)  # run from inside a session, that would point the replay at your real memory
    os.environ["LANTERN_HOME"] = str(home)
    ask = ask or (lambda prompt, schema: curate.ask(prompt, schema, MODEL))
    sessions = mine(projects)
    items, cutoff = pick(sessions, n)
    if not items:
        sys.exit(f"no prompts with a follow-up message found under {projects}")
    stored, curated = build_memory(sessions, cutoff, curate_ask or curate._ask)
    proc = start_server(home) if server else None
    try:
        lantern_contexts(items)
    finally:
        stop_server(home, proc)
    results = grade(items, ask)
    out = DATA / f"replay-{time.strftime('%Y%m%d-%H%M')}.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in results))
    return report(results, stored, curated, proc is not None) + f"\nReplies, contexts and grades: {out}"


if __name__ == "__main__":
    print(main(int(sys.argv[1]) if len(sys.argv) > 1 else 30))
