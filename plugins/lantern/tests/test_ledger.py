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

from lantern_core import curate, doctor, hooks, insights, memory, transcript  # noqa: E402


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

    def test_an_unchecked_admission_is_sent_back_once(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("is plex up?"), assistant("Plex is enabled. I didn't check whether it is actually listening on 32400.")])
        out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
        self.assertEqual(out["decision"], "block")
        self.assertIn("I didn't check whether it is actually listening on 32400", out["reason"])
        self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p), "stop_hook_active": True}))  # once
        write(p, [user("is plex up?"), assistant("Plex is listening on 32400 (ss -ltn).")])
        self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))

    def test_past_tense_admissions_and_deferred_looks_are_sent_back(self):
        for text in ("I haven't checked why the updater failed. I can look at that next.",
                     "I haven't looked at Plex's own internal logs inside the container yet.",
                     "Disk is 92% full. If you want, I can take a deeper read-only look at the disk usage."):
            self.assertTrue(hooks._UNCHECKED.search(text), text)
        self.assertFalse(hooks._UNCHECKED.search("The updater failed because its repo metadata is stale (journal, read just now)."))
        self.assertFalse(hooks._UNCHECKED.search("I can restart Plex once you say yes."))  # a write offer is the user's call

    def test_a_correction_supersedes_the_note_it_retracts(self):
        db = memory.connect()
        old = memory.add(db, "fact", "curate:2026-10-30:1", "podman-auto-update.timer is enabled and runs nightly, so images refresh by themselves")
        other = memory.add(db, "fact", "curate:2026-10-30:2", "the restic passphrase sits next to the repo it unlocks on Nyx")
        db.close()
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("what changed?"), assistant("Nothing new.\n\n**Correction:** podman-auto-update.timer isn't enabled; images do not refresh by themselves, that open loop is closed.")])
        self.call("stop", {"session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})
        db = memory.connect()
        self.assertEqual(db.execute("SELECT status FROM items WHERE id=?", (old,)).fetchone()[0], "superseded")
        self.assertEqual(db.execute("SELECT status FROM items WHERE id=?", (other,)).fetchone()[0], "active")
        facts = memory.rules(db, 4000, kind="fact", dated=True)
        db.close()
        self.assertTrue(any("Correction (" in f and "isn't enabled" in f for f in facts), facts)
        self.assertFalse(any("runs nightly" in f for f in facts))
        self.assertEqual(memory.corrections("All good. I was wrong earlier: Plex runs as container-plex.service, not plex.service."), ["Plex runs as container-plex.service, not plex.service"])

    def test_hands_off_reply_is_sent_back_to_the_snapshot(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("is everything ok with my computer?"), assistant("I couldn't check. Here is what it would have run, so you can approve it or run it yourself: systemctl --failed")])
        with mock.patch.object(hooks.discovery, "settings", return_value={"insights_at": time.time() - 600}):
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
        self.assertEqual(out["decision"], "block")
        self.assertIn("within the last two hours", out["reason"])
        with mock.patch.object(hooks.discovery, "settings", return_value={"insights_at": time.time() - 3 * 3600}):
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
        self.assertNotIn("within the last two hours", out["reason"])  # no fresh snapshot to point at; only the plain "couldn't check" nudge remains

    def test_reader_level_follows_the_users_words(self):
        with mock.patch.object(hooks.discovery, "settings_path", return_value=Path(self.tmp.name) / "settings.json"):
            Path(self.tmp.name, "settings.json").write_text("{}")
            from lantern_core import register
            register.learn("is the plex thing ok? keep it simple")
            self.assertEqual(register.level(), "plain")
            register.learn("check ~/.config/systemd/user/plex.service")
            self.assertEqual(register.level(), "plain")  # a path once does not undo "keep it simple"
            self.assertIn("no unit names, paths, commands", register.guidance())
            Path(self.tmp.name, "settings.json").write_text(json.dumps({"capabilities": {"container:plex": {"name": "plex"}}}))
            self.assertEqual(register.level(), "plain")  # unsure means plain, whatever the machine runs
            register.learn("please don't restart the plex thing without asking")
            self.assertEqual(register.names(), {"plex": "the plex thing"})
            self.assertIn('"the plex thing" for plex', register.guidance())
            self.assertEqual(register.say("container-plex.service has failed; lantern-curate.timer missed"),
                             "the plex thing has failed; the lantern-curate job missed")
            self.assertEqual(register.say("Plex is down"), "the plex thing is down")
            Path(self.tmp.name, "settings.json").write_text("{}")
            register.learn("journalctl --user -u plex.service -n 20 please")
            self.assertEqual(register.level(), "expert")
            self.assertIn("paths, units and line numbers inline", register.guidance())
            register.learn("I'm a developer, give me the details")
            self.assertEqual(register.level(), "expert")

    def test_carry_keeps_hedges_whole_past_the_cut(self):
        from lantern_core import memory
        body = "The log is full of decoder lines. " * 30
        reply = body + "It's a guess, because I couldn't read Plex's own error log: that command was declined. " + body + "Next, I can check the container."
        short = memory.carry(reply, 600)
        self.assertLess(len(short), 1100)
        self.assertIn("It's a guess, because I couldn't read Plex's own error log", short)
        self.assertIn("Next, I can check the container.", short)
        self.assertIn("guesses above stay guesses", short)
        self.assertEqual(memory.carry("short and sure.", 600), "short and sure.")
        self.assertNotIn("hedged", memory.carry(body * 3, 600))

    def test_ledger_keeps_the_folder_the_session_started_in(self):
        db = memory.connect()
        memory.ledger_update(db, "d2", "/proj", {"user": "what changed", "reply": "two things", "files": [], "commands": [], "agents": [], "failed": 0})
        memory.ledger_update(db, "d2", "/proj/simrepo", {"user": "look at simrepo", "reply": "ok", "files": [], "commands": [], "agents": [], "failed": 0})  # the model cd'd
        self.assertEqual(memory.ledger_get(db, "d2")["cwd"], "/proj")
        self.assertEqual(memory.previous_session(db, "d3", "/proj")["goal"], "what changed")  # day 3 resumes from day 2, not an older session
        db.close()

    def test_what_changed_points_at_the_computed_block(self):
        with mock.patch.object(hooks.semantic, "search", return_value=None), \
                mock.patch.object(hooks.discovery, "settings", return_value={"since_last": {"names": ["simrepo"], "at": time.time()}, "insights_at": time.time()}):
            r = hooks.user_prompt_submit({"prompt": "what changed since yesterday?", "session_id": "s1", "transcript_path": "/nonexistent"})
            self.assertIn("This asks what changed", r["hookSpecificOutput"]["additionalContext"])  # the prompt hook must not crash when names are recorded
        with mock.patch.object(hooks.semantic, "search", return_value=None):
            ctx = hooks.user_prompt_submit({"prompt": "what changed since yesterday?", "session_id": "s1"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("File timestamps in this folder are not the answer", ctx)
            self.assertTrue(ctx.startswith("This asks what changed"), ctx[:80])  # at the top, not buried under recall
        with mock.patch.object(hooks.semantic, "search", return_value=None), \
                mock.patch.object(hooks.discovery, "settings", return_value={"since_last": {"block": "Since the last session: 0 findings resolved, 2 new, 29 unchanged. New: failed unit lantern-sim-fail.service; uncommitted work in ~/Documents/Week/simrepo.", "block_at": time.time()}}):
            ctx = hooks.user_prompt_submit({"prompt": "what changed since yesterday?", "session_id": "s1"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("New: failed unit lantern-sim-fail.service", ctx)  # the block itself travels with the question
            r = hooks.user_prompt_submit({"prompt": "add a nightly timer", "session_id": "s1"})
        self.assertNotIn("File timestamps", (r or {}).get("hookSpecificOutput", {}).get("additionalContext", ""))

    def test_a_restart_of_something_a_rule_names_asks_first(self):
        db = memory.connect()
        memory.add(db, "rule", "said:s0", "from now on, never restart Plex without asking me first, it breaks my parents' stream")
        db.close()
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("plex looks stuck, restart it now"), assistant("On it.")])
        call = lambda cmd: hooks.rule_guard({"tool_name": "Bash", "tool_input": {"command": cmd}, "transcript_path": str(p)})
        out = call("systemctl --user restart container-plex.service")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("never restart Plex", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("Lantern is checking with you before this restart.", out["hookSpecificOutput"]["permissionDecisionReason"])  # the user reads this
        with mock.patch.object(hooks.semantic, "search", return_value=None):
            ctx = hooks.user_prompt_submit({"prompt": "plex looks stuck, restart it now", "session_id": "s1"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ask with the question tool; a yes there is the go-ahead", ctx)  # the guard is the backstop, the ask comes first
        self.assertIsNone(call("systemctl --user restart sonarr.service"))  # no rule names it
        self.assertIsNone(call("systemctl --user status container-plex.service"))  # reading is free
        write(p, [user("plex looks stuck, restart it now"), assistant('Your rule: "from now on, never restart Plex without asking me first, it breaks my parents\' stream". Nobody is streaming; restarting now as you asked.')])
        self.assertIsNone(call("systemctl --user restart container-plex.service"))  # quoted this turn: the go-ahead stands
        self.assertEqual(memory.open_loops("Say which of these duties to take on. The most useful next step is a local backup."),
                         ["Say which of these duties to take on", "The most useful next step is a local backup"])
        self.assertEqual(memory.open_loops("Plex is up. One of Plex's own helper processes looks stuck, with 7 MB waiting to be read."),
                         ["One of Plex's own helper processes looks stuck, with 7 MB waiting to be read"])  # a problem Lantern saw is tracked too

    def test_question_tool_answers_are_kept_by_lantern(self):
        p = Path(self.tmp.name) / "t.jsonl"
        result = ('The user answered: "From what\'s on bazzite, my guess is this machine is for gaming plus building AI agent tooling. Is that right?"='
                  '"Yes, that\'s right", "Which guardrails should Lantern hold on this machine?"="Never restart a service without asking first., '
                  'Never push personal folders off this machine.". Read the answers carefully.')
        lines = [user("hello"),
                 {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {"questions": []}}]}},
                 {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "q1", "content": result}]}},
                 assistant("Saved.")]
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        with mock.patch.object(hooks.discovery, "settings_path", return_value=Path(self.tmp.name) / "settings.json"):
            Path(self.tmp.name, "settings.json").write_text(json.dumps({"pending": ["purpose", "guardrails", "wider"]}))
            self.call("stop", {"session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})
            self.call("stop", {"session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})  # twice: recorded once
            self.assertEqual(hooks.discovery.settings()["pending"], ["wider"])
        db = memory.connect()
        self.assertEqual(memory.profile(db, 1000), "This machine is for gaming plus building AI agent tooling")
        self.assertEqual(sorted(r for r in memory.rules(db, 4000) if "." in r), ["Never push personal folders off this machine.", "Never restart a service without asking first."])
        db.close()

    def test_an_offered_read_only_check_is_run_instead(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("is the drive ok?"), assistant("Probably. Should I run the read-only drive health check?")])
        with mock.patch.object(hooks.discovery, "settings", return_value={}):
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
        self.assertIn("offers a read-only check instead of doing it", out["reason"])
        write(p, [user("plex is stuck"), assistant("Should I run the restart now?")])
        with mock.patch.object(hooks.discovery, "settings", return_value={}):
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))  # a change is asked about, rightly

    def test_a_closing_choice_in_prose_and_allow_next_time_are_sent_back(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("hello"), assistant("Your project exists only here.\n\nTell me which GitHub account to use, or say \"copy it locally\" and I'll copy it. Which would you like?")])
        with mock.patch.object(hooks.discovery, "settings", return_value={}):
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
        self.assertIn("choice written as prose", out["reason"])
        write(p, [user("hello"), assistant("I could not see which account this machine is signed in to; that check was declined. Allow it the next time it comes up. Meanwhile the project stays local.")])
        with mock.patch.object(hooks.discovery, "settings", return_value={}):
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
        self.assertIn("never put back on the user", out["reason"])
        write(p, [user("hello"), assistant("Your project exists only here. The disk is fine and nothing has failed.\n\nNext I can put the project online; say so when you want that.")])
        with mock.patch.object(hooks.discovery, "settings", return_value={}):
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))  # one plain offer is fine

    def test_jargon_is_sent_back_for_a_plain_reader(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("is my computer ok?"), assistant("Kase failed: `systemctl --user reset-failed app-io.github.dabarkle.Kase@autostart.service` clears it (status=137/n/a).")])
        with mock.patch.object(hooks.discovery, "settings", return_value={"expertise": "general"}):
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
        self.assertIn("plain words", out["reason"])
        with mock.patch.object(hooks.discovery, "settings", return_value={"expertise": "expert"}):
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))  # an expert asked for exactly this
        write(p, [user("is my computer ok?"), assistant("All fine. One app, Kase, stopped during an update and will start again next time you log in.")])
        with mock.patch.object(hooks.discovery, "settings", return_value={"expertise": "general"}):
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))

    def test_only_a_pending_purpose_reasks_the_profile(self):
        with mock.patch.object(hooks.insights, "run", return_value=[]), mock.patch.object(hooks.discovery, "settings_path", return_value=Path(self.tmp.name) / "settings.json"):
            Path(self.tmp.name, "settings.json").write_text(json.dumps({"answered_at": 1.0, "pending": ["wider"]}))
            ctx = self.call("session-start", {"session_id": "d3", "source": "startup", "cwd": "/proj"})["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("was never confirmed", ctx)
        self.assertIn("The setup questions were answered", ctx)

    def test_first_reply_names_every_since_last_item(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("what changed?"), assistant("Since last session: 1 resolved, 1 new. The new one is simrepo, which has no remote.")])
        with mock.patch.object(hooks.discovery, "settings", return_value={"since_last": {"names": ["lantern-sim-fail.service", "simrepo"], "at": time.time()}}):
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
            self.assertIn("Not named yet: lantern-sim-fail.service", out["reason"])
            write(p, [user("what changed?"), assistant("lantern-sim-fail.service is resolved and simrepo is new.")])
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))
            write(p, [user("hi"), assistant("Hello."), user("add a timer"), assistant("Done.")])
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))  # a later unrelated turn is not held to it
            write(p, [user("hi"), assistant("Hello."), user("what changed since yesterday?"), assistant("Both changes are drafted below.")])
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
            self.assertIn("Not named yet: lantern-sim-fail.service, simrepo", out["reason"])  # a since-last question is, whenever it comes

    def test_a_background_accept_does_not_clobber_session_settings(self):
        from lantern_core import discovery
        with mock.patch.object(hooks.discovery, "settings_path", return_value=Path(self.tmp.name) / "settings.json"):
            Path(self.tmp.name, "settings.json").write_text(json.dumps({"insights": ["insight/failed/x.service"], "insights_at": 5}))
            with mock.patch.object(discovery.packs, "for_finds", return_value=None) if hasattr(discovery, "packs") else mock.patch.dict({}, {}):
                discovery.accept([{"id": "tool:sh", "kind": "tool", "name": "sh", "detail": "/bin", "check": {"type": "tool", "name": "sh"}}])
            s = discovery.settings()
        self.assertEqual(s["insights"], ["insight/failed/x.service"])  # the scout wrote only its own key
        self.assertIn("tool:sh", s["capabilities"])

    def test_a_reply_stored_empty_is_repaired_at_the_next_capture(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("plex looks stuck, restart it now")])  # Stop fired before the text landed
        self.call("stop", {"session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})
        db = memory.connect()
        self.assertTrue(hooks._empty_reply(db.execute("SELECT text FROM items WHERE kind='turn'").fetchone()[0]))
        write(p, [user("plex looks stuck, restart it now"), assistant("Not restarted. I'll ask for the Plex key the first time a restart actually comes up.")])
        with mock.patch.object(hooks.semantic, "search", return_value=None):
            hooks.user_prompt_submit({"prompt": "ok", "session_id": "s1", "transcript_path": str(p), "cwd": "/proj"})
        self.assertEqual(db.execute("SELECT count(*) FROM items WHERE kind='turn'").fetchone()[0], 1)  # repaired in place, not duplicated
        self.assertIn("Plex key", db.execute("SELECT text FROM items WHERE kind='turn'").fetchone()[0])
        self.assertIn("I'll ask for the Plex key the first time a restart actually comes up", memory.ledger_get(db, "s1")["loops"])
        db.close()

    def test_a_stop_hook_reason_does_not_start_a_new_turn(self):
        p = Path(self.tmp.name) / "t.jsonl"
        write(p, [user("what changed since yesterday?"), assistant("Three new problems: simrepo has uncommitted work."),
                  user("Stop hook feedback: Not named yet: lantern-sim-fail.service. Add one line per item."),
                  assistant("lantern-sim-fail.service failed overnight with exit status 1.")])
        turn = hooks.transcript.last_turn(p)
        self.assertEqual(turn["user"], "what changed since yesterday?")
        self.assertIn("simrepo", turn["reply"]); self.assertIn("lantern-sim-fail.service", turn["reply"])
        self.assertEqual(sum(1 for r, _ in hooks.transcript.messages(p) if r == "user"), 1)
        with mock.patch.object(hooks.discovery, "settings", return_value={"since_last": {"names": ["lantern-sim-fail.service", "simrepo"], "at": time.time()}}):
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)}))  # both named across the turn: no third pass
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p), "stop_hook_active": True}))

    def test_an_agents_result_is_kept_even_though_its_transcript_is_sidechain(self):
        p = Path(self.tmp.name) / "agent.jsonl"
        lines = [{"type": "user", "isSidechain": True, "message": {"role": "user", "content": "check backup coverage"}},
                 {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [{"type": "text", "text": "None of the four folders is covered by hermes-backup."}]}}]
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        self.call("subagent-stop", {"session_id": "s1", "agent_transcript_path": str(p)})
        db = memory.connect()
        self.assertEqual(db.execute("SELECT count(*) FROM items WHERE kind='turn' AND text LIKE 'Agent result:%'").fetchone()[0], 1)
        db.close()

    def test_newly_discovered_is_said_once(self):
        from lantern_core import stage
        db = memory.connect()
        stage.publish(db, "discovery/new", "discovery", "Found on this machine and remembered: plex, sonarr")
        db.close()
        with mock.patch.object(hooks.insights, "run", return_value=[]), mock.patch.object(hooks.discovery, "settings_path", return_value=Path(self.tmp.name) / "settings.json"):
            Path(self.tmp.name, "settings.json").write_text("{}")  # not a first session
            ctx = self.call("session-start", {"session_id": "d2", "source": "startup", "cwd": "/proj"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Found on this machine and remembered: plex, sonarr", ctx)
            ctx = self.call("session-start", {"session_id": "d3", "source": "startup", "cwd": "/proj"})["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Found on this machine and remembered", ctx)  # day 3 does not call day 2's finds new again

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
        self.assertEqual(memory.open_loops("Done.\nI didn't check whether the timer fired.\nNext, I can list the largest folders."),
                         ["I didn't check whether the timer fired", "Next, I can list the largest folders"])
        self.assertEqual(memory.open_loops("Enabled. Should I also enable it on Nyx? Is the 03:00 slot right for you?\n- I can read the journal next."),
                         ["Should I also enable it on Nyx?", "Is the 03:00 slot right for you?", "I can read the journal next"])  # questions and offers are loops too
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
        q = self.call("user-prompt-submit", {"session_id": "s1", "prompt": "can you restart plex?"})  # a question is not a rule
        self.assertNotIn("systemMessage", q)
        self.assertIn("Standing rules that touch this request", q["hookSpecificOutput"]["additionalContext"])  # but the rule is quoted
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
            self.assertEqual([c[-3] for c in spawned], ["curate", "doctor"])  # the first session scouted itself: discover is done for today
            self.assertIn("--data", spawned[0])
            for j in ("curate", "doctor", "discover"):
                jobs.mark(j, "done")
            self.assertEqual(jobs.due(), [])
            self.assertEqual(jobs.due(now=time.time() + 25 * 3600), ["curate", "doctor", "discover"])


class HookLatencyTest(unittest.TestCase):
    def test_async_stop_hook_is_not_in_the_latency_budget(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            rows = [{"hook": "user-prompt-submit", "ms": 40}] * 25 + [{"hook": "stop", "ms": 1600}] * 25
            (Path(d) / "hooks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            status, msg = doctor.check_hook_latency()
            self.assertEqual(status, doctor.OK, msg)
            self.assertIn("40 ms", msg)


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
                doctor.real_start_server()
                self.assertEqual(len(spawned), 1)  # a second start within START_EVERY is skipped: a server that cannot come up is not retried per prompt
                os.utime(Path(d) / "serve.started", (0, 0))
                doctor.real_start_server()
                self.assertEqual(len(spawned), 2)

    def test_a_prompt_that_finds_the_server_down_starts_it(self):
        from lantern_core import hooks
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(hooks.semantic, "search", return_value=None), mock.patch.object(doctor, "start_server") as start:
            hooks.user_prompt_submit({"session_id": "s", "prompt": "what did we decide about the backup schedule last week"})
            self.assertEqual(start.call_count, 1)


class SinceLastPlacementTest(unittest.TestCase):
    def test_since_last_is_named_at_the_top_and_in_full_at_the_end(self):
        from lantern_core import discovery, hooks, insights
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            discovery.save_settings({"depth": 2, "answered_at": 1.0})
            hooks.session_start({"source": "startup", "session_id": "s0", "cwd": d})  # a first returning session has nothing to diff
            with mock.patch.object(insights, "diff_since_last", return_value="Since the last session: 0 findings resolved, 1 new, 3 unchanged. New: failed x.service."):
                out = hooks.session_start({"source": "startup", "session_id": "s1", "cwd": d})
            text = out["hookSpecificOutput"]["additionalContext"]
            top, rest = text.split("\n", 2)[1], text.rsplit("SINCE LAST SESSION", 1)[-1]
            self.assertTrue(top.startswith("Since last session, in brief: Since the last session: 0 findings resolved, 1 new"), top[:120])
            self.assertIn("1 new", rest)
            self.assertLess(text.index("in brief"), text.index("SINCE LAST SESSION ("))


class ScoutingLineTest(unittest.TestCase):
    def test_scouting_is_announced_when_the_daily_discover_starts(self):
        from lantern_core import discovery, hooks, jobs
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            discovery.save_settings({"depth": 2, "answered_at": 1.0})  # not a first session
            with mock.patch.object(jobs, "start", return_value=["discover"]):
                out = hooks.session_start({"source": "startup", "session_id": "s", "cwd": d})
            self.assertIn("Scouting now", json.dumps(out))
            with mock.patch.object(jobs, "start", return_value=[]):
                out = hooks.session_start({"source": "startup", "session_id": "s", "cwd": d})
            self.assertNotIn("Scouting now", json.dumps(out))


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
                with mock.patch.object(insights, "repos", side_effect=AssertionError("re-checked within the hour")):
                    self.assertEqual(sorted(insights.real_run(db)), sorted(msgs))  # cached, same answers
            insights.diff_since_last({f"insight/repo-risk/{dirty}", f"insight/repo-noremote/{clean}"})  # this session's findings
            subprocess.run(["git", "-C", str(dirty), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(dirty), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"], check=True)
            with mock.patch.object(insights.discovery, "_run", return_value=""):
                line = insights.diff_since_last({f"insight/repo-noremote/{clean}"})
            self.assertIn("1 finding resolved, 0 new, 1 unchanged", line)
            self.assertNotIn("finding(s)", line)
            self.assertIn("Resolved: dirty (", line)  # a vanished finding is explained, never dropped
            self.assertIn("): now clean (last commit", line)
            self.assertIn("still no remote", line)
            line = insights.diff_since_last({f"insight/repo-noremote/{clean}", "insight/failed/x.service"})
            self.assertIn("0 findings resolved, 1 new, 1 unchanged", line)
            self.assertIn("New: failed unit x.service", line)  # kinds in words, never the internal key
            with mock.patch.object(insights.discovery, "_run", return_value=""):
                line = insights.diff_since_last({f"insight/repo-noremote/{clean}", "insight/backup/x.service"})
            self.assertIn("0 findings resolved, 0 new, 1 changed, 1 unchanged", line)  # same subject, new kind: changed, not resolved plus new
            self.assertIn("Changed: x.service: now", line)
            with mock.patch.object(insights.discovery, "_run", return_value=""):
                insights.diff_since_last({f"insight/repo-noremote/{clean}", "insight/failed/x.service"})
            with mock.patch.object(insights.discovery, "_run", side_effect=lambda cmd: "failed" if "is-failed" in cmd else ""):
                line = insights.diff_since_last({f"insight/repo-noremote/{clean}", "insight/failed/y.service"})  # cache says x.service is gone
            self.assertIn("0 findings resolved, 1 new, 1 back again, 1 unchanged", line)  # but it is failed right now: back, not resolved
            self.assertIn("Back again", line)
            self.assertNotIn("checked just now", line)
            self.assertIn("insight/failed/x.service", insights.discovery.settings()["insights"])  # remembered, so tomorrow it is not "new"
            self.assertTrue(any("dirty (" in m and "has 1 uncommitted change(s) and no git remote" in m for m in msgs), msgs)
            self.assertTrue(any("clean (" in m and "is active but has no git remote" in m for m in msgs), msgs)
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
                      json.dumps({"type": "result", "result": "# What this machine is for\nA lab.\n# Worth knowing\n1. The restic passphrase sits next to the repo it unlocks on Nyx.\n- Nothing is backed up off this machine at all.\n# Profile\nA builder (inferred)."})]
            fake = mock.Mock(stdout=iter(l + "\n" for l in events), poll=lambda: 0, wait=lambda timeout=None: 0, kill=lambda: None)
            with mock.patch.object(survey.subprocess, "Popen", return_value=fake):
                survey.run()
            self.assertEqual(json.loads((Path(d) / "survey.json").read_text())["steps"], 1)
            self.assertIn("A lab", survey.brief())
            db = memory.connect()
            self.assertIn("A builder", memory.profile(db, 1000))
            self.assertIn("finished its survey", stage.top(db)[0]["message"])
            block = insights.open_block(db)
            self.assertIn("Survey risks still open (2)", block)
            self.assertIn("The restic passphrase", block)
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


class SetupAnswerTest(unittest.TestCase):
    def test_a_short_yes_records_guardrails_profile_and_wider_look(self):
        from lantern_core import discovery
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            hooks.session_start({"session_id": "s1"})  # first session: questions pending
            self.assertEqual(discovery.settings()["pending"], ["purpose", "guardrails", "wider"])
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                self.assertIsNone(hooks.user_prompt_submit({"prompt": "what is a quadlet?", "session_id": "s1"}))
                r = hooks.user_prompt_submit({"prompt": "ok", "session_id": "s1"})
            self.assertIn("accepted the setup", r["hookSpecificOutput"]["additionalContext"])
            self.assertIn(hooks.GUARDRAILS[0], r["hookSpecificOutput"]["additionalContext"])  # the model is told exactly what was stored
            self.assertEqual(discovery.settings()["pending"], ["purpose"])  # a bare yes never answers who the user is
            self.assertEqual(discovery.settings()["depth"], 3)
            ctx = hooks.session_start({"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Never restart a service or container", ctx)
            self.assertIn("never confirmed", ctx)
            self.assertIn("Lantern's watches", ctx)
            self.assertIn("Now: 20", ctx)
            db = memory.connect()
            hooks.stage.publish(db, "insight/failed/x.service", "insight", "x.service has failed", score=5)
            self.assertIn("failed: x.service", insights.watch_summary(db))
            self.assertIn("Open findings (1; state counts only from this line): 1 failed: x.service", insights.open_block(db))
            db.close()


class MissedRunTest(unittest.TestCase):
    def test_a_scheduled_run_in_the_past_is_missed(self):
        from lantern_core import insights
        self.assertTrue(insights._missed("Fri 2026-10-30 03:30:00 AEDT", now=time.mktime(time.strptime("2026-10-30 05:00:00", "%Y-%m-%d %H:%M:%S"))))
        self.assertFalse(insights._missed("Fri 2026-10-30 03:30:00 AEDT", now=time.mktime(time.strptime("2026-10-30 03:35:00", "%Y-%m-%d %H:%M:%S"))))
        self.assertFalse(insights._missed(""))


class HeadlessCaptureTest(unittest.TestCase):
    def test_previous_exchange_is_captured_at_the_next_prompt_when_stop_did_not_run(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            p = Path(d) / "t.jsonl"
            write(p, [user("check the plex unit"), assistant("Done. I didn't check the journal yet.")])
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                hooks.user_prompt_submit({"prompt": "and now?", "session_id": "s1", "transcript_path": str(p), "cwd": d})
                hooks.user_prompt_submit({"prompt": "and now again?", "session_id": "s1", "transcript_path": str(p), "cwd": d})
            db = memory.connect()
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE kind='turn'").fetchone()[0], 1)  # once, not twice
            self.assertEqual(memory.ledger_get(db, "s1")["loops"], ["I didn't check the journal yet"])
            write(p, [user("check the plex unit"), assistant("Done. I didn't check the journal yet."), user("and now?"), assistant("Next, I can read it.")])
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                hooks.user_prompt_submit({"prompt": "go", "session_id": "s1", "transcript_path": str(p), "cwd": d})
            self.assertEqual(memory.ledger_get(db, "s1")["loops"], ["I didn't check the journal yet", "Next, I can read it"])  # accumulated
            # the next session inherits the unfinished items and closes the one its reply addresses
            write(p, [user("read the journal"), assistant("The plex journal shows a bind failure on port 32400.")])
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                hooks.user_prompt_submit({"prompt": "ok", "session_id": "s2", "transcript_path": str(p), "cwd": d})
            self.assertEqual(memory.ledger_get(db, "s2")["loops"], ["I didn't check the journal yet", "Next, I can read it"])  # one shared word is a mention, not a closure
            self.assertTrue(memory.addresses("I read the plex journal: a bind failure on port 32400, container-plex holds it.", "I didn't check the plex journal for the bind failure on 32400"))
            self.assertFalse(memory.addresses("The fix is a localhost bind or a firewall rule on 6006.", "I didn't check whether a firewall already blocks port 6006 from the LAN"))
            db.close()


class RuleTouchingTest(unittest.TestCase):
    def test_a_matching_rule_is_quoted_at_prompt_time(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "rule", "said:s0", "from now on never restart Plex without asking me first")
            memory.add(db, "rule", "said:s0", "keep replies short")
            db.close()
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                r = hooks.user_prompt_submit({"prompt": "plex looks stuck, restart it now", "session_id": "s1"})
            ctx = r["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Standing rules that touch this request", ctx)
            self.assertIn("never restart Plex", ctx)
            self.assertNotIn("keep replies short", ctx)
            db = memory.connect()
            self.assertEqual(memory.rules_touching(db, "what should I fix first?"), [])  # "first" alone touches no rule
            db.close()
            self.assertNotIn("states its reason", ctx)  # no reason given, nothing to pre-check
            db = memory.connect()
            memory.add(db, "rule", "said:s0", "never restart Plex without asking me first, it breaks my parents' stream")
            db.close()
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                ctx = hooks.user_prompt_submit({"prompt": "plex looks stuck, restart it now", "session_id": "s1"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("run one read-only check of whether that reason holds", ctx)
            db = memory.connect()
            hooks.stage.publish(db, "insight/x", "insight", "plex.container has AutoUpdate=registry: it can restart itself", score=3)
            db.close()
            with mock.patch.object(hooks.semantic, "search", return_value=None):
                r = hooks.user_prompt_submit({"prompt": "plex looks stuck, restart it now", "session_id": "s2"})
            self.assertIn("Open findings that touch this request", r["hookSpecificOutput"]["additionalContext"])


class LastSessionCaptureTest(unittest.TestCase):
    def test_a_session_that_ended_without_stop_is_captured_at_the_next_start(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(hooks.Path, "home", return_value=Path(d)):
            cwd = "/work/proj"
            folder = Path(d) / ".claude" / "projects" / "-work-proj"
            folder.mkdir(parents=True)
            write(folder / "old-session.jsonl", [user("fix the timer"), assistant("Done. Next, I can check it fired.")])
            ctx = hooks.session_start({"session_id": "new-session", "cwd": cwd})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Next, I can check it fired", ctx)  # the open loop made it into the new session
            hooks.session_start({"session_id": "newer", "cwd": cwd})
            db = memory.connect()
            self.assertEqual(db.execute("SELECT count(*) FROM items WHERE kind='turn' AND source='old-session'").fetchone()[0], 1)
            db.close()


WEEK6_REPLY = """I couldn't check. The health check was blocked as if it would change the machine, but every command in it only reads. Nothing was restarted or modified. Here is what it would have run, so you can approve it or run it yourself:

- `uptime` and `free -h`: system load and memory.
- `df -h`: whether any disk is running low.
- `systemctl --failed` and `systemctl --user --failed`: system and user services that have failed.

If you approve it, I'll run it as one read-only check.

I don't know the state of your machine right now."""


class HandsOffTest(unittest.TestCase):
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

    def test_hands_off_is_recognised_by_shape_not_only_by_phrase(self):
        self.assertTrue(hooks.hands_off(WEEK6_REPLY))
        # no stock phrase at all: an admission plus a list of commands is still hands-off
        self.assertTrue(hooks.hands_off("That check was declined.\n\n- `systemctl --failed`\n- `df -h`\n\nIf you approve it, I'll run it."))
        self.assertTrue(hooks.hands_off("Paste me the output of `df -h` and I'll take it from there."))
        # honest hedges with an answer are not hands-off
        self.assertFalse(hooks.hands_off("Plex looks healthy: up 28 hours, answers locally (HTTP 200). It's a guess, because I couldn't read Plex's own error log."))
        # a declined change the user asked for is theirs to approve; handing that command over is not hands-off looking
        self.assertFalse(hooks.hands_off("Plex has not been restarted; the restart was declined.\n\n```\nsystemctl --user restart container-plex.service\n```\n\nAllow it when the prompt appears (or run it yourself)."))
        self.assertFalse(hooks.hands_off("All fine. I ran `systemctl --failed` (nothing) and `df -h` (41% used).\n\n- `lantern-curate.timer`: next 03:00\n- `lantern-doctor.timer`: next 04:00"))

    def test_after_one_failed_retry_the_hook_itself_states_the_snapshot(self):
        p = Path(self.tmp.name) / "t.jsonl"
        db = memory.connect()
        hooks.stage.publish(db, "insight/failed/x.service", "insight", "x.service has failed", score=5)
        db.close()
        settings = {"insights_at": time.time() - 600}
        with mock.patch.object(hooks.discovery, "settings", return_value=settings), mock.patch.object(hooks.discovery, "save_settings"):
            write(p, [user("is everything ok with my computer?"), assistant(WEEK6_REPLY)])
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p)})
            self.assertEqual(out["decision"], "block")
            self.assertIn("found: 1 failed; failed: x.service", out["reason"])  # the snapshot's words are in the nudge itself
            # the retry still hands off: the hook adds the answer rather than nudging again
            write(p, [user("is everything ok with my computer?"), assistant(WEEK6_REPLY), assistant("Sorry, I still couldn't check. You could run `systemctl --failed` and `df -h` yourself.")])
            out = self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p), "stop_hook_active": True})
            self.assertNotIn("decision", out)
            self.assertIn("failed: x.service", out["systemMessage"])
            # a retry that answers is left alone, even though the first half of the turn was hands-off
            write(p, [user("is everything ok with my computer?"), assistant(WEEK6_REPLY), assistant("One thing is wrong: x.service has failed. Disk and the rest are fine.")])
            self.assertIsNone(self.call("stop-nudge", {"session_id": "s1", "transcript_path": str(p), "stop_hook_active": True}))

    def test_a_health_question_is_handed_the_snapshot_as_its_answer(self):
        db = memory.connect()
        hooks.stage.publish(db, "insight/failed/x.service", "insight", "x.service has failed", score=5)
        db.close()
        with mock.patch.object(hooks.discovery, "settings", return_value={"insights_at": time.time() - 600}), mock.patch.object(hooks.discovery, "save_settings"):
            with mock.patch.object(hooks.insights, "run") as run:
                r = self.call("user-prompt-submit", {"session_id": "s1", "prompt": "is everything ok with my computer?", "transcript_path": ""})
            ctx = r["hookSpecificOutput"]["additionalContext"]
            self.assertIn("You already know the answer to this, and it is current", ctx)
            self.assertTrue(run.called)  # the cheap checks ran again for this question, so no "I haven't looked live" hedge is needed
            self.assertIn("failed: x.service", ctx)
            self.assertIsNone(self.call("user-prompt-submit", {"session_id": "s1", "prompt": "rename the backup timer", "transcript_path": ""}))
        with mock.patch.object(hooks.discovery, "settings", return_value={"insights_at": time.time() - 3 * 3600}), mock.patch.object(hooks.discovery, "save_settings"):
            self.assertIsNone(self.call("user-prompt-submit", {"session_id": "s1", "prompt": "is everything ok with my computer?", "transcript_path": ""}))  # stale: no claim


class LiveChecksTest(unittest.TestCase):
    def test_failed_units_and_timers_are_checked_live_while_the_slow_scan_is_cached(self):
        from lantern_core import discovery, stage
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            discovery.accept([{"id": "repo:/r", "kind": "repo", "name": "r", "detail": "", "check": {"type": "path", "path": d}}])
            db = memory.connect()
            stage.publish(db, "insight/repo-risk//r", "insight", "r has 2 uncommitted change(s)", score=5)  # the hourly scan's result
            discovery.save_settings({**discovery.settings(), "insights_at": time.time() - 300, "insights": ["insight/repo-risk//r", "insight/failed/x.service"]})

            def run(cmd):
                if "--failed" in cmd and "--user" in cmd:
                    return "x.service loaded failed failed\n"
                if "list-timers" in cmd:
                    return "Sat 2026-11-01 03:00:00 GMT 8h left - - new-job.timer new-job.service\n"
                return ""
            with mock.patch.object(insights.discovery, "_run", side_effect=run), mock.patch.object(insights, "disk", return_value=[]), \
                    mock.patch.object(insights, "repos", side_effect=AssertionError("the slow scan must stay cached")):
                msgs = insights.real_run(db)
                self.assertTrue(any("x.service has failed" in m for m in msgs), msgs)  # live, not from the hour-old cache
                self.assertTrue(any("r has 2 uncommitted" in m for m in msgs), msgs)  # cached
                self.assertFalse(any("new-job.timer" in m for m in msgs))  # the first look at timers only learns them
                line = insights.diff_since_last({e["key"] for e in stage.top(db, k=50) if e["source"] == "insight"}, db)
                self.assertIn("0 findings resolved, 0 new, 2 unchanged", line)  # the flap week's "Resolved: x: now failed" cannot happen
                self.assertNotIn("Resolved", line)

            def run2(cmd):
                if "list-timers" in cmd:
                    return ("Sat 2026-11-01 03:00:00 GMT 8h left - - new-job.timer new-job.service\n"
                            "Sun 2026-11-02 04:00:00 GMT 1 day left - - other.timer other.service\n")
                return ""
            with mock.patch.object(insights.discovery, "_run", side_effect=run2), mock.patch.object(insights, "disk", return_value=[]), \
                    mock.patch.object(insights, "repos", return_value=[]):
                msgs = insights.real_run(db)
            self.assertTrue(any("new scheduled job appeared: other.timer (next run Sun 2026-11-02 04:00:00)" in m for m in msgs), msgs)
            self.assertFalse(any("x.service" in m for m in msgs))  # cleared since: gone from the live set
            self.assertEqual(discovery.settings()["timers_seen"], ["new-job.timer", "other.timer"])
            db.close()
