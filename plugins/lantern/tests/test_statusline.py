import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import statusline  # noqa: E402


class StatusLineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.parent.mkdir()
        self.env = mock.patch.dict(os.environ, {"LANTERN_HOME": str(self.home / "data"), "CLAUDE_CONFIG_DIR": str(self.settings.parent)})
        self.env.start()
        self.line = mock.patch.object(statusline.status, "line", return_value="Lantern: all well")
        self.line.start()

    def tearDown(self):
        self.line.stop()
        self.env.stop()
        self.tmp.cleanup()

    def read(self):
        return json.loads(self.settings.read_text())

    def backups(self):
        return sorted(self.settings.parent.glob("settings.json.*.lantern-bak"))

    def test_installs_once_by_merging_with_a_backup(self):
        self.settings.write_text(json.dumps({"model": "opus", "enabledPlugins": {"lantern@lantern": True}}))
        status, msg, changed = statusline.ensure()
        self.assertEqual((status, changed), ("ok", True))
        cfg = self.read()
        self.assertEqual(cfg["model"], "opus")  # nothing else touched
        self.assertEqual(cfg["statusLine"], {"type": "command", "command": statusline.command()})
        self.assertEqual(json.loads(self.backups()[0].read_text()), {"model": "opus", "enabledPlugins": {"lantern@lantern": True}})
        self.assertEqual(statusline.ensure(), ("ok", "status line is Lantern's", False))  # idempotent
        self.assertEqual(len(self.backups()), 1)

    def test_a_status_line_the_user_had_keeps_running_above_lanterns(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine", "padding": 1}}))
        status, msg, changed = statusline.ensure()
        self.assertIn("'echo mine', still runs above it", msg)
        self.assertEqual(self.read()["statusLine"], {"type": "command", "command": statusline.command(), "padding": 1})
        out = io.StringIO()
        statusline.cli(stdin=io.StringIO('{"session_id": "s"}'), stdout=out)
        self.assertEqual(out.getvalue(), "mine\nLantern: all well\n")

    def test_removed_by_the_user_stays_removed(self):
        self.settings.write_text("{}")
        statusline.ensure()
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo theirs"}}))
        self.assertEqual(statusline.ensure(), ("ok", "status line was changed by you; Lantern leaves it", False))
        self.assertEqual(self.read()["statusLine"]["command"], "echo theirs")

    def test_stale_command_after_a_plugin_update_is_repaired(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": 'uv run --no-project --quiet "/old/lantern.py" status'}}))
        status, msg, changed = statusline.ensure()
        self.assertTrue(changed)
        self.assertEqual(self.read()["statusLine"]["command"], statusline.command())

    def test_unreadable_settings_are_left_alone(self):
        self.settings.write_text("{not json")
        status, msg, changed = statusline.ensure()
        self.assertEqual((status, changed), ("warn", False))
        self.assertEqual(self.settings.read_text(), "{not json")
        self.assertEqual(self.backups(), [])

    def test_missing_settings_file_is_created(self):
        self.assertEqual(statusline.ensure()[0], "ok")
        self.assertEqual(self.read()["statusLine"]["command"], statusline.command())

    def test_signal_only_where_no_status_line_renders(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "cli"}):
            self.assertIsNone(statusline.signal({}))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}):
            self.assertEqual(statusline.signal({}), {"systemMessage": "Lantern: all well"})
