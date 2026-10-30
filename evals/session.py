"""One Claude Code session driven the way the desktop app drives it: a long-lived process, streaming JSON in and out,
the full built-in tool set, hooks firing on every turn, and a scripted user who answers the question tool and the
permission prompts. Safe by construction: a shell command runs only if every part of it matches the read-only allowlist
(plus Lantern's own commands); edits stay inside the probe folder; everything else is declined the way a user declines.

Usage: Session(cwd, sid).say("hello") ... .close(); the plugin under test is passed with plugin_dir so the installed copy
is not what gets measured. Needs `claude` 2.1+ (stream-json input, --permission-prompt-tool stdio)."""
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

MODEL = os.environ.get("WEEK_MODEL", "claude-opus-5-5")
TURN_SECONDS = 900
RAW = open(os.environ["WEEK_RAW"], "a") if os.environ.get("WEEK_RAW") else None  # every frame, for debugging the rig
SEGMENT = re.compile(r"\s*(?:\|\||&&|;|\||\n)\s*")
REDIRECT = re.compile(r"(?<![0-9&])>|>>|\d>(?!&1|/dev/null)")
PLAIN = {"ls", "cat", "head", "tail", "wc", "stat", "file", "df", "du", "free", "uptime", "date", "hostname", "id", "whoami", "env",
         "printenv", "which", "type", "command", "grep", "rg", "sort", "uniq", "tr", "cut", "awk", "echo", "jq", "ss", "realpath",
         "readlink", "basename", "dirname", "test", "true", "nproc", "lscpu", "lsblk", "mount", "ps", "pgrep", "ip", "flatpak",
         "rpm-ostree", "crontab", "getent", "timedatectl", "journalctl", "loginctl", "systemd-analyze", "cd", "pwd", "lantern",
         "lantern.py", "tree", "column", "paste", "diff", "cmp", "md5sum", "sha256sum", "nl", "tac", "rev", "seq", "printf", "sleep", "done", "fi",
         "last", "od", "xxd", "hexdump", "strings", "[", "sensors", "lsusb", "lspci", "uname"}
SUB = {"git": {"status", "log", "diff", "show", "remote", "rev-parse", "branch", "stash", "ls-files", "describe", "config", "tag", "worktree"},
       "systemctl": {"status", "show", "cat", "list-units", "list-timers", "list-unit-files", "is-active", "is-enabled", "is-failed",
                     "list-dependencies"},
       "podman": {"ps", "images", "inspect", "logs", "version", "info", "stats", "volume", "network", "top", "system", "healthcheck"},
       "distrobox": {"list", "ls"}, "docker": {"ps", "images", "inspect", "logs"},
       "virsh": {"list", "dominfo", "domstate", "domblklist", "domblkinfo", "dommemstat", "net-list", "pool-list", "vol-list"}}
CURL_WRITES = {"-X", "--request", "-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form", "-T", "--upload-file", "-o", "--output", "-O"}
WRAPPERS = {"timeout", "env", "nice", "distrobox-host-exec", "command", "xargs"}


def _ok(toks: list[str]) -> bool:
    while toks and re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):  # VAR=value prefixes
        toks = toks[1:]
    if not toks:
        return True
    if toks[0] == "for":  # `for x in a b; do cmd; done`: the header runs nothing, the body is checked as its own segments
        return "do" not in toks or _ok(toks[toks.index("do") + 1:])
    if toks[0] in ("do", "then", "else"):
        return _ok(toks[1:])
    prog = os.path.basename(toks[0])
    if prog == "command" and toks[1:2] == ["-v"]:
        return True  # asks which programs exist
    if prog in WRAPPERS:
        rest = [t for t in toks[1:] if not t.startswith("-") and not re.fullmatch(r"\d+[smh]?", t)]
        return _ok(rest)
    if prog == "uv":
        return any(t.endswith("lantern.py") for t in toks)  # Lantern's own commands write only to its memory
    if prog == "sed":
        return not any(t.startswith("-i") or t == "--in-place" for t in toks)
    if prog == "find":
        return not any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls") for t in toks)
    if prog == "sqlite3":  # only a read-only open: -readonly, or a file: URI with mode=ro / immutable=1
        return "-readonly" in toks or any(t.startswith("file:") and ("mode=ro" in t or "immutable=1" in t) for t in toks)
    if prog == "curl":
        drop = {i for i, t in enumerate(toks) if t == "/dev/null" and toks[i - 1] in ("-o", "--output")}
        toks = [t for i, t in enumerate(toks) if i not in drop and i + 1 not in drop]  # "-o /dev/null" writes nothing
        return not any(t in CURL_WRITES or t.startswith("-X") for t in toks)  # a GET is a read; -o /dev/null writes nothing
    if prog in SUB:
        words = [t for t in toks[1:] if not t.startswith("-") and "=" not in t]
        if (prog == "git" and "-C" in toks) or (prog == "virsh" and "-c" in toks):
            words = words[1:]  # the folder after git -C, the URI after virsh -c
        if prog == "git" and ("stash" in words or "worktree" in words):
            return words[1:2] == ["list"]
        if prog == "git" and "config" in words:
            return "--get" in toks or "--list" in toks or "-l" in toks
        if prog == "podman" and words and words[0] in ("volume", "network", "system"):
            return len(words) > 1 and words[1] in ("ls", "inspect", "df", "info")
        if prog == "systemctl" and not words:
            return True  # `systemctl --user --failed` lists units
        return bool(words) and words[0] in SUB[prog]
    return prog in PLAIN


