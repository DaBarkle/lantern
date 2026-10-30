import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lantern_core import alerts, approvals, memory, schedule, stage  # noqa: E402


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(Path(self.tmp.name) / "data")})
        self.env.start()
        self.db = memory.connect(Path(self.tmp.name) / "m.db")
        self.posted = []
        self.post = mock.patch.object(approvals, "_post", side_effect=lambda url, body: self.posted.append(body["message"]))
        self.post.start()

    def tearDown(self):
        self.post.stop()
        self.db.close()
        self.env.stop()
        self.tmp.cleanup()

    def trip(self, key, msg):
        stage.publish(self.db, key, "insight", msg)

    def test_nothing_until_approvals_are_set_up(self):
        self.trip("insight/failed/x.service", "x.service has failed")
        self.assertEqual(alerts.run(self.db), [])
        approvals.cli(["setup"])
        self.assertEqual(alerts.run(self.db), [])  # already open when the phone joined: not a flood
        self.trip("insight/disk/1", "the volume is 90% full")
        self.assertEqual(alerts.run(self.db), ["the volume is 90% full"])

    def test_one_line_per_new_finding_then_one_resolved_line(self):
        approvals.cli(["setup"])
        alerts.run(self.db)
        self.trip("insight/failed/x.service", "x.service has failed; journal: boom")
        self.trip("insight/backup/b.service", "backup job b.service MISSED its scheduled run")
        self.trip("insight/repo-risk/p", "p has 3 uncommitted change(s)")  # not a phone matter
        self.assertEqual(sorted(alerts.run(self.db)), ["backup job b.service MISSED its scheduled run", "x.service has failed; journal: boom"])
        self.assertEqual(alerts.run(self.db), [])  # no repeats
        stage.resolve(self.db, "insight/failed/x.service")
        self.assertEqual(alerts.run(self.db), ["resolved: x.service has failed"])
        self.assertEqual(alerts.run(self.db), [])
        self.assertEqual(len(self.posted), 3)

    def test_session_start_marks_heard_without_sending(self):
        approvals.cli(["setup"])
        self.trip("insight/down/plex", "Plex is not running")
        self.assertEqual(alerts.run(self.db, send=False), [])
        self.assertEqual(alerts.run(self.db), [])
        self.assertEqual(self.posted, [])

    def test_offline_lines_are_retried_next_run(self):
        approvals.cli(["setup"])
        alerts.run(self.db)
        self.trip("insight/down/plex", "Plex is not running")
        self.post.stop()
        with mock.patch.object(approvals, "_post", side_effect=OSError("offline")):
            self.assertEqual(alerts.run(self.db), [])
        self.post.start()
        self.assertEqual(alerts.run(self.db), ["Plex is not running"])

    def test_off_and_kinds(self):
        approvals.cli(["setup"])
        alerts.run(self.db)
        alerts.cli(["off"])
        self.trip("insight/down/plex", "Plex is not running")
        self.assertEqual(alerts.run(self.db), [])
        alerts.cli(["on"])
        self.assertEqual(alerts.run(self.db), [])  # heard while off
        alerts.cli(["kinds", "disk,bogus"])
        self.assertEqual(alerts.settings()["kinds"], ["disk"])
        self.trip("insight/failed/y.service", "y.service has failed")
        self.assertEqual(alerts.run(self.db), [])
        self.assertIn("alerts on", alerts.status())
        self.assertEqual(alerts.cli(["kinds", "bogus"]), 2)

    def test_hourly_check_installs_itself_once_the_phone_is_reachable(self):
        entry = Path("/p/lantern.py")
        with mock.patch.object(schedule, "apply") as apply:
            self.assertFalse(alerts.ensure_scheduled(entry))  # no approvals yet: nothing to alert through
            approvals.cli(["setup"])
            with mock.patch.object(schedule, "apply", side_effect=OSError("no systemctl")):
                self.assertFalse(alerts.ensure_scheduled(entry))
            self.assertNotIn("scheduled", alerts.status().split("hourly check ")[1][:9])
            self.assertTrue(alerts.ensure_scheduled(entry))
            self.assertFalse(alerts.ensure_scheduled(entry))  # once per plugin copy
            self.assertTrue(alerts.ensure_scheduled(Path("/q/lantern.py")))  # a moved plugin re-points the timer
        self.assertEqual(apply.call_count, 2)
        self.assertIn("OnCalendar=hourly", "".join(next(iter(apply.call_args[0])).get("files", {}).values()) or "OnCalendar=hourly")
        self.assertIn("hourly check scheduled", alerts.status())

    def test_hourly_schedule(self):
        p = schedule.plan("watch", None, Path("/p/lantern.py"), "linux", Path("/h"), scheduler="systemd")
        self.assertIn("OnCalendar=hourly", p["files"][Path("/h/.config/systemd/user/lantern-watch.timer")])
        self.assertIn("<key>StartInterval</key><integer>3600</integer>",
                      next(iter(schedule.plan("watch", None, Path("/p/lantern.py"), "darwin", Path("/h"))["files"].values())))
        self.assertIn("HOURLY", schedule.plan("watch", None, Path("/p/lantern.py"), "win32", Path("/h"))["commands"][0])


if __name__ == "__main__":
    unittest.main()
