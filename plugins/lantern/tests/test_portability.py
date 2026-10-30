"""Lantern on machines unlike the one it was built on: a hook that crashes never blocks a prompt, free memory is read on
every OS, a server that will not start is reported, and nothing in the core assumes Fedora, systemd or a desktop keyring."""
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

from lantern_core import curate, doctor, hooks, paths, resources, shield  # noqa: E402

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               12345.
Pages active:                            500000.
Pages inactive:                          100000.
Pages speculative:                         5000.
Pages throttled:                              0.
"""


class HookCrashTest(unittest.TestCase):
    def test_a_crashing_hook_is_logged_and_said_once(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": tmp}), \
                mock.patch.dict(hooks.HANDLERS, {"stop": mock.Mock(side_effect=RuntimeError("no such module: fts5"))}):
            out = io.StringIO()
            self.assertEqual(hooks.run("stop", io.StringIO("{}"), out), 0)
            self.assertIn("no such module: fts5", json.loads(out.getvalue())["systemMessage"])
            out = io.StringIO()
            self.assertEqual(hooks.run("stop", io.StringIO("{}"), out), 0)
            self.assertEqual(out.getvalue(), "")  # said once an hour, not on every turn
            errors = [json.loads(l) for l in Path(tmp, "errors.jsonl").read_text().splitlines()]
            self.assertEqual(len(errors), 2)
            self.assertEqual(errors[0]["hook"], "stop")
            status, msg = doctor.check_hook_errors()
            self.assertEqual(status, doctor.WARN)
            self.assertIn("2 hook error(s)", msg)

    def test_the_shield_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": tmp}), \
                mock.patch.dict(hooks.HANDLERS, {"pre-tool-use": mock.Mock(side_effect=RuntimeError("x"))}):
            with self.assertRaises(RuntimeError):
                hooks.run("pre-tool-use", io.StringIO("{}"), io.StringIO())


class ResourcesTest(unittest.TestCase):
    def test_macos_free_memory_from_vm_stat(self):
        with mock.patch.object(resources.sys, "platform", "darwin"), \
                mock.patch("subprocess.run", return_value=mock.Mock(stdout=VM_STAT)):
            self.assertEqual(resources.available_mb(), (12345 + 100000 + 5000) * 16384 // (1024 * 1024))

    def test_unreadable_means_no_limit(self):
        with mock.patch.object(resources.sys, "platform", "darwin"), mock.patch("subprocess.run", side_effect=OSError):
            self.assertIsNone(resources.available_mb())
            self.assertTrue(resources.can_spawn())


class ServerTest(unittest.TestCase):
    def test_a_server_that_did_not_come_up_is_reported_not_restarted(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": tmp}), \
                mock.patch.object(doctor.semantic, "search", return_value=None), mock.patch.object(doctor.resources, "can_spawn", return_value=True):
            marker = Path(tmp, "serve.started")
            marker.touch()
            os.utime(marker, (time.time() - 200, time.time() - 200))
            Path(tmp, "serve.log").write_text("ImportError: no onnxruntime wheel for this platform\n")
            status, msg = doctor.check_semantic()
            self.assertEqual(status, doctor.WARN)
            self.assertIn("no onnxruntime wheel", msg)
            self.assertIn("keyword-only", msg)

    def test_uv_is_the_one_that_runs_us(self):
        with mock.patch.dict(os.environ, {"UV": "/opt/x/uv"}):
            self.assertEqual(paths.uv(), "/opt/x/uv")
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "UV"}, clear=True), \
                mock.patch.object(paths.shutil, "which", return_value=None):
            self.assertEqual(paths.uv(), "uv")


class NothingFedoraTest(unittest.TestCase):
    def test_guardrails_and_lookalikes_name_no_distro_unless_present(self):
        self.assertFalse(any("ostree" in g for g in hooks.GUARDRAILS))
        with mock.patch.object(hooks.Path, "exists", return_value=False):
            self.assertEqual(hooks.lookalikes(), "")
        with mock.patch.object(hooks.Path, "exists", return_value=True):
            self.assertIn("/var/home", hooks.lookalikes())

    def test_project_folder_is_found_for_any_path_and_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": tmp, "CLAUDE_PLUGIN_DATA": f"{tmp}/d"}):
            folder = Path(tmp, "projects", "C--Users-dave-my-project")
            folder.mkdir(parents=True)
            with mock.patch.object(hooks.transcript, "last_turn", return_value={"user": "", "reply": "", "files": [], "commands": [], "agents": [], "failed": 0}) as lt:
                Path(folder, "abc.jsonl").write_text("{}\n")
                hooks.capture_last_session(r"C:\Users\dave\my.project", "current")
                self.assertEqual(lt.call_count, 1)

    def test_claude_binary_fallbacks_exist_per_os(self):
        with mock.patch.object(curate.shutil, "which", return_value=None):
            self.assertTrue(curate.claude_bin().endswith(("claude", "claude.exe", "claude.cmd")))


class KeychainTest(unittest.TestCase):
    def test_headless_linux_has_no_keychain_and_says_so(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")}
        with mock.patch.object(shield.sys, "platform", "linux"), mock.patch.dict(os.environ, {**env, "XDG_RUNTIME_DIR": "/nonexistent"}, clear=True):
            self.assertIn("no desktop keyring", shield.keychain_missing())
        with mock.patch.object(shield.sys, "platform", "darwin"):
            self.assertIsNone(shield.keychain_missing())

    def test_data_dir_is_private(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permissions")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": f"{tmp}/d"}):
            self.assertEqual(paths.ensure_data_dir().stat().st_mode & 0o777, 0o700)



class CronTest(unittest.TestCase):
    """Linux without a systemd user session (WSL, servers over SSH, Alpine): the jobs go into the user's crontab."""

    def test_cron_plan_replaces_only_its_own_line(self):
        from lantern_core import schedule

        def run(argv, **kw):
            if argv[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout="MAILTO=me\n0 2 * * * /usr/bin/backup # mine\n0 * * * * old # lantern-watch\n")
            return mock.Mock(returncode=1, stdout="")
        with mock.patch.object(schedule.subprocess, "run", run), mock.patch.object(schedule.discovery, "HOST_EXEC", None):
            p = schedule.plan("watch", None, Path("/p/lantern.py"), "linux", Path("/h"), data=Path("/d"), scheduler="cron")
            text = p["files"][Path("/d/crontab")]
            self.assertIn("MAILTO=me\n0 2 * * * /usr/bin/backup # mine\n", text)
            self.assertNotIn("old", text)
            self.assertRegex(text, r"\n0 \* \* \* \* \S*uv run --no-project --quiet /p/lantern.py job watch --data /d >/dev/null 2>&1 # lantern-watch\n$")
            self.assertEqual(p["commands"], [["crontab", "/d/crontab"]])
            daily = schedule.plan("curate", 3, Path("/p/lantern.py"), "linux", Path("/h"), data=Path("/d"), scheduler="cron")
            self.assertIn("\n0 3 * * * ", daily["files"][Path("/d/crontab")])

    def test_scheduler_is_probed_when_not_named(self):
        from lantern_core import schedule
        with mock.patch.object(schedule.subprocess, "run", side_effect=OSError), mock.patch.object(schedule.discovery, "HOST_EXEC", None):
            with mock.patch.object(schedule.discovery, "_which", return_value="/usr/bin/crontab"):
                self.assertEqual(schedule.linux_scheduler(), "cron")
            with mock.patch.object(schedule.discovery, "_which", return_value=None):
                self.assertIsNone(schedule.linux_scheduler())
                with self.assertRaises(OSError):  # alerts.ensure_scheduled catches this and falls back to in-session checks
                    schedule.plan("watch", None, Path("/p/lantern.py"), "linux", Path("/h"), data=Path("/d"))
        with mock.patch.object(schedule.subprocess, "run", return_value=mock.Mock(returncode=0)), mock.patch.object(schedule.discovery, "HOST_EXEC", None):
            self.assertEqual(schedule.linux_scheduler(), "systemd")


if __name__ == "__main__":
    unittest.main()
