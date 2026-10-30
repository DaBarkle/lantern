"""Doctor: checks what each part is for, not just that it runs, and keeps Stage in step with the results.

A failing check publishes its finding to Stage; a passing check resolves it; a check that no longer exists has its
old finding dropped. So an alert lives exactly as long as its problem.
"""
import json
import os
import time

from . import discovery, memory, paths, resources, semantic, shield, stage, statusline
from .paths import ensure_data_dir

OK, WARN, FAIL = "ok", "warn", "fail"
RECALL_WINDOW = 50
RECALL_FLOOR = 0.2  # fewer prompts than this getting memory means retrieval has broken or drifted
STALE_DAYS = 3


def check_data_dir():
    d = ensure_data_dir()
    probe = d / ".doctor-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return FAIL, f"data folder {d} is not writable: {e}"
    return OK, f"data folder {d}"


START_EVERY = 300  # seconds between start attempts: a server that cannot come up (no memory) is not retried on every prompt


def start_server() -> None:
    """Start the memory server detached, at most once per START_EVERY; it exits by itself if one is already up."""
    import subprocess
    import sys
    import time
    from pathlib import Path
    marker = ensure_data_dir() / "serve.started"
    try:
        if time.time() - marker.stat().st_mtime < START_EVERY:
            return
    except OSError:
        pass
    marker.touch()
    # Its stderr goes to a log: a server that cannot start (no wheel for this CPU, a reserved port, no room) says why there.
    log = open(ensure_data_dir() / "serve.log", "w", encoding="utf-8")
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": log}
    kw["creationflags" if sys.platform == "win32" else "start_new_session"] = 0x00000208 if sys.platform == "win32" else True
    subprocess.Popen([paths.uv(), "run", "--no-project", "--quiet", str(Path(__file__).resolve().parents[1] / "serve.py")],
                     env={**os.environ, "LANTERN_HOME": str(ensure_data_dir())}, **kw)


WARM_SECONDS = 120  # a server started longer ago than this and still not answering did not come up


def check_semantic():
    if semantic.search(ensure_data_dir(), "doctor") is None:
        if not resources.can_spawn():
            return WARN, "memory server is down and the machine is short of memory, so it was not started; recall is keyword-only"
        marker, log = ensure_data_dir() / "serve.started", ensure_data_dir() / "serve.log"
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            age = None
        if age is not None and WARM_SECONDS < age < START_EVERY:
            why = ""
            try:
                why = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1][:160]
            except (OSError, IndexError):
                pass
            return WARN, (f"memory server did not come up after its last start ({why or 'no error written'}; full log in {log}); "
                          "recall is keyword-only on this machine")
        try:
            start_server()  # repair rather than report, as the plugin monitor would at the next session
        except OSError as e:
            return WARN, f"memory server was down and could not be started ({e}); recall is keyword-only"
        return OK, "memory server was down; started it (recall is keyword-only until it is warm)"
    return OK, "memory server answering"


def check_hook_errors():
    """Hooks that crashed in the last day (hooks.run catches and logs them, so a prompt is never blocked)."""
    try:
        lines = (ensure_data_dir() / "errors.jsonl").read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return OK, "no hook errors recorded"
    recent = [json.loads(l) for l in lines if l.strip()]
    recent = [r for r in recent if time.time() - r.get("ts", 0) < 86400]
    if not recent:
        return OK, "no hook errors in the last day"
    last = recent[-1]
    return WARN, f"{len(recent)} hook error(s) in the last day, last in {last.get('hook')}: {last.get('error', '')[:160]}"


def check_memory_fresh():
    db = memory.connect()
    try:
        newest = db.execute("SELECT max(created) FROM items WHERE kind='turn'").fetchone()[0]
    finally:
        db.close()
    if newest is None:
        return OK, "memory is empty; it fills as you work"
    days = (time.time() - newest) / 86400
    if days > STALE_DAYS and _recent_prompts() > 0:
        return WARN, f"no new memory for {days:.0f} days although you have been working; capture may have stopped"
    return OK, f"newest memory {days * 24:.0f} h old"


def _recall_log() -> list[dict]:
    try:
        lines = (ensure_data_dir() / "recall.jsonl").read_text().splitlines()[-RECALL_WINDOW:]
    except OSError:
        return []
    return [json.loads(l) for l in lines if l.strip()]


def _recent_prompts() -> int:
    return sum(1 for r in _recall_log() if time.time() - r.get("ts", 0) < STALE_DAYS * 86400)


