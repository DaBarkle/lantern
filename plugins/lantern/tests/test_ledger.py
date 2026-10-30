import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import curate, doctor, hooks, memory, transcript  # noqa: E402


def write(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def user(text):
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def assistant(text, *tools):
    blocks = [{"type": "text", "text": text}] + [{"type": "tool_use", "id": f"t{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(tools)]
    return {"type": "assistant", "message": {"content": blocks}}


def result(err=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t0", "content": "x", "is_error": err}]}}


class TranscriptTest(unittest.TestCase):
    def test_last_turn_collects_actions_and_failures(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            write(p, [user("old"), assistant("ok", ("Bash", {"command": "ls"})), user("fix the timer"),
                      assistant("looking", ("Edit", {"file_path": "/x/a.py"}), ("Bash", {"command": "systemctl --user restart x\n# more"})),
                      result(err=True), assistant("done", ("Agent", {"description": "check logs"}))])
            t = transcript.last_turn(p)
            self.assertEqual(t["user"], "fix the timer")
            self.assertEqual(t["reply"], "looking\ndone")
            self.assertEqual(t["files"], ["/x/a.py"])
            self.assertEqual(t["commands"], ["systemctl --user restart x"])
            self.assertEqual(t["agents"], ["check logs"])
            self.assertEqual(t["failed"], 1)

    def test_only_the_tail_of_a_huge_transcript_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            filler = [user("x" * 10000)] * 600  # ~6 MB, beyond the tail window
            write(p, filler + [user("last question"), assistant("last answer")])
            self.assertGreater(p.stat().st_size, transcript.TAIL_BYTES)
            t0 = time.monotonic()
            self.assertEqual(transcript.last_exchange(p), ("last question", "last answer"))
            self.assertLess(time.monotonic() - t0, 1.0)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def call(self, name, payload):
        out = io.StringIO()
        hooks.run(name, io.StringIO(json.dumps(payload)), out)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_resume_shows_what_the_last_session_did_in_this_folder(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("add a nightly backup timer"), assistant("ok", ("Edit", {"file_path": "/proj/backup.timer"}), ("Bash", {"command": "systemctl --user daemon-reload"}))])
        self.call("stop", {"session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})
        write(p, [user("add a nightly backup timer"), assistant("ok"), user("now enable it"), assistant("enabled", ("Bash", {"command": "systemctl --user enable --now backup.timer"}))])
        self.call("stop", {"session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})
        # a later session elsewhere must not hide the project's own last session
        write(p, [user("unrelated"), assistant("sure")])
        self.call("stop", {"session_id": "s2", "transcript_path": str(p), "cwd": "/other"})
        ctx = self.call("session-start", {"session_id": "s3", "source": "startup", "cwd": "/proj"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("last session in this folder", ctx)
        self.assertIn("Goal: add a nightly backup timer", ctx)
        self.assertIn("Last asked: now enable it", ctx)
        self.assertIn("/proj/backup.timer", ctx)
        self.assertIn("enable --now backup.timer", ctx)
        ctx = self.call("session-start", {"session_id": "s4", "source": "startup", "cwd": "/third"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Goal: unrelated", ctx)
        # actions are searchable later
        db = memory.connect()
        self.assertTrue(any("backup.timer" in h["text"] for h in memory.search(db, "backup.timer daemon-reload")))
        db.close()

    def test_turn_capture_keeps_the_reply_after_a_long_prompt(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("log: " + "x" * 5000), assistant("the fix is to rotate the key")])
        self.call("stop", {"session_id": "s1", "transcript_path": str(p)})
        db = memory.connect()
        text = db.execute("SELECT text FROM items WHERE kind='turn'").fetchone()[0]
        db.close()
        self.assertIn("rotate the key", text)
        self.assertLess(len(text), 2 * hooks.CAPTURE_CHARS + 100)

    def test_correction_becomes_a_rule_at_once(self):
        r = self.call("user-prompt-submit", {"session_id": "s1", "prompt": "don't restart plex without asking me first"})
        self.assertIn("standing rule", r["systemMessage"])
        self.assertIsNone(self.call("user-prompt-submit", {"session_id": "s1", "prompt": "can you restart plex?"}))  # a question is not a rule
        for task in ("stop the plex container and check the logs", "make sure you restart wireplumber after", "remember to push the branch"):
            r = self.call("user-prompt-submit", {"session_id": "s1", "prompt": task})
            self.assertTrue(r is None or "systemMessage" not in r, task)  # a task for now is not a standing rule
        ctx = self.call("session-start", {"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- don't restart plex without asking me first", ctx)
        again = self.call("user-prompt-submit", {"session_id": "s2", "prompt": "don't restart plex without asking me first"})
        self.assertTrue(again is None or "systemMessage" not in again)  # already known

    def test_subagent_result_is_captured(self):
        p = Path(self.tmp.name) / "agent.jsonl"
        write(p, [user("check the logs"), assistant("Found: sshd is rejecting key type ssh-rsa since the 9.8 upgrade.")])
        self.call("subagent-stop", {"session_id": "s1", "agent_transcript_path": str(p)})
        db = memory.connect()
        self.assertTrue(memory.search(db, "sshd rejecting ssh-rsa"))
        db.close()

    def test_git_state_goes_on_stage_and_clears(self):
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a").write_text("1")
        self.call("session-start", {"session_id": "s1", "cwd": str(repo)})
        db = memory.connect()
        self.assertIn("1 uncommitted change", hooks.stage.top(db)[0]["message"])
        (repo / "a").unlink()
        db.close()
        self.call("session-start", {"session_id": "s2", "cwd": str(repo)})
        db = memory.connect()
        self.assertEqual(hooks.stage.top(db), [])
        db.close()

    def test_hook_latency_is_measured(self):
        for _ in range(20):
            self.call("session-start", {"session_id": "s"})
        self.assertEqual(doctor.check_hook_latency()[0], doctor.OK)
        with mock.patch.object(doctor, "HOOK_P90_MS", -1):
            self.assertEqual(doctor.check_hook_latency()[0], doctor.WARN)


class CurateUpdateTest(unittest.TestCase):
    def test_changed_fact_replaces_the_old_one(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            old = memory.add(db, "fact", "pack", "Plex listens on port 32400 on bazzite.")
            memory.add(db, "turn", "s1", "User: plex moved to 32401\nAssistant: noted", created=time.time() - 60)
            db.close()
            fake = lambda p: [{"kind": "fact", "summary": "Plex listens on port 32401 on bazzite.", "detail": "moved Oct 30"},
                              {"kind": "profile", "summary": "David games on this PC in the evenings.", "detail": "said so"},
                              {"kind": "pattern", "summary": "If Nyx services show network errors, check Gluetun first.", "detail": "it gateways them all"}]
            self.assertEqual(curate.run(ask=fake), 3)
            db = memory.connect()
            self.assertEqual(db.execute("SELECT status FROM items WHERE id=?", (old,)).fetchone()[0], "superseded")
            self.assertIn("games on this PC", memory.profile(db, 1000))
            db.close()
            ctx = hooks.session_start({"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("32401", ctx)
            self.assertNotIn("32400", ctx)
            self.assertIn("patterns learned", ctx)
            self.assertIn("check Gluetun first", ctx)


if __name__ == "__main__":
    unittest.main()


class JobsTest(unittest.TestCase):
    def test_due_jobs_start_once_a_day_from_session_start(self):
        from lantern_core import jobs
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(jobs.curate, "available", return_value=True):
            self.assertEqual(jobs.due(), ["curate", "doctor", "discover"])
            spawned = []
            with mock.patch.object(jobs.subprocess, "Popen", side_effect=lambda cmd, **kw: spawned.append(cmd)):
                hooks.session_start({"session_id": "s1", "source": "startup"})
                hooks.session_start({"session_id": "s2", "source": "startup"})  # already started: not again
            self.assertEqual([c[-3] for c in spawned], ["curate", "doctor", "discover"])
            self.assertIn("--data", spawned[0])
            for j in ("curate", "doctor", "discover"):
                jobs.mark(j, "done")
            self.assertEqual(jobs.due(), [])
            self.assertEqual(jobs.due(now=time.time() + 25 * 3600), ["curate", "doctor", "discover"])


class DoctorRepairTest(unittest.TestCase):
    def test_memory_server_down_is_started_not_reported(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(doctor.semantic, "search", return_value=None):
            spawned = []
            with mock.patch("subprocess.Popen", side_effect=lambda cmd, **kw: spawned.append(cmd)):
                status, msg = doctor.check_semantic()
            self.assertEqual(status, doctor.OK)
            self.assertIn("started", msg)
            self.assertTrue(spawned[0][-1].endswith("serve.py"))


class AutoDiscoveryTest(unittest.TestCase):
    def test_new_finds_within_depth_are_remembered_and_announced_once(self):
        from lantern_core import discovery, stage
        find = {"id": "tool:docker", "kind": "tool", "name": "docker", "detail": "/usr/bin/docker", "check": {"type": "tool", "name": "docker"}}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(discovery, "discover", return_value=[find]):
            self.assertEqual(len(discovery.auto()), 1)
            self.assertEqual(discovery.auto(), [])  # known now
            db = memory.connect()
            top = stage.top(db)
            self.assertIn("docker", top[0]["message"])
            self.assertIn("lantern-docker", top[0]["message"])
            self.assertTrue(memory.search(db, "docker capability"))
            db.close()
            ctx = hooks.session_start({"session_id": "s"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("docker", ctx)
