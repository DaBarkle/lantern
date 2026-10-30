#!/usr/bin/env -S uv run --no-project --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Lantern command line. Hooks call: uv run --no-project lantern.py hook <name>"""
import hashlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lantern_core import alerts, approvals, curate, discovery, doctor, hooks, insights, memory, schedule, shield, stage  # noqa: E402

USAGE = """usage: lantern.py hook <name>
       lantern.py doctor | status
       lantern.py stage list | note <message> [--key K] [--score 1-10] [--days N] | resolve <key>
       lantern.py secret set NAME | run NAME[,NAME...] -- <command> | list | delete NAME | link NAME <ref> | status
       lantern.py ssh grant HOST [--dry-run] | ssh status     (Lantern's own key, after one tap on the phone)
       lantern.py remember [--profile|--rule|--pattern] <text> | forget <query> | forget --id N | recall <query> | profile
       lantern.py approve setup | "<question>" [--timeout SECONDS]
       lantern.py alerts [on | off | kinds failed,down,disk,backup]   (phone line per newly tripped watch)
       lantern.py depth [1-4] | discover [--accept all|1,2,...]
       lantern.py job curate | doctor | discover | watch   (what the schedule runs)
       lantern.py schedule [--apply]           (show, or install, the nightly jobs and the hourly watch)"""


def stage_cli(argv: list[str]) -> int:
    db = memory.connect()
    try:
        if argv[:1] == ["list"]:
            for e in stage.top(db, k=50):
                print(f"{e['salience']:5.1f}  {e['key']:<28} [{e['source']}] {e['message']}")
            return 0
        if argv[:1] == ["note"] and len(argv) >= 2:
            opts = dict(zip(argv[2::2], argv[3::2]))
            stage.publish(db, opts.get("--key", "note/" + hashlib.sha256(argv[1].encode()).hexdigest()[:10]), "note", argv[1],
                          float(opts.get("--score", 5)), float(opts.get("--days", stage.DEFAULT_DAYS)))
            return 0
        if argv[:1] == ["resolve"] and len(argv) == 2:
            return 0 if stage.resolve(db, argv[1]) else 1
    finally:
        db.close()
    print(USAGE, file=sys.stderr)
    return 2


def settle_setup(item: str) -> None:
    """A setup question answered through the question tool (profile stored, rule stored, depth raised) stops being pending,
    exactly as a bare "ok" in chat does; otherwise every later session re-asks it."""
    s = discovery.settings()
    if item and item in (s.get("pending") or []):
        s["pending"] = [p for p in s["pending"] if p != item]
        s["answered_at"] = time.time()
        discovery.save_settings(s)


def memory_cli(argv: list[str]) -> int:
    db = memory.connect()
    try:
        if argv[:1] == ["remember"] and len(argv) >= 2:
            kind = {"--profile": "profile", "--rule": "rule", "--pattern": "pattern"}.get(argv[1], "fact")
            text = " ".join(argv[2:] if kind != "fact" else argv[1:])
            if shield.find(text):
                print("that looks like a secret; Lantern will not store it in memory (ask for it with `lantern secret ask NAME`, it goes to the keychain)", file=sys.stderr)
                return 1
            memory.add(db, kind, "manual", text)
            settle_setup({"profile": "purpose", "rule": "guardrails"}.get(kind, ""))
            print(f"remembered ({kind})")
            return 0
        if argv[:2] == ["forget", "--id"] and len(argv) == 3:
            memory.supersede(db, int(argv[2]))
            print(f"forgot #{argv[2]}")
            return 0
        if argv[:1] in (["forget"], ["recall"]) and len(argv) >= 2:
            for h in memory.search(db, " ".join(argv[1:]), limit=8):
                print(f"#{h['id']:<7} [{h['kind']}, {time.strftime('%Y-%m-%d', time.localtime(h['created']))}] {h['text'][:300]!s}")
            if argv[0] == "forget":
                print("\nDrop one with `lantern forget --id N`.")
            return 0
        if argv == ["profile"]:
            print("Profile:\n" + (memory.profile(db, 6000) or "(nothing yet: it fills in as you work)"))
            rules = [(i, t) for i, t in memory.rule_items(db) if db.execute("SELECT kind FROM items WHERE id=?", (i,)).fetchone()[0] == "rule"]
            print("\nRules and preferences:\n" + ("\n".join(f"#{i} {t}" for i, t in rules)[:12000] or "(none yet: say 'from now on...' and it sticks)"))
            print("\nThis machine: " + (discovery.summary() or "(not looked at yet)"))
            return 0
    finally:
        db.close()
    print(USAGE, file=sys.stderr)
    return 2


def watch() -> list[str]:
    """Re-check the watches now and tell the phone what newly tripped."""
    db = memory.connect()
    try:
        insights.run(db, force=True)
        return alerts.run(db)
    finally:
        db.close()


def main(argv: list[str]) -> int:
    if "--data" in argv[:-1]:  # scheduled jobs: use the same data folder as the sessions
        i = argv.index("--data")
        os.environ["LANTERN_HOME"] = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if argv[:1] == ["hook"] and len(argv) == 2:
        return hooks.run(argv[1])
    if argv[:1] == ["doctor"]:
        return doctor.cli()
    if argv == ["status"]:
        from lantern_core import statusline
        return statusline.cli()
        return 0
    if argv[:1] == ["stage"]:
        return stage_cli(argv[1:])
    if argv[:1] == ["secret"]:
        return shield.cli(argv[1:])
    if argv[:1] == ["ssh"]:
        from lantern_core import sshkey
        return sshkey.cli(argv[1:])
    if argv[:1] in (["depth"], ["discover"]):
        if argv[:2] in (["depth", "3"], ["depth", "4"]):
            settle_setup("wider")
        return discovery.cli(argv if argv[0] == "depth" else argv[1:])
    if argv == ["job", "curate"]:
        from lantern_core import jobs
        print(f"curated {curate.run()} new rules and facts")
        jobs.mark("curate", "done")
        return 0
    if argv == ["job", "survey"]:
        from lantern_core import survey
        survey.run()
        return 0
    if argv == ["job", "discover"]:
        from lantern_core import insights, jobs
        print(f"discovered {len(discovery.auto())} new capabilities")
        db = memory.connect()
        try:
            print(f"{len(insights.run(db, force=True))} insights")
        finally:
            db.close()
        jobs.mark("discover", "done")
        return 0
    if argv == ["job", "doctor"]:
        from lantern_core import jobs
        code = doctor.cli()
        jobs.mark("doctor", "done")
        watch()  # a machine with only the nightly jobs still hears about a tripped watch once a day
        return code
    if argv == ["job", "watch"]:
        print(f"sent {len(watch())} phone line(s)")
        return 0
    if argv[:1] == ["schedule"]:
        entry = Path(__file__).resolve()
        from lantern_core.paths import data_dir
        plans = [schedule.plan(job, hour, entry, data=data_dir()) for job, hour in (("curate", 3), ("doctor", 4), ("watch", None))]
        for p in plans:
            for path in p["files"]:
                print(f"write {path}")
            for cmd in p["commands"]:
                print("run  " + " ".join(cmd))
        if "--apply" in argv:
            for p in plans:
                schedule.apply(p)
        return 0
    if argv[:1] in (["remember"], ["forget"], ["recall"], ["profile"]):
        return memory_cli(argv)
    if argv[:1] == ["approve"]:
        return approvals.cli(argv[1:])
    if argv[:1] == ["alerts"]:
        return alerts.cli(argv[1:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