def segments(command: str) -> list[list[str]]:
    """Token lists for each simple command, split on |, ||, &&, ; and newlines with quotes respected (grep -E 'a|b' is one
    token). Raises ValueError on unbalanced quotes."""
    out: list[list[str]] = []
    for line in command.replace("\\\n", " ").splitlines():  # a line continuation is one command
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        cur: list[str] = []
        for tok in lex:
            if tok in ("|", "||", "&&", ";", ";;"):
                out.append(cur)
                cur = []
            elif tok in ("(", ")"):
                continue
            else:
                cur.append(tok)
        out.append(cur)
    return [c for c in out if c]


def allowed(command: str) -> bool:
    """Every simple command must be a read-only program or Lantern itself; no redirections to files, no substitution."""
    if "$(" in command or "`" in command or "<(" in command:
        return False
    try:  # redirections are judged as tokens, so a quoted "->" or ">" inside a format string is not one
        parts = segments(re.sub(r"\d?>&\d|\d?>/dev/null|&>/dev/null", " ", command.strip()))  # harmless redirects
    except ValueError:
        return False
    names: dict[str, str] = {}
    for toks in parts:
        toks = [re.sub(r"\$\{?(\w+)\}?", lambda m: names.get(m.group(1), m.group(0)), t) for t in toks]
        if any(t in (">", ">>", "&>", ">&", ">|") or t.startswith(">") for t in toks):
            return False
        while toks and toks[0] in ("do", "then", "else", "done", "fi"):  # a loop or if around read-only commands is still read-only
            toks = toks[1:]
        if not toks or (toks[0] == "for" and toks[2:3] == ["in"]) or (toks[0] == "if" and _ok(toks[1:])):
            continue
        if len(toks) == 1 and (m := re.fullmatch(r"([A-Za-z_]\w*)=(.*)", toks[0], re.S)):
            names[m.group(1)] = m.group(2)  # NAME=value on its own, used as "$NAME" later
            continue
        if not _ok(toks):
            return False
    return True


def answer(question: dict) -> str:
    """The simulated user: confirms what the machine is, keeps the first guardrails, looks wider, declines phone approvals."""
    q = (question.get("header", "") + " " + question.get("question", "")).lower()
    labels = [o["label"] for o in question.get("options", [])]
    if not labels:
        return "Yes, that's right."
    def pick(pattern):
        return next((l for l in labels if re.search(pattern, l, re.I)), None)
    if re.search(r"phone|notif|ntfy|pushcut|approv", q):
        chosen = pick(r"\b(no|not now|skip|later)\b") or labels[-1]
    elif "plex" in q:
        chosen = pick(r"server|container|quadlet") or labels[0]  # the rule and the parents' stream are about the server
    elif re.search(r"\b(edit|apply|write|commit|make the change|fix it|go ahead|proceed|install|add .* to)\b", q):
        # the simulated user never approves changes to real files, so the answer must match what the policy will allow
        chosen = pick(r"show|draft|not now|later|no\b|skip|just tell") or labels[-1]
    elif question.get("multiSelect"):
        return ", ".join(labels[:2])
    else:
        chosen = pick(r"\b(yes|right|correct|wider|everything|all)\b") or labels[0]
    return chosen


