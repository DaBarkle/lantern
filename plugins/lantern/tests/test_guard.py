import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import approvals, doctor, hooks, memory, shield, stage  # noqa: E402

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
        canary = "token = " + "ab12" * 6
        out = hooks.HANDLERS["pre-tool-use"]({"tool_name": "Bash", "tool_input": {"command": f"echo {canary}"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("shield", out["hookSpecificOutput"]["permissionDecisionReason"])


class DoctorTest(unittest.TestCase):
    def test_findings_follow_checks(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            with mock.patch.object(doctor.semantic, "search", return_value=None), \
                    mock.patch.object(doctor, "start_server", side_effect=OSError("no uv")):
                results = dict((n, s) for n, s, _ in doctor.run_all())
            self.assertEqual(results["semantic"], doctor.WARN)
            self.assertEqual(results["shield"], doctor.OK)
            db = memory.connect()
            self.assertEqual([e["key"] for e in stage.top(db)], ["doctor/semantic"])
            with mock.patch.object(doctor.semantic, "search", return_value=[]):
                doctor.run_all()
            self.assertEqual(stage.top(db), [])  # fixed problem clears its own alert
            db.close()


class ApprovalsTest(unittest.TestCase):
    """Against a fake ntfy server: the phone's tap is a POST to the reply topic."""

    def setUp(self):
        import http.server
        import threading
        posts, replies, rate_limits = [], [], []

        class Fake(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append(body)
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                if rate_limits:  # ntfy.sh does this to a fast poller; the answer must still get through
                    rate_limits.pop()
                    self.send_response(429)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write("".join(json.dumps({"event": "message", "message": m}) + "\n" for m in replies).encode())

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.posts, self.replies, self.rate_limits = posts, replies, rate_limits
        self.cfg = {"server": f"http://127.0.0.1:{self.srv.server_port}", "topic": "t"}

    def tearDown(self):
        self.srv.shutdown()

    def tap(self, which):
        # wait for the question, then answer it the way the ntfy action button would
        for _ in range(100):
            if self.posts:
                action = next(a for a in self.posts[0]["actions"] if a["label"] == which)
                self.replies.extend(["approve deadbeef", action["body"]])  # an old tap first: must be ignored
                return
            time.sleep(0.02)

    def run_ask(self, which, timeout=5):
        import threading
        threading.Thread(target=self.tap, args=(which,), daemon=True).start()
        with mock.patch.object(approvals, "POLL_SECONDS", 0.05):
            return approvals.ask("Restart Plex?", timeout=timeout, cfg=self.cfg)

    def test_approve_and_deny(self):
        self.rate_limits.extend([1, 1])
        with mock.patch.object(approvals, "RETRY_SECONDS", 0.05):
            self.assertEqual(self.run_ask("Approve"), approvals.APPROVED)
        self.assertEqual(self.posts[-1]["title"], "Approved")  # the phone is told the tap landed
        self.posts.clear()
        self.replies.clear()
        self.assertEqual(self.run_ask("Deny"), approvals.DENIED)

    def test_old_taps_and_silence_time_out(self):
        self.replies.append("approve deadbeef")
        with mock.patch.object(approvals, "POLL_SECONDS", 0.05):
            self.assertEqual(approvals.ask("x", timeout=0.3, cfg=self.cfg), approvals.TIMED_OUT)

    def test_topic_is_private_and_stable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            a, b = approvals.config(), approvals.config()
            self.assertEqual(a, b)
            self.assertGreater(len(a["topic"]), 25)


class SecretHandoverTest(unittest.TestCase):
    def test_pasted_secret_is_stored_and_never_reaches_the_model(self):
        from lantern_core import stage
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            calls = []

            def fake_run(cmd, **kw):
                calls.append((cmd, kw.get("input")))
                return mock.Mock(returncode=0)
            with mock.patch.object(shield.subprocess, "run", side_effect=fake_run):
                reply = hooks.user_prompt_submit({"prompt": "secret PLEX_TOKEN: abc123XYZtoken", "session_id": "s"})
            self.assertEqual(reply["decision"], "block")
            self.assertIn("PLEX_TOKEN", reply["reason"])
            self.assertNotIn("abc123XYZtoken", json.dumps(reply))
            self.assertEqual(calls[0][1], "abc123XYZtoken")  # handed to `secret set` on stdin
            self.assertIn("PLEX_TOKEN", calls[0][0])
            db = memory.connect()
            self.assertIn("PLEX_TOKEN", stage.top(db)[0]["message"])
            db.close()
            self.assertEqual(shield.paste("tell me a secret: how does plex work"), (None, False))

    def test_after_an_ask_the_value_pasted_on_its_own_is_the_hand_over(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            self.assertEqual(shield.paste("abc123XYZtoken"), (None, False))  # nothing asked: a bare word is a prompt
            sentence = shield.ask("PLEX_TOKEN")
            self.assertIn("plex token", sentence)
            self.assertNotIn("secret ", sentence)  # the user is never shown syntax
            self.assertEqual(shield.paste("is plex still down?"), (None, False))  # a sentence is not a value
            calls = []
            with mock.patch.object(shield.subprocess, "run", side_effect=lambda cmd, **kw: calls.append((cmd, kw.get("input"))) or mock.Mock(returncode=0)):
                reply = hooks.user_prompt_submit({"prompt": "abc123XYZtoken\n", "session_id": "s"})
            self.assertEqual(reply["decision"], "block")
            self.assertEqual(calls[0][1], "abc123XYZtoken")
            self.assertIn("PLEX_TOKEN", calls[0][0])
            self.assertIsNone(shield.pending())  # answered once
            shield.ask("PLEX_TOKEN")
            with mock.patch.object(shield.time, "time", return_value=shield.time.time() + shield.ASK_SECONDS + 1):
                self.assertEqual(shield.paste("abc123XYZtoken"), (None, False))  # an old ask does not swallow a later word

    def test_without_a_keychain_the_secret_is_still_stopped_and_kept_nowhere(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(shield.subprocess, "run", return_value=mock.Mock(returncode=1)):
            reply = hooks.user_prompt_submit({"prompt": "secret PLEX_TOKEN: abc123XYZtoken", "session_id": "s"})
        self.assertEqual(reply["decision"], "block")
        self.assertIn("no keychain", reply["reason"])
        self.assertNotIn("abc123XYZtoken", json.dumps(reply))


class OnePasswordServiceAccountTest(unittest.TestCase):
    def test_token_from_keychain_vault_pinned(self):
        from lantern_core import shield
        token = "ops_" + "eyJ" + "a1B2" * 220  # about 900 characters, like a real one
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            self.assertEqual(shield.find(f"here it is {token}"), ["1Password service account token"])  # never stored as memory
            shield.ask("OP_SERVICE_ACCOUNT_TOKEN")
            kr = mock.MagicMock()
            kr.get_password.side_effect = lambda svc, name: token if name == "OP_SERVICE_ACCOUNT_TOKEN" else None
            with mock.patch.object(shield, "_keyring", return_value=kr):
                name, stored = shield.paste(token)  # the long token pasted on its own is caught, not passed to the model
            self.assertEqual(name, "OP_SERVICE_ACCOUNT_TOKEN")
            self.assertTrue(stored)
            self.assertEqual(shield.cli(["op-vault", "Hermes"]), 0)
            self.assertTrue(shield.op_allowed("op://Hermes/Plex/token"))
            self.assertTrue(shield.op_allowed("op://hermes/Plex/token"))
            self.assertFalse(shield.op_allowed("op://Private/Bank/password"))
            self.assertEqual(shield.cli(["link", "BANK", "op://Private/Bank/password"]), 1)  # outside the vault: refused
            seen = {}

            def run(cmd, **kw):
                seen.update(cmd=cmd, env=kw["env"])
                return mock.Mock(returncode=0, stdout="s3cret\n")

            with mock.patch.object(shield.subprocess, "run", side_effect=run):
                self.assertEqual(shield._read("op://Hermes/Plex/token", kr), "s3cret")
                self.assertEqual(seen["env"]["OP_SERVICE_ACCOUNT_TOKEN"], token)  # from the keychain, for this call only
                self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", os.environ)
                seen.clear()
                self.assertIsNone(shield._read("op://Private/Bank/password", kr))
                self.assertEqual(seen, {})  # never even asked
            with mock.patch.object(shield, "names", return_value=["OP_SERVICE_ACCOUNT_TOKEN"]):
                self.assertEqual(shield._ready("op", {"OP_SERVICE_ACCOUNT_TOKEN"})[0], True)
