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


class JustWorksTest(unittest.TestCase):
    def test_one_server_per_folder_whatever_the_path(self):
        from lantern_core import semantic
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "data"
            real.mkdir()
            link = Path(d) / "link"
            link.symlink_to(real)
            self.assertEqual(semantic.port_for(real), semantic.port_for(link))
            semantic.write_token(real)
            self.assertTrue((real / f"serve-{semantic.port_for(real)}.token").exists())  # a key per port: never overwritten by another server

    def test_status_line_speaks_plainly_to_a_plain_reader(self):
        from lantern_core import memory, register, stage, status
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            stage.publish(db, "insight/failed-system/logrotate.service", "insight", "logrotate.service (a system unit) has failed", score=4)
            db.close()
            with mock.patch.object(register, "level", return_value="plain"):
                self.assertIn("all well", status.line())  # a system chore they cannot act on
                self.assertNotIn("things known", status.line())
                db = memory.connect()
                stage.publish(db, "insight/disk/1000", "insight", "the volume holding /home/x is 96% full (38 GB of 998 GB left; podman could free 51GB)", score=6)
                db.close()
                self.assertTrue(status.line().endswith("needs you: your drive is nearly full (38 GB left)"), status.line())
            with mock.patch.object(register, "level", return_value="expert"):
                self.assertIn("the volume holding /home/x", status.line())
                self.assertIn("things known", status.line())

    def test_errors_from_code_since_replaced_are_not_news(self):
        from lantern_core import doctor
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            log = Path(d) / "errors.jsonl"
            log.write_text(json.dumps({"ts": time.time() - 3600, "hook": "stop", "error": "NameError"}) + "\n")
            code = Path(doctor.__file__).parent / "doctor.py"
            os.utime(code)  # the code changed after that error
            self.assertEqual(doctor.check_hook_errors()[0], doctor.OK)
            log.write_text(log.read_text() + json.dumps({"ts": time.time() + 5, "hook": "stop", "error": "KeyError"}) + "\n")
            self.assertEqual(doctor.check_hook_errors()[0], doctor.WARN)

    def test_systemd_stamp(self):
        self.assertEqual(time.localtime(schedule._stamp("Sat 2026-10-31 03:00:00 AEDT"))[:5], (2026, 10, 31, 3, 0))
        self.assertEqual(schedule._stamp(""), 0.0)

    def test_casual_health_questions_get_the_snapshot(self):
        for q in ("is everything ok with my computer?", "what should i be worried about? keep it simple", "any problems?", "anything I should know?"):
            self.assertTrue(hooks.HEALTH_ASK.search(q), q)
        for q in ("fix the worry function in app.py", "add a test for the parser"):
            self.assertFalse(hooks.HEALTH_ASK.search(q), q)


