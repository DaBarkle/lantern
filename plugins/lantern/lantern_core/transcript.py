"""Read the user and assistant text, and what tools did, out of a Claude Code transcript (JSONL)."""
import json
import re
from pathlib import Path

TAIL_BYTES = 4_000_000  # a long session's transcript runs to hundreds of MB; the last turns are all a hook needs
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _entries(path: str | Path) -> list[dict]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]  # the first line is cut mid-way
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
            out.append(e)
    return out


def messages(path: str | Path) -> list[tuple[str, str]]:
    """[(role, text)] in order, skipping tool results and empty entries. Missing file -> []."""
    out = []
    for e in _entries(path):
        text = _text(e.get("message", {}).get("content")).strip()
        if text:
            out.append((e["type"], text))
    return out


def last_turn(path: str | Path) -> dict:
    """The last user message, the assistant text after it, and what tools did meanwhile:
    {"user", "reply", "files", "commands", "agents", "failed"}."""
    turn = {"user": "", "reply": [], "files": [], "commands": [], "agents": [], "failed": 0}
    for e in _entries(path):
        content = e.get("message", {}).get("content")
        text = _text(content).strip()
        blocks = content if isinstance(content, list) else []
        if e["type"] == "user":
            if text:
                turn = {"user": text, "reply": [], "files": [], "commands": [], "agents": [], "failed": 0}
            turn["failed"] += sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"))
            continue
        if text:
            turn["reply"].append(text)
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = b.get("name", ""), b.get("input") or {}
            if name in EDIT_TOOLS and inp.get("file_path"):
                turn["files"].append(inp["file_path"])
            elif name == "Bash" and inp.get("command"):
                turn["commands"].append(inp["command"].strip().splitlines()[0][:160])
            elif name == "Agent" and inp.get("description"):
                turn["agents"].append(inp["description"][:120])
    turn["reply"] = "\n".join(turn["reply"])
    for k in ("files", "commands", "agents"):
        turn[k] = list(dict.fromkeys(turn[k]))  # each once, in order
    return turn


_ANSWER = re.compile(r'"((?:[^"\\]|\\.){8,400}?)"="((?:[^"\\]|\\.){1,400}?)"')


def answers(path: str | Path) -> list[tuple[str, str]]:
    """Every (question, answer) pair the user gave through the question tool, in order, read from the tool results."""
    out = []
    for e in _entries(path):
        content = e.get("message", {}).get("content")
        if e["type"] != "user" or not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                text = _text(b.get("content")) if not isinstance(b.get("content"), str) else b["content"]
                if "answered" in text:
                    out += [(q, a) for q, a in _ANSWER.findall(text)]
    return out


def last_exchange(path: str | Path) -> tuple[str, str]:
    """(last user text, assistant text after it)."""
    t = last_turn(path)
    return t["user"], t["reply"]


def previous_user(path: str | Path) -> str:
    users = [t for r, t in messages(path) if r == "user"]
    return users[-1] if users else ""
