import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import discovery, doctor, memory, stage  # noqa: E402


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(Path(self.tmp.name) / "data")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_repos_found_without_descending_into_them(self):
        root = Path(self.tmp.name) / "home"
        for d in ["code/app/.git", "code/app/sub/.git", "node_modules/x/.git", ".hidden/y/.git", "a/b/c/d/.git"]:
            (root / d).mkdir(parents=True)
        self.assertEqual([f["name"] for f in discovery.scout_repos(root)], ["app"])

    def test_depth_one_scouts_nothing(self):
        with mock.patch.object(discovery, "scout_repos", side_effect=AssertionError("scouted at depth 1")):
            self.assertEqual(discovery.discover(1), [])

    def test_accept_replaces_card_and_doctor_checks_it(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        find = {"id": "port:x", "kind": "local-service", "name": "Plex", "detail": "answering", "check": {"type": "port", "port": port}}
        discovery.accept([find])
        discovery.accept([{**find, "detail": "answering again"}])
        db = memory.connect()
        cards = [t for (t,) in db.execute("SELECT text FROM items WHERE source='discovery:port:x' AND status='active'")]
        self.assertEqual(len(cards), 1)
        self.assertIn("again", cards[0])
        self.assertEqual(dict((n, s) for n, s, _ in doctor.run_all())["cap:port:x"], doctor.OK)
        srv.close()
        self.assertEqual(dict((n, s) for n, s, _ in doctor.run_all())["cap:port:x"], doctor.WARN)
        self.assertIn("doctor/cap:port:x", [e["key"] for e in stage.top(db, k=9)])
        db.close()

    def test_depth_setting_persists(self):
        self.assertEqual(discovery.settings()["depth"], 2)  # workspace by default
        discovery.cli(["depth", "3"])
        self.assertEqual(discovery.settings()["depth"], 3)


class HostExecTest(unittest.TestCase):
    def test_commands_missing_in_a_container_go_to_the_host(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(stdout="plex\tdocker.io/plex\n", returncode=0)
        with mock.patch.object(discovery, "HOST_EXEC", "/usr/bin/distrobox-host-exec"), \
                mock.patch.object(discovery.shutil, "which", return_value=None), \
                mock.patch.object(discovery.subprocess, "run", side_effect=fake):
            self.assertEqual(discovery._which("podman"), "plex\tdocker.io/plex")  # whatever the host answers
            discovery._run(["podman", "ps"])
        self.assertEqual(calls[-1][:2], ["/usr/bin/distrobox-host-exec", "podman"])
        # systemctl exists inside the container too, but only the host's can see the user's services
        with mock.patch.object(discovery, "HOST_EXEC", "/usr/bin/distrobox-host-exec"), \
                mock.patch.object(discovery.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(discovery.subprocess, "run", side_effect=fake):
            discovery._run(["systemctl", "--user", "list-units"])
        self.assertEqual(calls[-1][:2], ["/usr/bin/distrobox-host-exec", "systemctl"])


class SignalsTest(unittest.TestCase):
    def test_alert_channels_readme_and_expertise(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(discovery.Path, "home", return_value=Path(d)):
            units = Path(d) / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            (units / "guardian.service").write_text("[Service]\nExecStart=/x/guardian --notify https://ntfy.sh/topic\n")
            found = discovery.scout_alerts()
            self.assertEqual([f["name"] for f in found], ["ntfy"])
            repo = Path(d) / "proj"; repo.mkdir()
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "README.md").write_text("# Proj\n\nA tool that makes AirPods work on Linux.\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"], check=True)
            discovery.accept([{"id": f"repo:{repo}", "kind": "repo", "name": "proj", "detail": "", "check": None}]
                             + [{"id": f"tool:{t}", "kind": "tool", "name": t, "detail": "", "check": None} for t in "abcdefgh"])
            self.assertIn("proj: Proj A tool that makes AirPods work on Linux.", discovery.readme_snippets()[0])
            from lantern_core import register
            self.assertEqual(register.level(), "plain")  # eight dev tools say nothing about the reader; only their words do


class PlatformTest(unittest.TestCase):
    """macOS and Windows scouts, with the platform and its commands mocked (the suite runs on Linux)."""

    def test_macos_launchd_jobs_and_brew_casks(self):
        with tempfile.TemporaryDirectory() as d:
            import plistlib
            agents = Path(d) / "agents"; daemons = Path(d) / "daemons"
            agents.mkdir(); daemons.mkdir()
            (agents / "com.user.sync.plist").write_bytes(plistlib.dumps({"Label": "com.user.sync", "StartCalendarInterval": {"Hour": 3}}))
            (agents / "com.apple.foo.plist").write_bytes(plistlib.dumps({"Label": "com.apple.foo"}))
            (daemons / "com.user.plex.plist").write_bytes(plistlib.dumps({"Label": "com.user.plex", "KeepAlive": True}))
            (daemons / "broken.plist").write_text("not a plist")
            calls = []

            def run(cmd):
                calls.append(cmd)
                return {"brew": "firefox\nvlc\n", "launchctl": "state = running\n"}.get(cmd[0], "")
            with mock.patch.object(discovery, "PLATFORM", "mac"), mock.patch.object(discovery, "_run", run), \
                    mock.patch.object(discovery, "LAUNCHD_DIRS", {str(agents): "gui", str(daemons): "system"}):
                timers, services, apps = discovery.scout_timers(), discovery.scout_services(), discovery.scout_apps()
                self.assertEqual([t["name"] for t in timers], ["com.user.sync"])
                self.assertEqual([s["name"] for s in services], ["com.user.plex"])
                self.assertEqual(services[0]["check"], {"type": "launchd", "target": "system/com.user.plex"})
                self.assertIn("daemon", services[0]["detail"])
                self.assertEqual([a["name"] for a in apps], ["firefox", "vlc"])
                self.assertTrue(discovery.healthy(services[0]["check"]))
            self.assertEqual(calls[-1], ["launchctl", "print", "system/com.user.plex"])

    def test_windows_services_tasks_and_winget(self):
        outputs = {
            "powershell": '[{"Name":"PlexService","PathName":"\\"C:\\\\Program Files\\\\Plex\\\\Plex.exe\\""},'
                          '{"Name":"Spooler","PathName":"C:\\\\WINDOWS\\\\System32\\\\spoolsv.exe"}]',
            "schtasks": '"\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","N/A","Ready"\n"\\Backup nightly","1/11/2026 3:00:00 AM","Ready"\n',
            "winget": "   - \n"
                      "Name                 Id                     Version    Available  Source\n"
                      "------------------------------------------------------------------------\n"
                      "Git                  Git.Git                2.44.0                winget\n"
                      "NVIDIA Graphics Driver {B2FE1952-0186}       552.22\n"
                      "Spotify              Spotify.Spotify        1.2.3      1.2.4      msstore\n",
            "sc": "SERVICE_NAME: PlexService\n        STATE              : 4  RUNNING\n",
        }
        with mock.patch.object(discovery, "PLATFORM", "windows"), mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}), \
                mock.patch.object(discovery, "_run", lambda cmd: outputs.get(cmd[0], "")):
            services, timers, apps = discovery.scout_services(), discovery.scout_timers(), discovery.scout_apps()
            self.assertEqual([s["name"] for s in services], ["PlexService"])
            self.assertEqual(services[0]["check"], {"type": "winservice", "name": "PlexService"})
            self.assertEqual([t["name"] for t in timers], ["\\Backup nightly"])
            self.assertEqual([(a["name"], a["id"]) for a in apps], [("Git", "app:Git.Git"), ("Spotify", "app:Spotify.Spotify")])
            self.assertTrue(discovery.healthy(services[0]["check"]))
        with mock.patch.object(discovery, "PLATFORM", "windows"), mock.patch.object(discovery, "_run", lambda cmd: ""):
            self.assertEqual(discovery.scout_services() + discovery.scout_timers() + discovery.scout_apps(), [])  # nothing answered


class SettleSetupTest(unittest.TestCase):
    def test_answers_given_through_the_question_tool_settle_the_pending_setup(self):
        import lantern
        from lantern_core import discovery
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            discovery.save_settings({"depth": 2, "pending": ["purpose", "guardrails", "wider"]})
            self.assertEqual(lantern.memory_cli(["remember", "--profile", "a home server and gaming PC"]), 0)
            self.assertEqual(lantern.memory_cli(["remember", "--rule", "ask before restarting anything"]), 0)
            self.assertEqual(discovery.settings()["pending"], ["wider"])
            lantern.settle_setup("wider")
            self.assertEqual(discovery.settings()["pending"], [])
            self.assertIn("answered_at", discovery.settings())
