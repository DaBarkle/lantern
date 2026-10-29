"""Read the user and assistant text out of a Claude Code transcript (JSONL)."""
import json
from pathlib import Path


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def messages(path: str | Path) -> list[tuple[str, str]]:
    """[(role, text)] in order, skipping tool results and empty entries. Missing file -> []."""
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = entry.get("type")
        if role not in ("user", "assistant"):
            continue
        text = _text(entry.get("message", {}).get("content")).strip()
        if text:
            out.append((role, text))
    return out


def last_exchange(path: str | Path) -> tuple[str, str]:
    """(last user text, assistant text after it)."""
    user, reply = "", []
    for role, text in messages(path):
        if role == "user":
            user, reply = text, []
        else:
            reply.append(text)
    return user, "\n".join(reply)


def previous_user(path: str | Path) -> str:
    users = [t for r, t in messages(path) if r == "user"]
    return users[-1] if users else ""
