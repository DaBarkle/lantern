import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import doctor, hooks, memory, shield, stage  # noqa: E402

HOUR = 3600
FAKE_GH = "ghp_" + "A1b2" * 9  # built at runtime: no literal secret in the repo


class StageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = memory.connect(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_fades_and_expires(self):
        now = time.time()
        stage.publish(self.db, "a", "t", "old but loud", 8, now=now - 48 * HOUR)
        stage.publish(self.db, "b", "t", "new and mild", 4, now=now)
        stage.publish(self.db, "c", "t", "gone", 10, days=1, now=now - 25 * HOUR)
        self.assertEqual([e["key"] for e in stage.top(self.db, now=now)], ["b", "a"])

    def test_republish_refreshes_one_entry(self):
        stage.publish(self.db, "k", "t", "first")
        stage.publish(self.db, "k", "t", "second")
        self.assertEqual([e["message"] for e in stage.top(self.db)], ["second"])

    def test_delta_once_per_change_per_session(self):
        self.assertIsNone(stage.delta(self.db, "s1"))
        stage.publish(self.db, "k", "t", "disk nearly full")
        self.assertIn("disk nearly full", stage.delta(self.db, "s1"))
        self.assertIsNone(stage.delta(self.db, "s1"))
        self.assertIn("disk nearly full", stage.delta(self.db, "s2"))  # other sessions still get it
        stage.resolve(self.db, "k")
        self.assertEqual(stage.delta(self.db, "s1"), "Stage is now clear.")

    def test_resolve_source_drops_ghosts(self):
        stage.publish(self.db, "doctor/a", "doctor", "x")
        stage.publish(self.db, "doctor/gone", "doctor", "y")
        stage.publish(self.db, "note/1", "note", "z")
        stage.resolve_source(self.db, "doctor", {"doctor/a"})
        self.assertEqual(sorted(e["key"] for e in stage.top(self.db, k=9)), ["doctor/a", "note/1"])

    def test_notes_are_redacted(self):
        stage.publish(self.db, "k", "t", f"use {FAKE_GH} for the push")
        self.assertNotIn(FAKE_GH, stage.top(self.db)[0]["message"])


class ShieldTest(unittest.TestCase):
    def call(self, tool, **inp):
        return shield.pre_tool_use({"tool_name": tool, "tool_input": inp})

    def test_blocks_raw_secrets(self):
        for tool, inp in [("Bash", {"command": f"curl -H 'Authorization: token {FAKE_GH}' x"}),
                          ("Write", {"content": "API_KEY=" + "Zq9" * 8}),
                          ("Edit", {"new_string": "-----BEGIN OPENSSH PRIVATE KEY-----"}),
                          ("MultiEdit", {"edits": [{"new_string": "aws = AKIA" + "ABCD1234" * 2}]})]:
            r = self.call(tool, **inp)
            self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny", tool)

    def test_allows_references_and_ordinary_code(self):
        for cmd in ['curl -H "Authorization: token $GITHUB_TOKEN" x', "token = secrets.token_hex(16)",
                    "password = getpass.getpass()", "git log --oneline", "export TOKEN_FILE=/tmp/serve.token"]:
            self.assertIsNone(self.call("Bash", command=cmd), cmd)

    def test_fails_closed(self):
        with mock.patch.object(shield, "find", side_effect=RuntimeError("boom")):
            self.assertEqual(self.call("Bash", command="ls")["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hook_registered(self):
        self.assertIs(hooks.HANDLERS["pre-tool-use"], shield.pre_tool_use)


class DoctorTest(unittest.TestCase):
    def test_findings_follow_checks(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            with mock.patch.object(doctor.semantic, "search", return_value=None):
                results = dict((n, s) for n, s, _ in doctor.run_all())
            self.assertEqual(results["semantic"], doctor.WARN)
            self.assertEqual(results["shield"], doctor.OK)
            db = memory.connect()
            self.assertEqual([e["key"] for e in stage.top(db)], ["doctor/semantic"])
            with mock.patch.object(doctor.semantic, "search", return_value=[]):
                doctor.run_all()
            self.assertEqual(stage.top(db), [])  # fixed problem clears its own alert
            db.close()