class Session:
    def __init__(self, cwd: Path, sid: str, plugin_dir: Path | None = None, hooks_log: Path | None = None, max_turns: int = 40):
        self.cwd, self.sid, self.asked, self.turns = Path(cwd), sid, [], 0
        self.ran, self.denied = [], []  # every shell command approved or declined, for the safety audit
        self.hooks_log = hooks_log
        self.stops0 = self._stops()
        cmd = ["claude", "-p", "--model", MODEL, "--session-id", sid, "--setting-sources", "user,project,local",
               "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
               "--permission-prompt-tool", "stdio", "--tools", "default", "--disallowedTools", "WebFetch,WebSearch",
               "--strict-mcp-config", "--max-turns", str(max_turns)]
        if plugin_dir:  # measure the checkout, not the installed copy; the installed copies stay off so hooks fire once
            off = {"lantern@lantern": False, "lantern-tailscale@lantern": False, "lantern-media-server@lantern": False}
            cmd += ["--plugin-dir", str(plugin_dir), "--settings", json.dumps({"enabledPlugins": off})]
        env = dict(os.environ)
        if plugin_dir:  # the model's own `lantern` commands must hit the folder the hooks use (the trial plugin's), not the installed one's
            env["LANTERN_HOME"] = str(Path.home() / ".claude" / "plugins" / "data" / "lantern-inline")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, cwd=self.cwd, bufsize=1, env=env)

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _decide(self, req: dict) -> dict:
        tool, inp = req.get("tool_name", ""), req.get("input") or {}
        if tool == "AskUserQuestion":
            answers = {q["question"]: answer(q) for q in inp.get("questions", [])}
            self.asked += [f"{q} -> {a}" for q, a in answers.items()]
            return {"behavior": "allow", "updatedInput": {**inp, "answers": answers}}
        if tool == "Bash":
            if allowed(inp.get("command", "")):
                self.ran.append(inp.get("command", ""))
                return {"behavior": "allow", "updatedInput": inp}
            self.denied.append(inp.get("command", ""))
            return {"behavior": "deny", "message": "David did not approve that command. Plain read-only commands and Lantern's own "
                    "commands go through without asking; a loop, a substitution or anything that changes the machine does not. Use what "
                    "Lantern already told you, or try a simpler read; never hand the user a command to run."}
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = Path(inp.get("file_path") or inp.get("notebook_path") or "/")
            if self.cwd in path.resolve().parents:
                return {"behavior": "allow", "updatedInput": inp}
            return {"behavior": "deny", "message": "David has not approved changes to his real files; show the change and he will apply "
                    "it himself when ready. (Scratch work inside the current folder needs no approval.)"}
        if tool in ("Read", "Glob", "Grep", "Agent", "TodoWrite", "ExitPlanMode", "Skill"):
            return {"behavior": "allow", "updatedInput": inp}
        return {"behavior": "deny", "message": f"The user declined {tool} in this session."}

    def _stops(self) -> int:
        try:
            return sum('"hook": "stop"' in l for l in self.hooks_log.read_text().splitlines()) if self.hooks_log else 0
        except OSError:
            return 0

    def say(self, text: str) -> str:
        """One user turn; returns everything the assistant said in it (tool use and permission prompts are handled on the
        way). A Stop hook may send the model back for one more pass, and the user sees both parts."""
        self.turns += 1
        self._send({"type": "user", "message": {"role": "user", "content": text}})
        deadline = time.monotonic() + TURN_SECONDS
        said: list[str] = []
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read()[-800:]
                return f"(no reply: the session ended; stderr: {err})"
            if RAW:
                RAW.write(line)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "control_request":
                decision = self._decide(msg.get("request") or {})
                self._send({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"],
                                                                      "response": decision}})
            elif msg.get("type") == "assistant":
                said += [b.get("text", "") for b in (msg.get("message") or {}).get("content") or [] if b.get("type") == "text" and b.get("text", "").strip()]
            elif msg.get("type") == "result":
                final = msg.get("result") or f"({msg.get('subtype', 'no result')})"
                return "\n\n".join(said) if len(said) > 1 else final
        self.proc.kill()
        return "(no reply: the turn timed out)"

    def close(self) -> None:
        """End the session the way closing the app does, after the async Stop hook has had its chance to run."""
        if self.hooks_log:
            for _ in range(60):
                if self._stops() >= self.stops0 + self.turns:
                    break
                time.sleep(0.5)
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


