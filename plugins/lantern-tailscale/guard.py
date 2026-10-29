"""PreToolUse guard: block commands that would change Tailscale. Reading status is fine."""
import json
import re
import sys

CHANGE = re.compile(r"\btailscale\s+(?:--\S+\s+)*(up|down|set|logout|login|serve|funnel|cert|switch|file|update|configure)\b"
                    r"|\b(?:systemctl|service)\b[^;&|]*\b(?:start|stop|restart|reload|enable|disable|mask|kill|edit)\b[^;&|]*\btailscaled\b"
                    r"|\b(?:systemctl|service)\b[^;&|]*\btailscaled\b[^;&|]*\b(?:start|stop|restart|reload)\b"
                    r"|\btailscaled\b\s+--(?:cleanup|state)")


def check(command: str) -> str | None:
    return "Lantern's Tailscale pack is read-only: this command would change Tailscale. Explain the change and let the user run it." if CHANGE.search(command or "") else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    reason = check((payload.get("tool_input") or {}).get("command", ""))
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        assert check("sudo tailscale up --ssh") and check("systemctl restart tailscaled") and check("tailscale --socket=/x set --exit-node=a")
        assert check("service tailscaled stop") and check("sudo systemctl disable --now tailscaled.service")
        assert not check("tailscale status --json") and not check("tailscale ping nyx")
        assert not check("systemctl status tailscaled") and not check("journalctl -u tailscaled -n 50")
        sys.exit(print("ok"))
    sys.exit(main())
