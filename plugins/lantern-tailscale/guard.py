"""Compatibility shim: the Tailscale guard now lives in the core plugin (packs/tailscale/guard.py). An install that still
enables lantern-tailscale@lantern keeps working until it is removed; this file only runs the real guard."""
import runpy
import sys
from pathlib import Path

sys.argv[0] = str(Path(__file__).resolve().parents[1] / "lantern" / "packs" / "tailscale" / "guard.py")
runpy.run_path(sys.argv[0], run_name="__main__")
