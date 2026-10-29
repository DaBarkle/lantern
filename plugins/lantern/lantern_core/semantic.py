"""Client side of the memory server. Stdlib only, so the per-prompt hook stays fast."""
import json
import secrets
import socket
import zlib
from pathlib import Path

MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # same model as Hermes, so its vectors import as-is
DIM = 384
BUDGET_SECONDS = 0.3


def port_for(data: Path) -> int:
    # One port per data folder, so two users or two test runs never share a server.
    return 47000 + zlib.crc32(str(data.resolve()).encode()) % 2000


def write_token(data: Path) -> str:
    token = secrets.token_hex(16)
    path = data / "serve.token"
    path.write_text(token)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows: the per-user data folder already limits access
    return token


def search(data: Path, query: str, limit: int = 30) -> list[tuple[int, float]]:
    """[(item id, cosine)] from the warm server, or [] if it is not running or too slow."""
    try:
        token = (data / "serve.token").read_text().strip()
        with socket.create_connection(("127.0.0.1", port_for(data)), timeout=BUDGET_SECONDS) as s:
            s.settimeout(BUDGET_SECONDS)
            s.sendall((json.dumps({"token": token, "op": "search", "q": query, "limit": limit}) + "\n").encode())
            reply = json.loads(s.makefile("r", encoding="utf-8").readline())
        return [(int(i), float(c)) for i, c in reply.get("hits", [])]
    except (OSError, ValueError):
        return []
