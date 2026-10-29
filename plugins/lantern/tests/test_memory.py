import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import hooks, memory  # noqa: E402

DAY = 86400


def write_transcript(path: Path, turns: list[tuple[str, str]]) -> None:
    with path.open("w") as f:
        for role, text in turns:
            f.write(json.dumps({"type": role, "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        # tool results arrive as user entries with no text; they must be ignored
        f.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}) + "\n")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = memory.connect(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_old_relevant_item_still_found(self):
        # The Hermes bug: a hard 14-day cut threw away every older match.
        now = time.time()
        memory.add(self.db, "note", "s1", "Plex transcoding fails on PGS subtitles", created=now - 200 * DAY)
        hits = memory.search(self.db, "why does plex transcode subtitles", now=now)
        self.assertEqual(len(hits), 1)

    def test_recent_beats_old_on_equal_text(self):
        now = time.time()
        old = memory.add(self.db, "note", "s", "tailscale exit node relay", created=now - 365 * DAY)
        new = memory.add(self.db, "note", "s", "tailscale exit node relay", created=now - DAY)
        self.assertEqual([h["id"] for h in memory.search(self.db, "tailscale relay", now=now)], [new, old])

    def test_superseded_hidden(self):
        i = memory.add(self.db, "fact", "s", "backups go to the NAS")
        memory.supersede(self.db, i)
        self.assertEqual(memory.search(self.db, "backups NAS"), [])

    def test_query_with_quotes_and_symbols_is_safe(self):
        memory.add(self.db, "note", "s", 'he said "hello" OR NOT')
        self.assertIsInstance(memory.search(self.db, 'said "hello" OR NOT * ) ('), list)
        self.assertEqual(memory.search(self.db, "?? !!"), [])

    def test_long_document_is_chunked_with_title(self):
        body = "# Family\n\n" + "\n\n".join(f"para {i} " + "x" * 300 for i in range(8))
        ids = memory.add_document(self.db, "fact", "f.md", body)
        self.assertGreater(len(ids), 1)
        last = self.db.execute("SELECT text FROM items WHERE id=?", (ids[-1],)).fetchone()[0]
        self.assertTrue(last.startswith("# Family"))
        self.assertTrue(all(len(t) < memory.CHUNK_CHARS + 500 for (t,) in self.db.execute("SELECT text FROM items")))

    def test_profile_respects_budget(self):
        memory.add(self.db, "profile", "u", "Name: Sam")
        memory.add(self.db, "profile", "u", "y" * 5000)
        memory.add(self.db, "fact", "u", "not profile")
        self.assertEqual(memory.profile(self.db, 100), "")  # newest item alone is over budget
        self.assertNotIn("not profile", memory.profile(self.db, 10000))


class RecallCaptureTest(unittest.TestCase):
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

    def test_capture_then_recall_in_a_later_session(self):
        t1 = Path(self.tmp.name) / "s1.jsonl"
        write_transcript(t1, [("user", "Make bazzite a tailscale exit node"), ("assistant", "Done, advertised exit node on bazzite.")])
        self.assertIsNone(self.call("stop", {"session_id": "s1", "transcript_path": str(t1)}))

        reply = self.call("user-prompt-submit", {"session_id": "s2", "prompt": "is the bazzite exit node still advertised?", "transcript_path": ""})
        self.assertIn("exit node", reply["hookSpecificOutput"]["additionalContext"])

    def test_short_follow_up_uses_previous_message(self):
        t1 = Path(self.tmp.name) / "s1.jsonl"
        write_transcript(t1, [("user", "Plex streams buffer over the tailscale relay"), ("assistant", "Relay is DERP; go direct.")])
        self.call("stop", {"session_id": "s1", "transcript_path": str(t1)})
        t2 = Path(self.tmp.name) / "s2.jsonl"
        write_transcript(t2, [("user", "plex buffering on the tailscale relay again")])
        reply = self.call("user-prompt-submit", {"session_id": "s2", "prompt": "why?", "transcript_path": str(t2)})
        self.assertIsNotNone(reply)

    def test_same_session_and_system_events_skipped(self):
        t1 = Path(self.tmp.name) / "s1.jsonl"
        write_transcript(t1, [("user", "configure the nightly backup job"), ("assistant", "ok")])
        self.call("stop", {"session_id": "s1", "transcript_path": str(t1)})
        self.assertIsNone(self.call("user-prompt-submit", {"session_id": "s1", "prompt": "nightly backup job status"}))
        self.assertIsNone(self.call("user-prompt-submit", {"session_id": "s2", "prompt": "<task-notification>backup job</task-notification>"}))
        log = (Path(self.tmp.name) / "recall.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(log[-1])["fired"], False)

    def test_semantic_only_hit_is_recalled_and_server_down_is_harmless(self):
        db = memory.connect()
        i = memory.add(db, "note", "s1", "media server stutters when subtitles force re-encoding")
        db.close()
        prompt = {"session_id": "s2", "prompt": "why is playback choppy with captions on"}
        with mock.patch.object(hooks.semantic, "search", return_value=[(i, 0.62)]):
            self.assertIn("stutters", self.call("user-prompt-submit", prompt)["hookSpecificOutput"]["additionalContext"])
        with mock.patch.object(hooks.semantic, "search", return_value=[(i, 0.2)]):  # below the floor
            self.assertIsNone(self.call("user-prompt-submit", prompt))
        self.assertIsNone(self.call("user-prompt-submit", prompt))  # no server running: keyword only, no crash


if __name__ == "__main__":
    unittest.main()


class SessionStartContextTest(unittest.TestCase):
    def test_rules_and_last_session_are_loaded(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "rule", "pack", "Never change Tailscale on bazzite.\n\nWhy: remote access.")
            memory.add(db, "turn", "old-session", "User: fix the AirPods\nAssistant: restarted WirePlumber", created=time.time() - 3600)
            memory.add(db, "turn", "hermes:abc", "imported turn, not a session", created=time.time())
            db.close()
            ctx = hooks.session_start({"session_id": "new"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("- Never change Tailscale on bazzite.", ctx)
            self.assertNotIn("Why: remote access", ctx)  # one line per rule
            self.assertIn("restarted WirePlumber", ctx)
            self.assertNotIn("imported turn", ctx)

    def test_old_sessions_are_not_resumed(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "turn", "s1", "User: something from last week", created=time.time() - 7 * DAY)
            db.close()
            ctx = hooks.session_start({"session_id": "new"})["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("last week", ctx)


class WorkingMemoryTest(unittest.TestCase):
    def test_compaction_brings_back_this_sessions_details(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "turn", "s1", "User: the voice companion hangs\nAssistant: it listens on port 8767, unit `voice-companion.service`")
            memory.add(db, "turn", "s1", "User: archive it\nAssistant: moved to ~/hermes-archive/20261030/ and see DAY-221")
            db.close()
            ctx = hooks.session_start({"session_id": "s1", "source": "compact"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Started with: User: the voice companion hangs", ctx)
            for name in ("voice-companion.service", "~/hermes-archive/20261030/", "DAY-221"):
                self.assertIn(name, ctx)
            fresh = hooks.session_start({"session_id": "s2", "source": "startup"})["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("working memory", fresh)
            self.assertIn("last session left off", fresh)
