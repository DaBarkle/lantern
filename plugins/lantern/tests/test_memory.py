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


if __name__ == "__main__":
    unittest.main()
