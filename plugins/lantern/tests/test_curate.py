import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import curate, hooks, memory  # noqa: E402


class CurateTest(unittest.TestCase):
    def test_turns_become_rules_loaded_next_session(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "rule", "pack", "Never change Tailscale on bazzite.")
            memory.add(db, "turn", "s1", "User: stop using hacky workarounds\nAssistant: understood", created=time.time() - 60)
            db.close()
            seen = {}

            def fake(prompt):
                seen["prompt"] = prompt
                return [{"kind": "rule", "summary": "Prefer proper fixes over hacky workarounds.", "detail": "Said so on Oct 30."},
                        {"kind": "rule", "summary": "Never change Tailscale on bazzite.", "detail": "duplicate"},
                        {"kind": "fact", "summary": "The API token is ghp_" + "A1b2" * 9, "detail": "secret"}]

            self.assertEqual(curate.run(ask=fake), 1)
            self.assertIn("stop using hacky workarounds", seen["prompt"])
            self.assertIn("Never change Tailscale", seen["prompt"])  # known items are shown so they aren't repeated
            ctx = hooks.session_start({"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("- Prefer proper fixes over hacky workarounds.", ctx)
            self.assertNotIn("ghp_", ctx)
            # Nothing new since the last run: no model call at all.
            self.assertEqual(curate.run(ask=lambda p: self.fail("called the model with no new turns")), 0)


class LearnFromUseTest(unittest.TestCase):
    def test_a_heavy_day_is_read_whole(self):
        turns = [f"User: turn {n} " + "x" * 9000 for n in range(20)]  # 180k characters
        parts = curate.chunks(turns)
        self.assertEqual(len(parts), 4)
        self.assertTrue(all(len(p) <= curate.MAX_CHARS for p in parts))
        self.assertIn("turn 19", parts[-1])  # newest last
        joined = "".join(parts)
        self.assertIn("turn 0 ", joined)  # the start of the day is no longer cut away
        self.assertEqual(curate.chunks(["a", "b"]), ["a\n\n---\n\nb"])
        many = [f"t{n} " + "y" * 30000 for n in range(12)]
        self.assertEqual(len(curate.chunks(many)), curate.MAX_CALLS)  # capped: the oldest parts are skipped
        self.assertIn("t11", curate.chunks(many)[-1])

    def test_each_part_is_asked_and_sees_what_earlier_parts_learned(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            for n in range(14):
                memory.add(db, "turn", "s1", f"User: part {n} " + "z" * 9000, created=time.time() - 600 + n)
            db.close()
            prompts = []

            def fake(prompt):
                prompts.append(prompt)
                return [{"kind": "routine", "summary": "Check the nightly backup's last result and say when it ran.", "detail": "Asked most days."}] if len(prompts) == 1 else []

            self.assertEqual(curate.run(ask=fake), 1)
            self.assertEqual(len(prompts), 3)
            self.assertIn("Check the nightly backup", prompts[1])  # a later part does not re-learn it
            ctx = hooks.session_start({"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Checks the user keeps asking for", ctx)
            self.assertIn("- Check the nightly backup's last result", ctx)

    def test_retaught_rule_gains_weight_and_keeps_it_when_reworded(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            old = memory.add(db, "rule", "said:s0", "Keep replies short and plain.", created=time.time() - 3 * 86400)
            memory.add(db, "rule", "said:s0b", "Never touch Tailscale.", created=time.time() - 86400)
            memory.add(db, "turn", "s1", "User: I told you, keep replies short and plain\nAssistant: sorry", created=time.time() - 60)
            db.close()
            curate.run(ask=lambda p: [{"kind": "rule", "summary": "Keep replies short and plain.", "detail": "Said again.", "retaught": True}])
            db = memory.connect()
            self.assertEqual(memory.uses(db, [old])[old], (0, 1))
            self.assertEqual(memory.rules(db, 2000)[0], "Keep replies short and plain. (the user has had to say this 2 times)")
            memory.add(db, "turn", "s2", "User: again, keep replies short and plain please, three lines\nAssistant: ok", created=time.time() + 5)
            db.close()
            curate.run(now=time.time() + 10, ask=lambda p: [{"kind": "rule", "summary": "Keep replies short and plain, three lines at most.",
                                                            "detail": "Said a third time.", "retaught": True}])
            db = memory.connect()
            self.assertEqual(memory.rules(db, 2000)[0], "Keep replies short and plain, three lines at most. (the user has had to say this 3 times)")
            db.close()

    def test_recalled_notes_stay_in_the_index(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            now = time.time()
            used = memory.add(db, "fact", "curate:a", "Plex runs as container-plex.service on bazzite.", created=now - 60 * 86400)
            memory.add(db, "fact", "curate:b", "The printer is in the study.", created=now - 30 * 86400)
            self.assertEqual(memory.rules(db, 2000, kind="fact")[0], "The printer is in the study.")
            memory.used(db, [used])
            memory.used(db, [used])
            memory.used(db, [used])
            memory.used(db, [used])
            memory.used(db, [used])  # five recalls: counted five weeks newer, so it now outranks the unused month-old note
            self.assertTrue(memory.rules(db, 2000, kind="fact")[0].startswith("Plex runs"))
            db.close()

    def test_recall_counts_curated_hits_only(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(hooks.semantic, "search", return_value=None), mock.patch.object(hooks.resources, "can_spawn", return_value=False):
            db = memory.connect()
            fact = memory.add(db, "fact", "curate:x", "Sonarr on Nyx needs the Gluetun tunnel up before it can reach indexers.")
            turn = memory.add(db, "turn", "old", "User: Sonarr on Nyx cannot reach indexers through Gluetun tunnel\nAssistant: checking")
            db.close()
            hooks.user_prompt_submit({"session_id": "s9", "prompt": "why can Sonarr on Nyx not reach indexers through the Gluetun tunnel"})
            db = memory.connect()
            self.assertEqual(memory.uses(db, [fact, turn]), {fact: (1, 0)})
            db.close()

