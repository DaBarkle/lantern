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
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(paths.Path, "home", return_value=Path("/h")), \
                mock.patch.object(paths, "_pointer", return_value=Path("/nonexistent/data-dir")):
            for platform, want in [("linux", "/h/.local/share/lantern"),
                                   ("darwin", "/h/Library/Application Support/lantern"),
                                   ("win32", "/h/AppData/Roaming/lantern")]:
                with mock.patch.object(paths.sys, "platform", platform):
                    self.assertEqual(paths.data_dir(), Path(want))


class PointerTest(unittest.TestCase):
    def test_shell_commands_find_the_sessions_data_folder(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(paths, "_pointer", return_value=Path(d) / "default" / "data-dir"):
                with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": f"{d}/plugin-data"}):
                    self.assertEqual(paths.data_dir(), Path(d) / "plugin-data")  # a hook, inside Claude Code
                env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_DATA", "LANTERN_HOME")}
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(paths.data_dir(), Path(d) / "plugin-data")  # a `lantern` command from a shell
                with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": f"{d}/lantern-inline"}):
                    self.assertEqual(paths.data_dir(), Path(d) / "lantern-inline")  # a folder-loaded trial run
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(paths.data_dir(), Path(d) / "plugin-data")  # ...never redirected the shell


class ProfileTest(unittest.TestCase):
    def test_folder_profile_keeps_its_own_data(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": f"{d}/data"}):
            proj = Path(d) / "proj" / "sub"
            proj.mkdir(parents=True)
            with mock.patch.object(paths.Path, "cwd", return_value=proj):
                self.assertEqual(paths.data_dir(), Path(d) / "data")
                (proj.parent / ".lantern.json").write_text('{"profile": "fresh"}')
                self.assertEqual(paths.data_dir(), Path(d) / "data" / "profiles" / "fresh")
                (proj.parent / ".lantern.json").write_text('{"profile": "../evil"}')
                self.assertEqual(paths.data_dir(), Path(d) / "data")  # a bad name is ignored, never a path


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
        service = p["files"][Path("/h/.config/systemd/user/lantern-doctor.service")]
        self.assertIn("ExecStart=/", service)  # a full path: systemd's PATH has no uv
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
