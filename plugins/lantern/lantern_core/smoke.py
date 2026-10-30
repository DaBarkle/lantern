"""Every hook entry point run against a throwaway returning-session profile, in process, with realistic stdin: a
crashing hook is a crash the user never sees (hooks.run logs it and carries on), so the suite and the doctor run this
and fail loudly instead. Nothing here touches the real data folder, spawns a job or starts a server."""
import io
import json
import os
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


def _transcript(path: Path) -> None:
    rows = [{"type": "user", "timestamp": "2026-10-30T10:00:00Z", "message": {"role": "user", "content": "is plex ok?"}},
            {"type": "assistant", "timestamp": "2026-10-30T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Plex is up. I didn't check its log."}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def run(verbose: bool = False) -> list[str]:
    """Problems found, as strings ('<hook>: <error>'); empty when every hook ran clean."""
    from . import alerts, discovery, doctor, hooks, jobs, memory, packs, paths, statusline, survey
    problems = []
    with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
        home = Path(d)
        (home / "data").mkdir()
        stack.enter_context(mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(home / "data")}))
        stack.enter_context(mock.patch.object(paths, "_pointer", lambda: home / "data-dir"))  # never the real pointer
        for mod, name, value in [(jobs, "start", lambda *a, **k: []), (doctor, "start_server", lambda: None),
                                 (survey, "start", lambda *a, **k: False), (discovery, "discover", lambda depth: []),
                                 (discovery, "frontiers", lambda: []), (alerts, "ensure_scheduled", lambda *a, **k: False),
                                 (statusline, "settings_file", lambda: home / "settings.json"), (packs, "sync", lambda *a, **k: [])]:
            stack.enter_context(mock.patch.object(mod, name, value))
        transcript = home / "t.jsonl"
        _transcript(transcript)
        # A returning session: setup answered, one rule, one turn stored, one since-last block already computed.
        discovery.save_settings({"depth": 2, "answered_at": time.time() - 86400, "capabilities": {}})
        db = memory.connect()
        try:
            memory.add(db, "rule", "setup", "Never restart Plex without asking first.")
            memory.add(db, "turn", "s0", "User: is plex ok?\nAssistant: Plex is up.")
        finally:
            db.close()
        calls = [("session-start", {"source": "startup", "session_id": "s0", "cwd": d, "transcript_path": str(transcript)}),
                 ("session-start", {"source": "startup", "session_id": "s1", "cwd": d, "transcript_path": str(transcript)}),
                 ("user-prompt-submit", {"prompt": "what changed since yesterday?", "session_id": "s1", "cwd": d, "transcript_path": str(transcript)}),
                 ("user-prompt-submit", {"prompt": "plex looks stuck, restart it now", "session_id": "s1", "cwd": d, "transcript_path": str(transcript)}),
                 ("pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": "systemctl --user status container-plex.service"}, "session_id": "s1", "transcript_path": str(transcript)}),
                 ("stop", {"session_id": "s1", "cwd": d, "transcript_path": str(transcript)}),
                 ("stop-nudge", {"session_id": "s1", "cwd": d, "transcript_path": str(transcript)}),
                 ("subagent-stop", {"session_id": "s1", "cwd": d, "transcript_path": str(transcript)}),
                 ("signal", {"session_id": "s1", "transcript_path": str(transcript)})]
        for name, payload in calls:
            out = io.StringIO()
            try:
                code = hooks.run(name, stdin=io.StringIO(json.dumps(payload)), stdout=out)
            except Exception as e:  # the shield re-raises on purpose; here that is a failure like any other
                problems.append(f"{name}: {type(e).__name__}: {e}")
                continue
            if code != 0:
                problems.append(f"{name}: exit {code}")
            if out.getvalue():
                try:
                    json.loads(out.getvalue())
                except ValueError:
                    problems.append(f"{name}: output is not JSON")
            if verbose:
                print(f"{name}: ok ({len(out.getvalue())} chars)")
        try:
            for line in (home / "data" / "errors.jsonl").read_text().splitlines():
                e = json.loads(line)
                problems.append(f"{e['hook']}: {e['error']}\n{e.get('trace', '')[-600:]}")
        except OSError:
            pass
    return problems