def check_recall_rate():
    log = _recall_log()
    if len(log) < 20:
        return OK, f"recall rate: too few prompts yet ({len(log)})"
    rate = sum(r.get("fired", False) for r in log) / len(log)
    if rate < RECALL_FLOOR:
        return WARN, f"recall brought in memory on only {rate:.0%} of the last {len(log)} prompts"
    return OK, f"recall brought in memory on {rate:.0%} of the last {len(log)} prompts"


def check_shield():
    canary = "token = " + "ab12" * 6  # built at runtime so this file never trips a scanner
    if not shield.find(canary):
        return FAIL, "shield did not catch a test secret; secret protection is off"
    return OK, "shield catches test secrets"


HOOK_P90_MS = 500


def check_hook_latency():
    try:
        lines = (ensure_data_dir() / "hooks.jsonl").read_text().splitlines()[-200:]
    except OSError:
        return OK, "hook latency: nothing measured yet"
    entries = [json.loads(l) for l in lines if l.strip()]
    ms = sorted(e["ms"] for e in entries if e.get("hook") != "stop")  # Stop runs async (it waits for the reply text); never on the prompt path
    if len(ms) < 20:
        return OK, f"hook latency: too few calls yet ({len(ms)})"
    p90 = ms[int(len(ms) * 0.9) - 1]
    if p90 > HOOK_P90_MS:
        return WARN, f"hooks are slow: 9 in 10 finish within {p90} ms (budget {HOOK_P90_MS} ms)"
    return OK, f"hooks: 9 in 10 finish within {p90} ms"


def check_resources():
    free = resources.available_mb()
    if free is not None and free < resources.MIN_FREE_MB:
        return WARN, f"the machine has only {free} MB free; Lantern is holding its background work until that recovers"
    return OK, "memory headroom ok" if free is None else f"{free:,} MB free"


def check_status_line():
    status, msg, _ = statusline.ensure()  # repairs a stale command itself, as with the memory server
    return (OK if status == "ok" else WARN), msg


def check_packs():
    from . import packs
    on = sorted(packs.active())
    if not on:
        return OK, "no packs on"
    fixed = packs.sync()
    return OK, f"packs on: {', '.join(on)}" + (f" (restored the skill of {', '.join(fixed)})" if fixed else "")


def check_hook_smoke():
    """Every hook entry point against a throwaway returning-session profile: a crash shows here, never only in a log."""
    from . import smoke
    problems = smoke.run()
    if problems:
        return FAIL, "a hook crashes on a returning session: " + problems[0].splitlines()[0] + f" ({len(problems)} problem(s); run `lantern doctor` for the trace)"
    return OK, "every hook runs clean on a returning session"


CHECKS = {"data-dir": check_data_dir, "hook-smoke": check_hook_smoke, "status-line": check_status_line, "semantic": check_semantic, "memory-fresh": check_memory_fresh,
          "recall-rate": check_recall_rate, "shield": check_shield, "hook-latency": check_hook_latency, "hook-errors": check_hook_errors,
          "resources": check_resources, "packs": check_packs}


def run_all() -> list[tuple[str, str, str]]:
    results = []
    checks = dict(CHECKS)
    for cap_id, cap in discovery.settings().get("capabilities", {}).items():
        if cap.get("check"):  # each accepted capability is checked for what it is for: being up
            checks[f"cap:{cap_id}"] = lambda c=cap: ((OK, f"{c['name']} is up") if discovery.healthy(c["check"])
                                                    else (WARN, f"{c['name']} is not responding"))
    for name, check in checks.items():
        try:
            status, msg = check()
        except Exception as e:  # a broken check is itself a finding
            status, msg = FAIL, f"check crashed: {e}"
        results.append((name, status, msg))
    # One line for the capabilities that are fine; only a missing one deserves its own.
    fine = [r for r in results if r[0].startswith("cap:") and r[1] == OK]
    if len(fine) > 1:
        results = [r for r in results if r not in fine] + [("capabilities", OK, f"{len(fine)} known capabilities are up")]
    db = memory.connect()
    try:
        for name, status, msg in results:
            if status == OK:
                stage.resolve(db, f"doctor/{name}")
            else:
                stage.publish(db, f"doctor/{name}", "doctor", msg, score=8 if status == FAIL else 5, days=2)
        stage.resolve_source(db, "doctor", {f"doctor/{n}" for n, s, _ in results if s != OK})
        for name in fine:  # folded into one line above, so clear any old finding of theirs too
            stage.resolve(db, f"doctor/{name[0]}")
    finally:
        db.close()
    return results


def cli() -> int:
    from . import __version__
    results = run_all()
    print(f"lantern {__version__}")
    for name, status, msg in results:
        print(f"{status:<5} {name:<13} {msg}")
    return 1 if any(s == FAIL for _, s, _ in results) else 0
