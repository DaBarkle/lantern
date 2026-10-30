"""The public replay eval (evals/replay.py) on a few mocked transcripts: no model, no server, no real data folder."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "evals"))

import replay  # noqa: E402


def transcript(path: Path, session: str, day: int, turns: list[tuple[str, str]]) -> None:
    with path.open("w") as f:
        for i, (user, reply) in enumerate(turns):
            ts = f"2026-10-{day:02d}T10:{i:02d}:00Z"
            f.write(json.dumps({"type": "user", "timestamp": ts, "message": {"content": user}}) + "\n")
            f.write(json.dumps({"type": "user", "timestamp": ts, "message": {"content": [{"type": "tool_result", "content": "x"}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": ts, "isSidechain": True, "message": {"content": [{"type": "text", "text": "agent chatter"}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": reply}]}}) + "\n")


OLD = [("the plex container on nyx keeps stopping overnight, what is going on with it", "Gluetun restarts and takes Plex down with it."),
       ("from now on never restart plex without asking me first, my parents stream from it", "Noted."),
       ("<command-name>/status</command-name>", "ok"),
       ('Run `uv run --no-project --quiet "${CLAUDE_PLUGIN_ROOT}/lantern.py" profile`. Present it as a short page.', "ok"),
       ("Stop hook feedback:\nREASONING QUALITY CHECK: you did a lot", "ok")]
NEW = [("the plex container on nyx is stopping again this morning, can you sort it out", "I would restart the Plex container now."),
       ("no, I said ask first, my parents are watching", "Sorry, I will ask first."),
       ("explain what a reciprocal rank fusion is in two sentences", "It merges ranked lists by summed inverse ranks."),
       ("thanks", "You're welcome.")]


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "proj").mkdir()
        transcript(root / "proj" / "old.jsonl", "old", 1, OLD)
        transcript(root / "proj" / "new.jsonl", "new", 5, NEW)
        self.projects = root
        replay.DATA = root / "data"
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(root / "real")})  # must be ignored by the replay
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_pick_ground_truth_and_cutoff(self):
        sessions = replay.mine(self.projects)
        self.assertEqual([t["ok"] for t in sessions["old"]], [True, True, False, False, False])  # slash commands and hook text are not the user's words
        items, cutoff = replay.pick(sessions, 30)
        # "from now on ..." is followed only by a slash command, so it has no ground truth and is left out
        self.assertEqual([it["text"][:12] for it in items], ["the plex con", "the plex con", "no, I said a", "explain what"])
        self.assertEqual(cutoff, replay._ts("2026-10-01T10:00:00Z"))  # the oldest chosen session began on day 1
        items, cutoff = replay.pick(sessions, 3)
        self.assertEqual(cutoff, replay._ts("2026-10-05T10:00:00Z"))
        with mock.patch.object(replay, "PER_SESSION", 2):  # one long session cannot crowd the others out
            self.assertEqual([it["session"] for it in replay.pick(sessions, 3)[0]], ["old", "new", "new"])
        plex = items[0]
        self.assertTrue(plex["first"])
        self.assertEqual(plex["next"], NEW[1][0])  # what the user said next is the ground truth
        self.assertEqual(plex["history"], "(this is the first message)")
        self.assertIn("User: the plex container on nyx is stopping", items[1]["history"])

    def test_run_end_to_end_without_model_or_server(self):
        asked = []

        def ask(prompt, schema):
            asked.append(prompt)
            if schema is replay.TEXT:
                return {"reply": "lantern reply" if "Lantern" in prompt.split("<conversation-so-far>")[0] else "plain reply"}
            return {"needs_memory": "plex" in prompt, "X": {"reteach": 0, "wrong": False, "wrong_what": "", "score": 4},
                    "Y": {"reteach": 2, "wrong": True, "wrong_what": "says nyx is a laptop", "score": 2}}

        def curate_ask(prompt):
            return [{"kind": "rule", "summary": "Never restart Plex without asking first; the user's parents stream from it.", "detail": "Said on Oct 1."}]

        text = replay.main(3, self.projects, ask=ask, curate_ask=curate_ask, server=False)
        self.assertIn("3 of your prompts", text)
        self.assertIn("2 earlier turns, 1 curated notes, keyword-only recall", text)
        self.assertIn("prompts that needed personal memory (1)", text)
        self.assertIn("session openers (1)", text)
        self.assertEqual(len(asked), 9)  # two replies and one judgement per prompt
        out = [json.loads(l) for l in next((replay.DATA).glob("replay-*.jsonl")).read_text().splitlines()]
        plex = out[0]
        self.assertIn("Never restart Plex without asking first", plex["lantern_context"])  # the curated rule is loaded at start
        self.assertIn("Lantern recalled these past notes", plex["lantern_context"])  # and the old turn is recalled by keyword
        self.assertNotIn("Lantern recalled", out[2]["lantern_context"])  # nothing in memory about rank fusion
        self.assertEqual(set(plex["grades"]), {"vanilla", "lantern"})
        self.assertFalse((Path(self.tmp.name) / "real").exists())  # the real data folder was never written
        self.assertEqual(os.environ["LANTERN_HOME"], str(replay.DATA / "replay-memory"))
        again = replay.rejudge(next(replay.DATA.glob("replay-*.jsonl")), ask=ask)
        self.assertIn("3 of your prompts", again)
        self.assertEqual(len(asked), 12)  # one judgement per prompt, no new replies
        rejudged = [json.loads(l) for l in next(replay.DATA.glob("replay-*-rejudged.jsonl")).read_text().splitlines()]
        self.assertEqual([r["grades"] for r in rejudged], [r["grades"] for r in out])  # same shuffle, same labels


if __name__ == "__main__":
    unittest.main()
