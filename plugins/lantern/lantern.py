# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Lantern command line. Hooks call: uv run --no-project lantern.py hook <name>"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lantern_core import __version__, hooks  # noqa: E402
from lantern_core.paths import data_dir  # noqa: E402


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"] and len(argv) == 2:
        return hooks.run(argv[1])
    if argv[:1] == ["doctor"]:
        d = data_dir()
        print(f"lantern {__version__}\ndata folder: {d} ({'ok' if d.exists() else 'not created yet'})")
        return 0
    print("usage: lantern.py hook <name> | doctor", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
