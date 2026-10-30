"""PreToolUse guard: ask the user before a container command that destroys data. Reading and restarting pass through."""
import json
import re
import sys

ENGINE = r"\b(?:docker|podman)(?:-compose)?\b(?:\s+(?:compose|-\S+(?:\s+[^-\s]\S*)?))*\s+"
DESTROY = re.compile(ENGINE + r"(?:(?:container|image|volume|network|system|buildx)\s+)?(?:rm[if]?|prune|reset)\b"
                     + "|" + ENGINE + r"down\b[^;&|]*(?:\s-v\b|--volumes)")


def check(command: str) -> str | None:
    return ("Lantern's container pack: this command deletes containers, images or volumes. Confirm with the user first."
            if DESTROY.search(command or "") else None)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    reason = check((payload.get("tool_input") or {}).get("command", ""))
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        for bad in ("docker rm -f web", "podman system prune -af", "docker compose down -v", "docker-compose down --volumes",
                    "podman volume rm data", "docker image prune", "sudo docker rmi nginx", "podman system reset --force",
                    "podman --remote container rm x", "docker volume prune -f"):
            assert check(bad), bad
        for ok in ("docker ps -a", "podman logs --tail 100 web", "docker compose down", "docker compose up -d",
                   "podman restart web", "docker inspect web", "rm -rf /tmp/x", "docker volume ls", "podman stats --no-stream", "docker run --rm -it alpine sh", "docker compose down --remove-orphans"):
            assert not check(ok), ok
        sys.exit(print("ok"))
    sys.exit(main())