class HardeningTest(unittest.TestCase):
    def test_a_job_pointing_at_a_moved_lantern_is_repointed(self):
        from lantern_core import discovery
        shows = {"lantern-curate.service": "LoadState=loaded\nExecStart={ path=/opt/uv ; argv[]=/opt/uv run --no-project /gone/lantern.py job curate }\nExecMainStatus=2\n",
                 "lantern-doctor.service": "LoadState=not-found\n", "lantern-watch.service": "LoadState=not-found\n"}
        applied = []
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(discovery, "_which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(discovery, "_run", side_effect=lambda cmd: shows[cmd[-1]] if cmd[:3] == ["systemctl", "--user", "show"] else ""), \
                mock.patch.object(schedule, "apply", side_effect=lambda p: applied.append(p)), mock.patch.object(schedule, "_uv", return_value="/opt/uv"), \
                mock.patch.object(schedule.sys, "platform", "linux"), mock.patch.object(schedule, "linux_scheduler", return_value="systemd"):
            found = schedule.real_own_jobs(Path(d) / "lantern.py")
        self.assertEqual(len(applied), 1)
        self.assertIn("no longer there; it now runs this one", found[0][1])

    def test_odd_input_never_crashes_a_hook(self):
        from lantern_core import memory
        odd = ["", "null", "[]", '"text"', "42", "{not json", json.dumps({"prompt": None}), json.dumps({"prompt": 5, "session_id": []}),
               json.dumps({"prompt": "x" * 200000, "session_id": "s"}), json.dumps({"prompt": "\udcff\x00‮ never restart plex", "session_id": "s"}),
               json.dumps({"transcript_path": "/nonexistent/t.jsonl", "session_id": "s"}), json.dumps({"tool_name": "Bash", "tool_input": None}),
               json.dumps({"tool_name": "Bash", "tool_input": {"command": None}}), json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}), json.dumps({"tool_name": 5}), json.dumps({"cwd": 5, "source": None})]
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.object(hooks.semantic, "search", return_value=None), mock.patch.object(hooks.resources, "can_spawn", return_value=False):
            garbage = Path(d) / "garbage.jsonl"
            garbage.write_bytes(b"\x00\xff{\n[1,2]\n{\"type\": 5}\n" + json.dumps({"type": "user", "message": {"content": 7}}).encode() + b"\n")
            odd.append(json.dumps({"transcript_path": str(garbage), "session_id": "s", "prompt": "hi there friend"}))
            for name in ("user-prompt-submit", "stop", "stop-nudge", "subagent-stop", "signal", "pre-tool-use"):
                for raw in odd:
                    out = io.StringIO()
                    self.assertEqual(hooks.run(name, io.StringIO(raw), out), 0, (name, raw[:40]))
                    if out.getvalue():
                        json.loads(out.getvalue())  # whatever it says is valid JSON
            log = Path(d) / "errors.jsonl"
            crashes = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
            self.assertEqual([c["hook"] + ": " + c["error"] for c in crashes], [])  # odd input is handled, not crashed on

    def test_a_hook_that_runs_long_stops_itself_and_says_why(self):
        if not hasattr(hooks.signal, "SIGALRM"):
            self.skipTest("no alarm signal on this OS")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}), \
                mock.patch.dict(hooks.BUDGET, {"stop": 1}), mock.patch.dict(hooks.HANDLERS, {"stop": lambda p: time.sleep(5)}):
            t0 = time.monotonic()
            hooks.run("stop", io.StringIO("{}"), io.StringIO())
            self.assertLess(time.monotonic() - t0, 3)
            self.assertIn("TimeoutError", (Path(d) / "errors.jsonl").read_text())

    def test_a_damaged_memory_file_heals_from_its_copy(self):
        from lantern_core import memory
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            db = memory.connect()
            memory.add(db, "rule", "said:s", "Never restart Plex without asking.")
            db.close()
            self.assertIn("memory copied for safety", memory.backup())
            db = memory.connect()
            memory.add(db, "fact", "x", "learned after the copy")
            db.close()
            (Path(d) / "memory.db-wal").unlink(missing_ok=True)
            (Path(d) / "memory.db").write_bytes(b"this is not a database" * 100)
            db = memory.connect()
            self.assertEqual([t for (t,) in db.execute("SELECT text FROM items WHERE kind='rule'")], ["Never restart Plex without asking."])
            db.close()
            self.assertTrue(list(Path(d).glob("memory.db.damaged-*")))  # set aside, never deleted
            ctx = hooks.session_start({"session_id": "s2"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("memory file was damaged and has been restored from its own copy", ctx)
            ctx = hooks.session_start({"session_id": "s3"})["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("was damaged", ctx)  # said once

    def test_a_damaged_file_with_no_copy_starts_fresh_and_says_so(self):
        from lantern_core import memory
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            (Path(d) / "memory.db").write_bytes(b"\x00garbage" * 500)
            memory.connect().close()
            self.assertIn("started fresh", hooks.session_start({"session_id": "s"})["hookSpecificOutput"]["additionalContext"])

    def test_a_locked_memory_is_not_mistaken_for_damage(self):
        from lantern_core import memory
        import sqlite3
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            memory.connect().close()
            with mock.patch.object(memory, "_open", side_effect=sqlite3.OperationalError("database is locked")):
                with self.assertRaises(sqlite3.OperationalError):
                    memory.connect()
            self.assertFalse(list(Path(d).glob("memory.db.damaged-*")))

    def test_an_old_data_folder_opens_and_upgrades(self):
        from lantern_core import memory, status
        import sqlite3
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            old = sqlite3.connect(Path(d) / "memory.db")  # a 0.1 database: items only, no vec column, no ledger, uses or stage
            old.executescript("CREATE TABLE items (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL, "
                              "created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active');"
                              "INSERT INTO items(kind, source, text, created) VALUES ('rule', 'said:s', 'Keep replies short.', 1.0);")
            old.commit()
            old.close()
            (Path(d) / "settings.json").write_text(json.dumps({"depth": 2, "expertise": "general"}))  # a key 0.4 retired
            ctx = hooks.session_start({"session_id": "s"})["hookSpecificOutput"]["additionalContext"]
            self.assertIn("- Keep replies short.", ctx)
            self.assertTrue(status.line().startswith("Lantern:"))
            hooks.user_prompt_submit({"prompt": "keep replies short and plain please", "session_id": "s"})
            db = memory.connect()
            self.assertIn("vec", {r[1] for r in db.execute("PRAGMA table_info(items)")})
            db.close()

    def test_two_sessions_writing_at_once_lose_nothing(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            script = ("import sys; sys.path.insert(0, %r)\n"
                      "from lantern_core import memory, discovery\n"
                      "n = sys.argv[1]\n"
                      "for i in range(40):\n"
                      "    db = memory.connect(); memory.add(db, 'turn', 's' + n, f'{n}-{i}'); db.close()\n"
                      "    discovery.update_settings(**{'k' + n: i})\n") % str(Path(__file__).resolve().parents[1])
            env = {**os.environ, "CLAUDE_PLUGIN_DATA": d}
            procs = [sp.Popen([sys.executable, "-c", script, str(n)], env=env) for n in range(4)]
            self.assertEqual([p.wait(timeout=120) for p in procs], [0, 0, 0, 0])
            with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
                from lantern_core import discovery, memory
                db = memory.connect()
                self.assertEqual(db.execute("SELECT count(*) FROM items WHERE kind='turn'").fetchone()[0], 160)
                db.close()
                self.assertEqual({k: v for k, v in discovery.settings().items() if k.startswith("k")}, {"k0": 39, "k1": 39, "k2": 39, "k3": 39})

    def test_a_full_disk_is_said_not_swallowed(self):
        from lantern_core import status
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": d}):
            with mock.patch.object(hooks, "_disk_full", return_value=True):
                self.assertIn("disk is full", status.line())
                with mock.patch.object(hooks, "_log", side_effect=OSError("No space left on device")):
                    out = hooks._crashed("stop", OSError("No space left on device"))
                self.assertIn("disk is full", out["systemMessage"])


class LoopsTest(unittest.TestCase):
    def test_a_mention_does_not_close_an_offer(self):
        from lantern_core import memory
        offer = "Next, I can save the 17 Hermes changes as a version right now, which keeps everything on this machine"
        self.assertFalse(memory.addresses("Hermes Orchestrator isn't covered either, so my earlier warning about it stands.", offer))
        self.assertTrue(memory.addresses("I saved the 17 Hermes changes as a version (abc123).", offer))
        self.assertTrue(memory.addresses("Do you still want me to save the Hermes changes as a version?", offer))  # asked again

    def test_a_bare_question_keeps_what_it_is_about(self):
        from lantern_core import memory
        loops = memory.open_loops("Hermes has 17 unsaved changes. Next, I can save them as a version right now. Want me to?")
        self.assertEqual(loops, ["Next, I can save them as a version right now. Want me to?"])

    def test_offers_outlast_small_talk_when_a_reply_has_many(self):
        from lantern_core import memory
        reply = "Next, I can save the Hermes changes now. " + " ".join(f"Is item {n} right?" for n in range(12))
        self.assertIn("Next, I can save the Hermes changes now", memory.open_loops(reply))
