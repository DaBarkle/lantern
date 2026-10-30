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
        write(p, [user("add a nightly backup timer"), assistant("ok"), user("now enable it"), assistant("enabled. Next: check it fired at 03:00.", ("Bash", {"command": "systemctl --user enable --now backup.timer"}))])
        self.call("stop", {"session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})
        # a later session elsewhere must not hide the project's own last session
        write(p, [user("unrelated"), assistant("sure")])
        self.call("stop", {"session_id": "s2", "transcript_path": str(p), "cwd": "/other"})
        ctx = self.call("session-start", {"session_id": "s3", "source": "startup", "cwd": "/proj"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("last session in this folder", ctx)
        self.assertIn("Goal: add a nightly backup timer", ctx)
        self.assertIn("Last asked: now enable it", ctx)
        self.assertIn("Last reply ended: enabled. Next: check it fired at 03:00.", ctx)
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
            with mock.patch.object(jobs.subprocess, "Popen", side_effect=lambda cmd, **kw: spawned.append(cmd)), \
                    mock.patch.object(hooks.jobs, "start", jobs.real_start):
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
            with mock.patch("subprocess.Popen", side_effect=lambda cmd, **kw: spawned.append(cmd)), \
                    mock.patch.object(doctor, "start_server", doctor.real_start_server):
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
            self.assertEqual(discovery.describe([find]), "  dev tools (1): docker")
            db = memory.connect()
            top = stage.top(db)
            self.assertIn("docker", top[0]["message"])
            self.assertTrue(memory.search(db, "docker capability"))
            db.close()
            ctx = hooks.session_start({"session_id": "s"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("1 dev tools (docker)", ctx)  # the machine summary, not 26 index lines
            self.assertNotIn("On this machine: docker", ctx)


class StatusTest(unittest.TestCase):
    def test_one_plain_line(self):
        from lantern_core import stage, status
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "fact", "curate:x", "Plex runs on bazzite as the container-plex user service.")
            db.close()
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                hooks.user_prompt_submit({"prompt": "where does plex run on bazzite", "session_id": "s"})
            s = status.line()
            self.assertTrue(s.startswith("Lantern: 1 note recalled last turn"), s)
            self.assertIn("learned 1 today", s)
            self.assertIn("all well", s)
            db = memory.connect()
            stage.publish(db, "doctor/x", "doctor", "memory capture stopped", score=8)
            db.close()
            self.assertIn("needs you: memory capture stopped", status.line())


class OnboardingTest(unittest.TestCase):
    def test_first_session_offers_setup_only_once(self):
        from lantern_core import discovery
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            ctx = hooks.session_start({"session_id": "s1"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("first session on this machine", ctx)
            self.assertIn("never leaves Claude", ctx)
            ctx = hooks.session_start({"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("first session on this machine", ctx)
            self.assertIn("never runs /lantern commands", ctx)


class ResourceGuardTest(unittest.TestCase):
    def test_no_background_work_when_memory_is_short(self):
        from lantern_core import jobs, resources
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(resources, "available_mb", return_value=200):
            with mock.patch("subprocess.Popen", side_effect=AssertionError("spawned under memory pressure")):
                self.assertEqual(jobs.start(["doctor"], Path("/x/lantern.py")), [])
                with mock.patch.object(doctor.semantic, "search", return_value=None):
                    self.assertEqual(doctor.check_semantic()[0], doctor.WARN)
            self.assertEqual(doctor.check_resources()[0], doctor.WARN)
        with mock.patch.object(resources, "available_mb", return_value=None):
            self.assertTrue(resources.can_spawn())  # unknown platform: no false alarms


class IndexDatesTest(unittest.TestCase):
    def test_fact_index_lines_are_dated(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "fact", "curate:a", "Paperclip was stopped on purpose.", created=time.time() - 3 * 86400)
            db.close()
            ctx = hooks.session_start({"session_id": "s"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn(f"- [{time.strftime('%Y-%m', time.localtime(time.time() - 3 * 86400))}] Paperclip was stopped on purpose.", ctx)
            db = memory.connect()
            memory.add(db, "fact", "curate:b", "SABnzbd on Nyx is stopped because the disks are full.", created=time.time() - 40 * 86400)
            memory.add(db, "fact", "curate:c", "Films and series run as separate flows; Tdarr converts to H.265.", created=time.time() - 40 * 86400)
            db.close()
            ctx = hooks.session_start({"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn(", may have changed] SABnzbd on Nyx is stopped", ctx)
            self.assertNotIn("may have changed] Films and series", ctx)


class PreviousMessageTest(unittest.TestCase):
    def test_previous_message_only_widens_short_prompts(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            seen = []
            with mock.patch.object(hooks.transcript, "previous_user", side_effect=lambda p: seen.append(p) or "earlier topic"), \
                    mock.patch.object(hooks.semantic, "search", return_value=None):
                hooks.user_prompt_submit({"prompt": "why?", "session_id": "s", "transcript_path": "t"})
                self.assertEqual(seen, ["t"])
                hooks.user_prompt_submit({"prompt": "please list every container on nyx with its image, restart policy and the port it publishes", "session_id": "s", "transcript_path": "t"})
                self.assertEqual(seen, ["t"])  # a full prompt stands on its own


class LogRotationTest(unittest.TestCase):
    def test_logs_keep_only_their_newest_part(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(hooks, "LOG_BYTES", 2000):
            for i in range(200):
                hooks._log("recall", {"fired": True, "hits": [i]})
            path = Path(d) / "recall.jsonl"
            self.assertLess(path.stat().st_size, 2100)
            lines = path.read_text().splitlines()
            self.assertTrue(all(l.startswith("{") for l in lines))  # no half line at the top
            self.assertEqual(json.loads(lines[-1])["hits"], [199])
            from lantern_core import status
            self.assertIn("1 note recalled last turn", status.line())


class DoctorSummaryTest(unittest.TestCase):
    def test_healthy_capabilities_fold_into_one_line(self):
        from lantern_core import discovery
        finds = [{"id": f"tool:{t}", "kind": "tool", "name": t, "detail": "/bin", "check": {"type": "tool", "name": t}} for t in ("sh", "ls")]
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            discovery.accept(finds)
            names = [n for n, _, _ in doctor.run_all()]
            self.assertIn("capabilities", names)
            self.assertFalse(any(n.startswith("cap:") for n in names))


class InsightsTest(unittest.TestCase):
    def test_findings_about_repos_land_on_stage(self):
        from lantern_core import discovery, insights, stage
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            dirty = Path(d) / "dirty"; dirty.mkdir()
            subprocess.run(["git", "init", "-q", str(dirty)], check=True)
            (dirty / "a").write_text("x")
            clean = Path(d) / "clean"; clean.mkdir()
            subprocess.run(["git", "init", "-q", str(clean)], check=True)
            subprocess.run(["git", "-C", str(clean), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            discovery.accept([{"id": f"repo:{dirty}", "kind": "repo", "name": "dirty", "detail": "", "check": {"type": "path", "path": str(dirty)}},
                              {"id": f"repo:{clean}", "kind": "repo", "name": "clean", "detail": "", "check": {"type": "path", "path": str(clean)}}])
            db = memory.connect()
            with mock.patch.object(insights.discovery, "_run", return_value=""), mock.patch.object(insights, "disk", return_value=[]), \
                    mock.patch.object(insights, "unit_secrets", return_value=[]):
                msgs = insights.real_run(db)
            insights.diff_since_last({f"insight/repo-risk/{dirty}", f"insight/repo-noremote/{clean}"})  # this session's findings
            subprocess.run(["git", "-C", str(dirty), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(dirty), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"], check=True)
            with mock.patch.object(insights.discovery, "_run", return_value=""):
                line = insights.diff_since_last({f"insight/repo-noremote/{clean}"})
            self.assertIn("1 finding(s) resolved", line)
            self.assertIn("dirty: now clean, still no remote", line)  # a vanished finding is explained, never dropped
            self.assertTrue(any("dirty has 1 uncommitted change(s) and no remote" in m for m in msgs), msgs)
            self.assertTrue(any("clean is active but has no remote" in m for m in msgs), msgs)
            self.assertEqual({e["source"] for e in stage.top(db, k=10)}, {"insight"})
            db.close()


class SurveyTest(unittest.TestCase):
    def test_survey_starts_once_and_stores_its_brief(self):
        from lantern_core import survey, stage
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(survey.curate, "available", return_value=True):
            spawned = []
            with mock.patch.object(survey.subprocess, "Popen", side_effect=lambda cmd, **kw: spawned.append(cmd)):
                self.assertTrue(survey.real_start())
                self.assertFalse(survey.real_start())  # already running
            self.assertEqual(spawned[0][-3:-2], ["survey"])
            events = [json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/README.md"}}]}}),
                      json.dumps({"type": "result", "result": "# What this machine is for\nA lab.\n# Profile\nA builder (inferred)."})]
            fake = mock.Mock(stdout=iter(l + "\n" for l in events), poll=lambda: 0, wait=lambda timeout=None: 0, kill=lambda: None)
            with mock.patch.object(survey.subprocess, "Popen", return_value=fake):
                survey.run()
            self.assertEqual(json.loads((Path(d) / "survey.json").read_text())["steps"], 1)
            self.assertIn("A lab", survey.brief())
            db = memory.connect()
            self.assertIn("A builder", memory.profile(db, 1000))
            self.assertIn("finished its survey", stage.top(db)[0]["message"])
            db.close()
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                r = hooks.user_prompt_submit({"prompt": "anything new?", "session_id": "s9"})
            self.assertIn("A lab", r["hookSpecificOutput"]["additionalContext"])  # the brief arrives mid-session, once
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                r = hooks.user_prompt_submit({"prompt": "and now?", "session_id": "s9"})
            self.assertTrue(r is None or "A lab" not in r["hookSpecificOutput"]["additionalContext"])
            self.assertFalse(survey.real_start())  # done: never again


class InsightDetailsTest(unittest.TestCase):
    def test_disk_is_one_finding_per_volume_and_secrets_are_names_only(self):
        from lantern_core import insights
        with mock.patch.object(insights.shutil, "disk_usage", return_value=mock.Mock(total=1000e9, free=50e9)), \
                mock.patch.object(insights, "_reclaimable", return_value="; podman could free 9GB of images"):
            found = insights.disk()
        self.assertEqual(len(found), 1)
        self.assertIn("95% full (50 GB of 1000 GB left; podman could free 9GB of images)", found[0][1])
        with tempfile.TemporaryDirectory() as d:
            units = Path(d) / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            (units / "plex.service").write_text("[Service]\nEnvironment=PLEX_CLAIM=claim-abcdef123456\nEnvironment=TZ=UTC\n")
            with mock.patch.object(insights.Path, "home", return_value=Path(d)):
                found = insights.unit_secrets()
        self.assertEqual(len(found), 1)
        self.assertIn("plex.service has PLEX_CLAIM written in plain text", found[0][1])
        self.assertNotIn("abcdef", found[0][1])
