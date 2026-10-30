"""The packs: each skill is read-first, hands the user no terminal step, references secrets by name, and the guards
enforce what the skill text promises."""
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import shield  # noqa: E402

PLUGINS = Path(__file__).resolve().parents[2]
MARKETPLACE = json.loads((PLUGINS.parent / ".claude-plugin" / "marketplace.json").read_text())
PACKS = sorted(p for p in PLUGINS.glob("lantern-*") if p.is_dir())
USER_STEP = re.compile(r"(?:ask|tell|have|get) the user to (?:run|type|paste|execute)|the user (?:runs|types|pastes|executes)"
                       r"|run (?:this|the following)|open a terminal|```(?:bash|sh|shell|mermaid)", re.I)


def skill(pack: Path) -> tuple[dict, str, dict[str, str]]:
    """Frontmatter, body, and the body's sections keyed by heading."""
    path = next(pack.glob("skills/*/SKILL.md"))
    _, front, body = path.read_text().split("---\n", 2)
    meta = dict(line.split(":", 1) for line in front.splitlines() if ":" in line)
    meta = {k.strip(): v.strip() for k, v in meta.items()} | {"skill_dir": path.parent.name}
    sections, key = {}, "top"
    for line in body.splitlines():
        if line.startswith("## "):
            key = line[3:].strip()
        sections[key] = sections.get(key, "") + line + "\n"
    return meta, body, sections


def guard(pack: Path):
    spec = importlib.util.spec_from_file_location(f"{pack.name}_guard", pack / "guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def backticked(text: str, prefix: str) -> list[str]:
    return [c for c in re.findall(r"`([^`]+)`", text) if c.startswith(prefix)]


class EveryPackTest(unittest.TestCase):
    def test_manifest_matches_folder_and_marketplace(self):
        listed = {p["name"]: p["source"] for p in MARKETPLACE["plugins"]}
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                manifest = json.loads((pack / ".claude-plugin" / "plugin.json").read_text())
                self.assertEqual(manifest["name"], pack.name)
                self.assertEqual(listed.get(pack.name), f"./plugins/{pack.name}")
                meta, body, _ = skill(pack)
                self.assertEqual(meta["name"], meta["skill_dir"])
                self.assertTrue(10 < len(meta["description"]) <= 1024)
                self.assertRegex(meta["description"], r"Use (?:when|for) ")

    def test_read_first_then_change_and_ask(self):
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                _, body, sections = skill(pack)
                read = next(k for k in sections if k.lower().startswith(("read first", "allowed")))
                change = next(k for k in sections if k.lower().startswith(("changing", "never run")))
                self.assertLess(body.index(read), body.index(change))
                self.assertRegex(sections[change] + sections.get("top", ""), r"ask|explicit yes|let the user")
                self.assertRegex(body, r"explicit yes|let the user make it themselves")

    def test_no_terminal_step_for_the_user_and_no_mermaid(self):
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                _, body, _ = skill(pack)
                self.assertIsNone(USER_STEP.search(body), USER_STEP.search(body) and USER_STEP.search(body).group())
                self.assertNotIn("mermaid", body.lower())
                self.assertRegex(body, r"never runs\s+a command")

    def test_secrets_by_name_only(self):
        for pack in PACKS:
            with self.subTest(pack=pack.name):
                _, body, _ = skill(pack)
                self.assertEqual(shield.find(body), [])
                for ref in re.findall(r"secret (\S+): (<\S+>)", body):
                    self.assertRegex(ref[0], r"^[A-Z][A-Z0-9_]+$")
                    self.assertEqual(ref[1], "<value>")
                self.assertNotRegex(body, r"(?i)(api[_ ]?key|token|password)\s*[:=]\s*[A-Za-z0-9]{12,}")

    def test_guards_are_wired_and_pass_their_self_test(self):
        for pack in [p for p in PACKS if (p / "guard.py").exists()]:
            with self.subTest(pack=pack.name):
                hook = json.loads((pack / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"][0]
                self.assertEqual(hook["matcher"], "Bash")
                self.assertTrue(hook["hooks"][0]["args"][-1].endswith("/guard.py"))
                out = subprocess.run([sys.executable, str(pack / "guard.py"), "--self-test"], capture_output=True, text=True)
                self.assertEqual((out.returncode, out.stdout.strip()), (0, "ok"), out.stderr)


class TailscalePackTest(unittest.TestCase):
    pack = PLUGINS / "lantern-tailscale"

    def test_skill_text_and_guard_agree(self):
        g = guard(self.pack)
        _, _, sections = skill(self.pack)
        allowed = backticked(sections["Allowed, because they only read"], "tailscale ") + ["systemctl status tailscaled", "journalctl -u tailscaled -n 100"]
        never = backticked(next(v for k, v in sections.items() if k.startswith("Never run")), "")
        verbs = [never[0].split()[1]] + never[1:]
        self.assertGreaterEqual(len(allowed), 8)
        self.assertGreaterEqual(len(verbs), 12)
        for cmd in allowed:
            self.assertIsNone(g.check(cmd.replace("<peer>", "nyx").replace("<ip>", "100.64.0.1")), cmd)
        for verb in verbs:
            if verb != "tailscaled":
                self.assertIsNotNone(g.check(f"sudo tailscale {verb} --accept-routes"), verb)
        for cmd in ("systemctl restart tailscaled", "sudo systemctl stop tailscaled.service", "service tailscaled start"):
            self.assertIsNotNone(g.check(cmd), cmd)

    def test_hook_denies_through_stdin(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "tailscale up --ssh"}})
        out = subprocess.run([sys.executable, str(self.pack / "guard.py")], input=payload, capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


class DockerPackTest(unittest.TestCase):
    pack = PLUGINS / "lantern-docker"

    def test_skill_text_and_guard_agree(self):
        g = guard(self.pack)
        _, body, sections = skill(self.pack)
        never = next(v for k, v in sections.items() if k.startswith("Never without"))
        for cmd in backticked(never, ""):
            for engine in ("docker", "podman"):
                full = f"{engine} compose down {cmd}" if cmd.startswith("-") else f"{engine} {'compose ' if cmd.startswith('down') else ''}{cmd} x"
                self.assertIsNotNone(g.check(full), full)
        for cmd in backticked(sections["Read first, in this order"], ""):
            if cmd.split()[0] in ("ps", "inspect", "logs", "stats", "images", "info"):
                self.assertIsNone(g.check(f"podman {cmd}"), cmd)
        for cmd in ("docker compose up -d", "podman restart web", "systemctl --user restart container-web"):
            self.assertIsNone(g.check(cmd), cmd)
        self.assertIn("guard asks the user", body)

    def test_hook_asks_through_stdin(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "docker compose down -v"}})
        out = subprocess.run([sys.executable, str(self.pack / "guard.py")], input=payload, capture_output=True, text=True)
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_exit_codes_explained(self):
        _, body, _ = skill(self.pack)
        for code in ("137", "OOMKilled", "125", "RestartCount", "RestartPolicy", "Health"):
            self.assertIn(code, body)


