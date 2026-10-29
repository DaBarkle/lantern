# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Lantern command line. Hooks call: uv run --no-project lantern.py hook <name>"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lantern_core import doctor, hooks, memory, shield, stage  # noqa: E402

USAGE = """usage: lantern.py hook <name>
       lantern.py doctor
       lantern.py stage list | note <message> [--key K] [--score 1-10] [--days N] | resolve <key>
       lantern.py secret set NAME | run NAME[,NAME...] -- <command> | list | delete NAME"""


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


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"] and len(argv) == 2:
        return hooks.run(argv[1])
    if argv[:1] == ["doctor"]:
        return doctor.cli()
    if argv[:1] == ["stage"]:
        return stage_cli(argv[1:])
    if argv[:1] == ["secret"]:
        return shield.cli(argv[1:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
