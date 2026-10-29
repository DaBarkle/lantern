import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import hooks, paths, schedule  # noqa: E402


class PathsTest(unittest.TestCase):
    def test_plugin_data_wins(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": "/x/data", "LANTERN_HOME": "/y"}):
            self.assertEqual(paths.data_dir(), Path("/x/data"))

    def test_os_defaults(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_DATA", "LANTERN_HOME", "XDG_DATA_HOME", "APPDATA")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(paths.Path, "home", return_value=Path("/h")):
            for platform, want in [("linux", "/h/.local/share/lantern"),
                                   ("darwin", "/h/Library/Application Support/lantern"),
                                   ("win32", "/h/AppData/Roaming/lantern")]:
                with mock.patch.object(paths.sys, "platform", platform):
                    self.assertEqual(paths.data_dir(), Path(want))


class HooksTest(unittest.TestCase):
    def test_session_start_reply_shape_and_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": f"{tmp}/d"}):
            out = io.StringIO()
            self.assertEqual(hooks.run("session-start", io.StringIO('{"source":"startup"}'), out), 0)
            reply = json.loads(out.getvalue())["hookSpecificOutput"]
            self.assertEqual(reply["hookEventName"], "SessionStart")
            self.assertTrue(Path(tmp, "d").is_dir())

    def test_bad_stdin_and_unknown_hook(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": tmp}):
            self.assertEqual(hooks.run("session-start", io.StringIO("not json"), io.StringIO()), 0)
        self.assertEqual(hooks.run("nope", io.StringIO(""), io.StringIO()), 1)


class ScheduleTest(unittest.TestCase):
    entry = Path("/p/lantern.py")

    def test_linux_timer(self):
        p = schedule.plan("doctor", 3, self.entry, "linux", Path("/h"))
        timer = p["files"][Path("/h/.config/systemd/user/lantern-doctor.timer")]
        self.assertIn("OnCalendar=*-*-* 03:00:00", timer)
        self.assertIn("enable", p["commands"][-1])

    def test_macos_and_windows(self):
        mac = schedule.plan("doctor", 3, self.entry, "darwin", Path("/h"))
        self.assertIn("<integer>3</integer>", next(iter(mac["files"].values())))
        win = schedule.plan("doctor", 3, self.entry, "win32", Path("/h"))
        self.assertIn("03:00", win["commands"][0])

    def test_bad_hour(self):
        with self.assertRaises(ValueError):
            schedule.plan("doctor", 24, self.entry, "linux")


if __name__ == "__main__":
    unittest.main()
