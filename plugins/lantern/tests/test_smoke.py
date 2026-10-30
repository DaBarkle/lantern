"""Every hook entry point must run clean on a returning session; a crash in one is a silent failure on a live install."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import doctor, hooks, smoke  # noqa: E402


class HookSmokeTest(unittest.TestCase):
    def test_every_hook_runs_clean_on_a_returning_session(self):
        self.assertEqual(smoke.run(), [])

    def test_each_hook_run_records_its_output_size(self):
        import io, json, os, tempfile
        from lantern_core import paths
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(paths, "_pointer", lambda: Path(d) / "ptr"), mock.patch.dict(hooks.HANDLERS, {"signal": lambda p: {"systemMessage": "hi"}}):
            hooks.run("signal", stdin=io.StringIO("{}"), stdout=io.StringIO())
            rows = [json.loads(l) for l in (Path(d) / "hooks.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["hook"], "signal")
        self.assertEqual(rows[-1]["bytes"], len(json.dumps({"systemMessage": "hi"})))

    def test_a_crashing_hook_is_reported_not_swallowed(self):
        def boom(payload):
            raise NameError("name 'since' is not defined")
        with mock.patch.dict(hooks.HANDLERS, {"signal": boom}):
            problems = smoke.run()
        self.assertTrue(any(p.startswith("signal: NameError") for p in problems), problems)
        with mock.patch.dict(hooks.HANDLERS, {"signal": boom}):
            status, msg = doctor.check_hook_smoke()
        self.assertEqual(status, doctor.FAIL)
        self.assertIn("signal: NameError", msg)


if __name__ == "__main__":
    unittest.main()
