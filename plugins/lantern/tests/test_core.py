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
            env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_DATA", "LANTERN_HOME")}
            with mock.patch.object(paths, "_pointer", return_value=Path(d) / "copy-a"):
                with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": f"{d}/plugin-data"}):
                    self.assertEqual(paths.data_dir(), Path(d) / "plugin-data")  # a hook, inside Claude Code
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(paths.data_dir(), Path(d) / "plugin-data")  # a `lantern` command from a shell
            with mock.patch.object(paths, "_pointer", return_value=Path(d) / "copy-b"):  # a worktree loaded with --plugin-dir
                with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": f"{d}/lantern-inline"}):
                    self.assertEqual(paths.data_dir(), Path(d) / "lantern-inline")
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(paths.data_dir(), Path(d) / "lantern-inline")  # its own commands follow it...
            with mock.patch.object(paths, "_pointer", return_value=Path(d) / "copy-a"), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(paths.data_dir(), Path(d) / "plugin-data")  # ...the installed copy's never moved

    def test_each_plugin_copy_has_its_own_pointer(self):
        a = paths.real_pointer()
        with mock.patch.object(paths, "__file__", "/elsewhere/lantern/lantern_core/paths.py"):
            self.assertNotEqual(paths.real_pointer(), a)
        self.assertEqual(paths.real_pointer(), a)
        self.assertEqual(a.parent, paths._os_default() / "data-dirs")


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
        p = schedule.plan("doctor", 3, self.entry, "linux", Path("/h"), scheduler="systemd")
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
            schedule.plan("doctor", 24, self.entry, "linux", scheduler="systemd")


    def test_inside_a_container_the_timer_is_built_for_the_host(self):
        from lantern_core import discovery
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="/home/linuxbrew/.linuxbrew/bin/uv\n")
        with mock.patch.object(discovery, "HOST_EXEC", "/usr/bin/distrobox-host-exec"), mock.patch.object(schedule.subprocess, "run", run), \
                tempfile.TemporaryDirectory() as d:
            p = schedule.plan("watch", None, self.entry, "linux", Path(d), scheduler="systemd")
            self.assertIn("ExecStart=/home/linuxbrew/.linuxbrew/bin/uv run", next(iter(p["files"].values())))  # the host's uv, not the box's
            schedule.apply(p)
            self.assertIn("OnCalendar=hourly", (Path(d) / ".config/systemd/user/lantern-watch.timer").read_text())
        self.assertEqual(calls[0], ["/usr/bin/distrobox-host-exec", "sh", "-c", "command -v uv"])
        self.assertEqual(calls[-1][:3], ["/usr/bin/distrobox-host-exec", "systemctl", "--user"])  # systemctl asks the host

if __name__ == "__main__":
    unittest.main()


class OwnJobsTest(unittest.TestCase):
    def test_a_failed_nightly_job_is_a_finding_and_a_bare_uv_is_repointed(self):
        from lantern_core import discovery
        entry = Path("/p/lantern.py")
        shows = {"lantern-curate.service": "LoadState=loaded\nExecStart={ path=uv ; argv[]=uv run --no-project x }\nExecMainStatus=203\nExecMainExitTimestamp=Sat 2026-10-31 03:00:00 AEDT\n",
                 "lantern-doctor.service": "LoadState=loaded\nExecStart={ path=/opt/uv ; argv[]=/opt/uv run x }\nExecMainStatus=1\nExecMainExitTimestamp=Sat 2026-10-31 04:00:00 AEDT\n",
                 "lantern-watch.service": "LoadState=not-found\n"}

        def run(cmd):
            if cmd[:3] == ["systemctl", "--user", "show"]:
                return shows[cmd[-1]]
            if cmd[0] == "journalctl":
                return "lantern-doctor.service: Main process exited, code=exited, status=1/FAILURE\nError: no space left\n"
            return ""
        applied = []
        with mock.patch.object(discovery, "_which", return_value="/usr/bin/systemctl"), mock.patch.object(discovery, "_run", side_effect=run), \
                mock.patch.object(schedule, "apply", side_effect=lambda p: applied.append(p)), mock.patch.object(schedule, "_uv", return_value="/opt/uv"), \
                mock.patch.object(schedule.sys, "platform", "linux"), mock.patch.object(schedule, "linux_scheduler", return_value="systemd"):
            found = schedule.real_own_jobs(entry)
        self.assertEqual([k for k, _, _ in found], ["insight/own-job/curate", "insight/own-job/doctor"])
        self.assertIn("now names /opt/uv", found[0][1])  # the bare uv was repointed
        self.assertEqual(len(applied), 1)
        self.assertIn("ExecStart=/opt/uv run", next(iter(applied[0]["files"].values())))
        self.assertIn("failed at Sat 2026-10-31 04:00:00 AEDT (exit 1; Error: no space left)", found[1][1])
        with mock.patch.object(discovery, "_which", return_value=None):
            self.assertEqual(schedule.real_own_jobs(entry), [])  # no systemctl here: nothing to check