class MediaServerPackTest(unittest.TestCase):
    pack = PLUGINS / "lantern-media-server"

    def test_pipeline_in_order_and_every_app_has_a_key_source(self):
        meta, body, sections = skill(self.pack)
        tree = sections["Map the pipeline before diagnosing"]
        hops = ["request", "Sonarr", "Prowlarr", "download client", "import", "Plex / Jellyfin", "playback"]
        self.assertEqual(hops, sorted(hops, key=tree.index))
        keys = sections["Keys and hosts, by reference only"]
        for app in ("Plex", "Jellyfin", "Sonarr", "Radarr", "Prowlarr", "Overseerr", "Tautulli", "Tdarr", "Bazarr", "SABnzbd", "qBittorrent"):
            self.assertIn(app, keys, app)
        self.assertLess(body.index("Space"), body.index("/api/v3/queue"))  # disk before anything else
        for state in ("importPending", "importBlocked", "failedPending", "TranscodeSession", "hardlink"):
            self.assertIn(state, body)
        self.assertRegex(body, r"[Nn]ever show a key")


class HomeAssistantPackTest(unittest.TestCase):
    pack = PLUGINS / "lantern-home-assistant"

    def test_reads_the_api_and_guards_safety_devices(self):
        _, body, sections = skill(self.pack)
        for endpoint in ("/api/`", "/api/config", "/api/error_log", "/api/states", "/api/services"):
            self.assertIn(endpoint, body)
        self.assertIn("secret HA_TOKEN: <value>", body)
        change = sections["Changing things"]
        for device in ("Locks", "alarm", "garage", "covers"):
            self.assertIn(device, change)
        self.assertRegex(change, r"explicit yes every\s+time")
        self.assertIn("Never edit `.storage/`", change)


class UnifiPackTest(unittest.TestCase):
    pack = PLUGINS / "lantern-unifi"

    def test_both_console_kinds_and_connect_order(self):
        _, body, sections = skill(self.pack)
        for s in ("8443", "/proxy/network", "secret UNIFI_USER: <value>", "secret UNIFI_API_KEY: <value>", "stat/health", "stat/sta", "rest/wlanconf", "rest/firewallrule"):
            self.assertIn(s, body)
        steps = re.findall(r"^\d+\. ", sections['"It can\'t connect": read in this order'], re.M)
        self.assertEqual(steps, [f"{i}. " for i in range(1, len(steps) + 1)])
        self.assertGreaterEqual(len(steps), 5)
        self.assertIn("associated", sections['"It can\'t connect": read in this order'].split("2. ")[0])
        self.assertIn("remote access", sections["Changing things"])
        self.assertIn("same step as anything else", sections["Changing things"])


if __name__ == "__main__":
    unittest.main()
