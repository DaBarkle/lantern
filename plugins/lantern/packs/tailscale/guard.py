"""PreToolUse guard: block commands that would change Tailscale. Reading status is fine."""
import json
import re
import sys

DECISION = "deny"

# Only a word in command position counts: the start of a command (after ; & | newline or a subshell), optionally
# behind sudo/env/exec. Prose that mentions the tool in a heredoc or a quoted string is not a command.
CMD = r"(?:^|[;&|(\n]\s*)(?:(?:sudo|exec|env|command|nohup|time)\s+(?:-\S+\s+|\w+=\S*\s+)*)?"
CHANGE = re.compile(CMD + r"tailscale\s+(?:--\S+\s+)*(up|down|set|logout|login|serve|funnel|cert|switch|file|update|configure)\b"
                    "|" + CMD + r"(?:systemctl|service)\b[^;&|\n]*\b(?:start|stop|restart|reload|enable|disable|mask|kill|edit)\b[^;&|\n]*\btailscaled\b"
                    "|" + CMD + r"(?:systemctl|service)\b[^;&|\n]*\btailscaled\b[^;&|\n]*\b(?:start|stop|restart|reload)\b"
                    "|" + CMD + r"tailscaled\b\s+--(?:cleanup|state)")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\s*(?=\n|$)", re.S)


def check(command: str) -> str | None:
    text = HEREDOC.sub("<<heredoc", command or "")  # the body of a heredoc is data, not commands
    return "Lantern's Tailscale pack is read-only: this command would change Tailscale. Explain the change and let the user run it." if CHANGE.search(text) else None


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
        assert not check("cat > notes.md <<'EOF'\nNever run tailscale up here; set is also off limits.\nEOF")  # prose in a heredoc
        assert check("cd /tmp && sudo tailscale up --ssh") and check("x=1; tailscale set --exit-node=a") and check("(tailscale down)")
        sys.exit(print("ok"))
    sys.exit(main())
