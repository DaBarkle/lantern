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
