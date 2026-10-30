"""Lantern's own SSH key: one keypair made here, installed on a host after one tap on the phone. Nothing is typed, no
password changes hands and the user's own keys are never copied. The one-off install rides on the access the user
already has to the host (ssh-copy-id in batch mode, so it fails cleanly rather than asking); after that Lantern uses
only its own key."""
import platform
import shlex
import subprocess
import time
from pathlib import Path

from . import approvals, discovery

KEY = Path.home() / ".ssh" / "lantern_ed25519"
TIMEOUT = 60


def grants() -> dict:
    """host -> when Lantern's key was installed there."""
    return discovery.settings().get("ssh_grants", {})


def _plan(host: str) -> list[list[str]]:
    make = [] if KEY.exists() else [["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"lantern@{platform.node()}", "-f", str(KEY)]]
    return make + [["ssh-copy-id", "-i", f"{KEY}.pub", "-o", "BatchMode=yes", host],
                   ["ssh", "-i", str(KEY), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", host, "true"]]


def grant(host: str, dry_run: bool = False, ask=approvals.ask) -> tuple[int, str]:
    """Ask once on the phone, then make the key if needed, install it and prove it works. Exit codes follow approvals."""
    plan = _plan(host)
    if dry_run:
        return 0, "After one approval on the phone, Lantern would run:\n" + "\n".join("  " + shlex.join(c) for c in plan)
    if approvals.ready():
        return 2, f"not installed on {host}: {approvals.ready()}"
    try:
        code = ask(f"Install Lantern's own SSH key on {host}? It lets Lantern check that host (read-only duties); "
                   "your own keys stay where they are.")
    except OSError as e:
        return approvals.TIMED_OUT, f"not installed on {host}: the phone could not be reached ({e}); is this machine online?"
    if code != approvals.APPROVED:
        return code, f"not installed on {host}: " + ("denied on the phone" if code == approvals.DENIED else "no answer")
    KEY.parent.mkdir(mode=0o700, exist_ok=True)
    for cmd in plan:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, f"{cmd[0]} did not finish for {host}: {e}"
        if r.returncode != 0:
            return 1, f"{cmd[0]} failed for {host}: {(r.stderr or r.stdout).strip()[:300] or 'no details'}"
    s = discovery.settings()
    s.setdefault("ssh_grants", {})[host] = time.time()
    discovery.save_settings(s)
    return 0, f"Lantern can now reach {host} with its own key: ssh -i {KEY} -o IdentitiesOnly=yes {host} <command>"


def cli(argv: list[str]) -> int:
    """lantern ssh grant HOST [--dry-run] | lantern ssh status"""
    if argv[:2] == ["ssh", "status"] or argv == ["status"]:
        print(f"key: {KEY if KEY.exists() else 'not made yet (made at the first grant)'}")
        print("hosts: " + (", ".join(sorted(grants())) or "none yet"))
        return 0
    if argv[:1] == ["grant"] and len(argv) >= 2:
        code, msg = grant(argv[1], dry_run="--dry-run" in argv)
        print(msg)
        return code
    print(cli.__doc__)
    return 2
