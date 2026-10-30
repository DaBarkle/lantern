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
