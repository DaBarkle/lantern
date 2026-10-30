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
         "lantern.py", "tree", "column", "paste", "diff", "md5sum", "sha256sum", "nl", "tac", "rev", "seq", "printf", "sleep"}
SUB = {"git": {"status", "log", "diff", "show", "remote", "rev-parse", "branch", "stash", "ls-files", "describe", "config", "tag"},
       "systemctl": {"status", "show", "cat", "list-units", "list-timers", "list-unit-files", "is-active", "is-enabled", "is-failed",
                     "list-dependencies"},
       "podman": {"ps", "images", "inspect", "logs", "version", "info", "stats", "volume", "network", "top"},
       "distrobox": {"list", "ls"}, "docker": {"ps", "images", "inspect", "logs"},
       "virsh": {"list", "dominfo", "domstate", "domblklist", "domblkinfo", "dommemstat", "net-list", "pool-list", "vol-list"}}
CURL_WRITES = {"-X", "--request", "-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form", "-T", "--upload-file", "-o", "--output", "-O"}
WRAPPERS = {"timeout", "env", "nice", "distrobox-host-exec", "command", "xargs"}


def _ok(toks: list[str]) -> bool:
    while toks and re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):  # VAR=value prefixes
        toks = toks[1:]
    if not toks:
        return True
    prog = os.path.basename(toks[0])
    if prog in WRAPPERS:
        rest = [t for t in toks[1:] if not t.startswith("-") and not re.fullmatch(r"\d+[smh]?", t)]
        return _ok(rest)
    if prog == "uv":
        return any(t.endswith("lantern.py") for t in toks)  # Lantern's own commands write only to its memory
    if prog == "sed":
        return not any(t.startswith("-i") or t == "--in-place" for t in toks)
    if prog == "find":
        return not any(t in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls") for t in toks)
    if prog == "curl":
        return not any(t in CURL_WRITES or t.startswith("-X") for t in toks)  # a GET is a read
    if prog in SUB:
        words = [t for t in toks[1:] if not t.startswith("-") and "=" not in t]
        if (prog == "git" and "-C" in toks) or (prog == "virsh" and "-c" in toks):
            words = words[1:]  # the folder after git -C, the URI after virsh -c
        if prog == "git" and "stash" in words:
            return words[:2] == ["stash", "list"]
        if prog == "git" and "config" in words:
            return "--get" in toks or "--list" in toks or "-l" in toks
        if prog == "podman" and words and words[0] in ("volume", "network"):
            return len(words) > 1 and words[1] in ("ls", "inspect")
        return bool(words) and words[0] in SUB[prog]
    return prog in PLAIN


def segments(command: str) -> list[list[str]]:
    """Token lists for each simple command, split on |, ||, &&, ; and newlines with quotes respected (grep -E 'a|b' is one
    token). Raises ValueError on unbalanced quotes."""
    out: list[list[str]] = []
    for line in command.splitlines():
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
    if REDIRECT.search(command) or "$(" in command or "`" in command or "<(" in command:
        return False
    try:
        parts = segments(re.sub(r"\d?>&\d|\d?>/dev/null|&>/dev/null", " ", command.strip()))  # harmless redirects
    except ValueError:
        return False
    names: dict[str, str] = {}
    for toks in parts:
        toks = [re.sub(r"\$\{?(\w+)\}?", lambda m: names.get(m.group(1), m.group(0)), t) for t in toks]
        if any(t in (">", ">>", "&>", ">&") for t in toks):
            return False
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
    elif question.get("multiSelect"):
        return ", ".join(labels[:2])
    else:
        chosen = pick(r"\b(yes|right|correct|wider|everything|all)\b") or labels[0]
    return chosen


class Session:
    def __init__(self, cwd: Path, sid: str, plugin_dir: Path | None = None, hooks_log: Path | None = None, max_turns: int = 40):
        self.cwd, self.sid, self.asked, self.turns = Path(cwd), sid, [], 0
        self.hooks_log = hooks_log
        self.stops0 = self._stops()
        cmd = ["claude", "-p", "--model", MODEL, "--session-id", sid, "--setting-sources", "user,project,local",
               "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
               "--permission-prompt-tool", "stdio", "--tools", "default", "--disallowedTools", "WebFetch,WebSearch",
               "--strict-mcp-config", "--max-turns", str(max_turns)]
        if plugin_dir:  # measure the checkout, not the installed copy; the installed copies stay off so hooks fire once
            off = {"lantern@lantern": False, "lantern-tailscale@lantern": False, "lantern-media-server@lantern": False}
            cmd += ["--plugin-dir", str(plugin_dir), "--settings", json.dumps({"enabledPlugins": off})]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, cwd=self.cwd, bufsize=1)

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
                return {"behavior": "allow", "updatedInput": inp}
            return {"behavior": "deny", "message": "David declined this one (it would change the machine). He allows read-only "
                    "commands and Lantern's own commands without asking. Say what you would have run and why; do not call this a hook."}
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            path = Path(inp.get("file_path") or inp.get("notebook_path") or "/")
            if self.cwd in path.resolve().parents:
                return {"behavior": "allow", "updatedInput": inp}
            return {"behavior": "deny", "message": f"David declined this edit; today he only wants edits inside {self.cwd}. Show the change instead."}
        if tool in ("Read", "Glob", "Grep", "Agent", "TodoWrite", "ExitPlanMode", "Skill"):
            return {"behavior": "allow", "updatedInput": inp}
        return {"behavior": "deny", "message": f"The user declined {tool} in this session."}

    def _stops(self) -> int:
        try:
            return sum('"hook": "stop"' in l for l in self.hooks_log.read_text().splitlines()) if self.hooks_log else 0
        except OSError:
            return 0

    def say(self, text: str) -> str:
        """One user turn; returns the final reply (tool use and permission prompts are handled on the way)."""
        self.turns += 1
        self._send({"type": "user", "message": {"role": "user", "content": text}})
        deadline = time.monotonic() + TURN_SECONDS
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
            elif msg.get("type") == "result":
                return msg.get("result") or f"({msg.get('subtype', 'no result')})"
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
    assert allowed("timeout 5 systemctl --user list-timers --all --no-pager") and not allowed("podman image rm x") and not allowed("python3 -c 'print(1)'")
    assert answer({"question": "Which guardrails?", "multiSelect": True, "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]}) == "A, B"
    assert answer({"question": "Send approvals to your phone?", "options": [{"label": "Yes"}, {"label": "Not now"}]}) == "Not now"
    assert answer({"question": "Is this right?", "options": [{"label": "Change it"}, {"label": "Yes, that's right"}]}) == "Yes, that's right"
    print("ok")
