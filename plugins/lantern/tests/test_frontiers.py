"""Frontiers, bridges and Lantern's own SSH key: walls are findings, secrets are read through the manager's CLI at run
time, and a host is reached after one tap on the phone."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import approvals, discovery, shield, sshkey  # noqa: E402

NOT_READY = {"managers": [{"scheme": "op", "tool": "op", "name": "1Password", "ready": False,
                           "why": "the 1Password CLI is installed but no account is added", "ask": "add it once"}]}


class FrontierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(Path(self.tmp.name) / "data")})
        self.env.start()
        discovery.save_settings({"depth": 3, "capabilities": {
            "ssh:nyx": {"name": "nyx", "check": None},
            "port:8989": {"name": "Sonarr", "check": {"type": "port", "port": 8989}},
            "port:8265": {"name": "Tdarr", "check": {"type": "port", "port": 8265}}}})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_walls_close_by_themselves(self):
        with mock.patch.object(shield, "manager_status", return_value=NOT_READY):
            fs = discovery.real_frontiers()
        self.assertEqual([f["name"] for f in fs], ["1Password", "nyx", "Sonarr"])  # Tdarr has no key to ask for
        self.assertTrue(all(f["why"] and f["unlocks"] and f["ask"] for f in fs))
        # the key is handed over, the host is granted, the manager signs in: nothing is left to ask
        (discovery.ensure_data_dir() / "secret-names.json").write_text(json.dumps(["SONARR_API_KEY"]))
        discovery.save_settings({**discovery.settings(), "ssh_grants": {"nyx": 1.0}})
        with mock.patch.object(shield, "manager_status", return_value={"managers": [{**NOT_READY["managers"][0], "ready": True}]}):
            self.assertEqual(discovery.real_frontiers(), [])

    def test_shown_greyed_and_raised_only_when_touched(self):
        with mock.patch.object(shield, "manager_status", return_value=NOT_READY), \
                mock.patch.object(discovery, "frontiers", discovery.real_frontiers):
            discovery.refresh_frontiers()
        self.assertIn("(not reachable yet)", discovery.frontier_block())
        self.assertIn("Sonarr: it answers locally", discovery.frontier_block())
        self.assertEqual(len(discovery.frontiers_touching("why is the sonarr queue stuck")), 1)
        self.assertIn("secret ask SONARR_API_KEY", discovery.frontiers_touching("sonarr")[0])
        self.assertEqual(discovery.frontiers_touching("how is the disk doing"), [])

    def test_depth_two_is_itself_a_frontier(self):
        discovery.save_settings({"depth": 2})
        with mock.patch.object(shield, "manager_status", return_value={"managers": []}):
            self.assertIn("look wider", discovery.real_frontiers()[0]["ask"])

    def test_a_refusal_while_scouting_is_a_wall(self):
        discovery.WALLS.clear()
        refused = mock.Mock(returncode=1, stdout="", stderr="permission denied while trying to connect to the Docker daemon socket")
        with mock.patch.object(discovery.subprocess, "run", return_value=refused), mock.patch.object(discovery, "HOST_EXEC", None):
            discovery._run(["docker", "ps"])
            discovery._run(["docker", "ps", "-a"])
        self.assertEqual(len(discovery.WALLS), 1)
        self.assertIn("docker", discovery.WALLS[0]["why"])
        discovery.WALLS.clear()


class FakeKeyring:
    def __init__(self, **values):
        self.values = values

    def get_password(self, service, name):
        return self.values.get(name)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(Path(self.tmp.name) / "data")}, clear=False)
        self.env.start()
        self.calls = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def fake_run(self, cmd, **kw):
        self.calls.append((cmd, kw.get("input"), kw.get("env", {})))
        return mock.Mock(returncode=0, stdout="s3cret-value\n", stderr="")

    def test_each_manager_is_read_through_its_own_cli(self):
        kr = FakeKeyring(BW_SESSION="bwsess", KEEPASSXC_PASSWORD="dbpass")
        with mock.patch.object(shield.subprocess, "run", side_effect=self.fake_run):
            for ref in ("op://Private/Plex/token", "bw://Plex/username", "pass://media/plex", "keepass:///home/me/v.kdbx/Media/Plex"):
                self.assertEqual(shield._read(ref, kr), "s3cret-value")
            self.assertIsNone(shield._read("ftp://nope", kr))
        self.assertEqual([c[0][:2] for c in self.calls], [["op", "read"], ["bw", "get"], ["pass", "show"], ["keepassxc-cli", "show"]])
        self.assertEqual(self.calls[1][0][2:], ["username", "Plex"])
        self.assertEqual(self.calls[1][2]["BW_SESSION"], "bwsess")  # the session key comes from the keychain, not the prompt
        self.assertEqual(self.calls[3][0][-2:], ["/home/me/v.kdbx", "Media/Plex"])
        self.assertEqual(self.calls[3][1], "dbpass")  # the database password goes in on stdin
        self.assertIsNone(shield._read("keepass:///home/me/v.kdbx/x", FakeKeyring()))  # locked: nothing to type, nothing read

    def test_link_accepts_known_managers_only(self):
        self.assertEqual(shield.cli(["link", "PLEX", "bw://Plex"]), 0)
        self.assertEqual(shield.links(), {"PLEX": "bw://Plex"})
        self.assertEqual(shield.cli(["link", "X", "ftp://x"]), 2)

    def test_status_says_why_a_manager_cannot_answer(self):
        with mock.patch.object(shield.shutil, "which", lambda t: "/usr/bin/op" if t == "op" else None), \
                mock.patch.object(shield.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")):
            st = shield.manager_status()
        self.assertEqual([(m["tool"], m["ready"]) for m in st["managers"]], [("op", False)])
        self.assertIn("no account", st["managers"][0]["why"])
        self.assertIn("op account add", st["managers"][0]["ask"])


class SshKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(Path(self.tmp.name) / "data")})
        self.env.start()
        self.key = mock.patch.object(sshkey, "KEY", Path(self.tmp.name) / "ssh" / "lantern_ed25519")
        self.key.start()
        self.calls = []

    def tearDown(self):
        self.key.stop()
        self.env.stop()
        self.tmp.cleanup()

    def fake_run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "ssh-keygen":
            sshkey.KEY.write_text("key")
        return mock.Mock(returncode=0, stdout="", stderr="")

    def test_dry_run_shows_the_plan_and_touches_nothing(self):
        code, msg = sshkey.grant("nyx", dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual([l.split()[0] for l in msg.splitlines()[1:]], ["ssh-keygen", "ssh-copy-id", "ssh"])
        self.assertIn("BatchMode=yes", msg)  # never a password prompt
        self.assertFalse(sshkey.KEY.exists())
        self.assertEqual(sshkey.grants(), {})

    def test_without_phone_approvals_it_fails_fast(self):
        with mock.patch.object(sshkey.subprocess, "run", side_effect=self.fake_run):
            code, msg = sshkey.grant("nyx", ask=lambda q: self.fail("asked a phone that is not listening"))
        self.assertEqual((code, self.calls), (2, []))
        self.assertIn("aren't set up yet", msg)

    def test_one_approval_then_key_install_and_proof(self):
        approvals.cli(["setup"])
        with mock.patch.object(sshkey.subprocess, "run", side_effect=self.fake_run):
            code, msg = sshkey.grant("nyx", ask=lambda q: approvals.APPROVED)
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.calls], ["ssh-keygen", "ssh-copy-id", "ssh"])
        self.assertIn("nyx", sshkey.grants())
        self.assertIn("IdentitiesOnly=yes", msg)
        self.calls.clear()
        with mock.patch.object(sshkey.subprocess, "run", side_effect=self.fake_run):
            sshkey.grant("other", ask=lambda q: approvals.APPROVED)
        self.assertEqual([c[0] for c in self.calls], ["ssh-copy-id", "ssh"])  # one key for every host

    def test_denied_or_unanswered_runs_nothing(self):
        approvals.cli(["setup"])
        with mock.patch.object(sshkey.subprocess, "run", side_effect=self.fake_run):
            code, msg = sshkey.grant("nyx", ask=lambda q: approvals.DENIED)
            self.assertEqual((code, self.calls), (approvals.DENIED, []))
            self.assertIn("denied", msg)
            code, _ = sshkey.grant("nyx", ask=lambda q: approvals.TIMED_OUT)
        self.assertEqual(code, approvals.TIMED_OUT)
        self.assertEqual(sshkey.grants(), {})


if __name__ == "__main__":
    unittest.main()