if __name__ == "__main__":  # self-check of the allowlist and the scripted user
    assert allowed("systemctl --user status plex.service | head -5")
    assert allowed("uv run --no-project --quiet /x/lantern.py remember --rule 'a'")
    assert allowed("journalctl --user -u lantern-sim-fail.service -n 20 2>/dev/null") and allowed("git -C ~/x status --porcelain")
    assert not allowed("systemctl --user restart plex.service") and not allowed("echo hi > /tmp/x") and not allowed("ls; rm -rf x")
    assert not allowed("cat $(find / -name secret)") and not allowed("sed -i s/a/b/ f") and not allowed("find . -name x -delete")
    assert allowed('L="/x/lantern.py"\n"$L" remember --profile "a b"\n"$L" remember --rule "c"') and allowed("git -C ~/x --no-optional-locks log --oneline '@{u}..HEAD' | head")
    assert allowed('git -C "$HOME/Documents/Ch at" status --short 2>&1 | head') and not allowed("git -C ~/x push") and not allowed("git stash")
    assert allowed("curl -s localhost:32400/status/sessions | head -3") and not allowed("curl -X POST localhost:1/x") and allowed("virsh list --all")
    assert allowed("grep -iH -E 'AutoUpdate|Image=|Restart' ~/.config/containers/systemd/*plex* 2>&1") and allowed("grep -ril plex ~/.config | xargs -r grep -iH -E 'a|b'")
    assert not allowed("ls | xargs rm") and not allowed("echo x &> f") and allowed("systemctl --user list-units --all --no-pager 2>/dev/null | grep -i plex")
    assert allowed("for c in a b; do virsh -c $c list --all 2>/dev/null; virsh -c $c domblklist Nyx; done") and not allowed("for f in *; do rm $f; done")
    assert allowed("curl -s -m 5 -o /dev/null -w '%{http_code}' http://localhost:32400/identity") and not allowed("curl -o out.bin http://x") and allowed("git worktree list") and not allowed("git worktree add x")
    assert allowed("command -v restic borg rclone") and allowed("podman system df") and not allowed("podman system prune") and allowed("[ -f $ex ] && cat $ex")
    assert allowed("podman inspect plex --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'") and not allowed("echo x > f") and not allowed("cat a >> b")
    assert allowed("sqlite3 -readonly ~/x/memory.db 'select count(*) from notes'") and allowed("sqlite3 'file:/x/m.db?immutable=1' .tables") and not allowed("sqlite3 /x/m.db 'delete from notes'")
    assert allowed("timeout 5 systemctl --user list-timers --all --no-pager") and not allowed("podman image rm x") and not allowed("python3 -c 'print(1)'")
    assert allowed('L="/x/lantern.py"\n"$L" remember --profile "a b." && \\\n"$L" remember --rule "c"')  # line continuation
    assert allowed("curl -s -o /dev/null -w '%{http_code}' http://localhost:32400/identity") and not allowed("curl -s -o /tmp/x http://localhost:1/")
    assert allowed("systemctl --user --failed --no-pager 2>&1 | tail") and allowed("uptime -s; last -x -n 6 | head") and allowed("od -A d -N 32 f; cmp f /dev/zero")
    assert allowed('for u in a.service b.service; do echo "=== $u"; systemctl status $u --no-pager 2>&1 | sed -n "3,8p"; journalctl -u $u -n 15 --no-pager | tail -15; done')
    assert not allowed("for u in a b; do systemctl restart $u; done") and allowed("if systemctl --user is-active plex.service; then echo up; else echo down; fi")
    assert answer({"question": "Which guardrails?", "multiSelect": True, "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]}) == "A, B"
    assert answer({"question": "Send approvals to your phone?", "options": [{"label": "Yes"}, {"label": "Not now"}]}) == "Not now"
    assert answer({"question": "Is this right?", "options": [{"label": "Change it"}, {"label": "Yes, that's right"}]}) == "Yes, that's right"
    assert answer({"question": "Which Plex is stuck?", "options": [{"label": "Desktop app"}, {"label": "Plex Media Server (container)"}]}).startswith("Plex Media Server")
    assert answer({"question": "Apply the backup edit now?", "options": [{"label": "Yes, everything"}, {"label": "Show me the change first"}]}) == "Show me the change first"
    assert answer({"question": "Which guardrails should I keep?", "multiSelect": True, "options": [{"label": "A"}, {"label": "B"}]}) == "A, B"
    print("ok")
