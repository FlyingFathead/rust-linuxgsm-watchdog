import copy
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import rust_watchdog as wd


UTC = timezone.utc


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class ForcedWipeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(wd.DEFAULTS)

    def test_highlight_keeps_current_wipe_during_post_release_window(self):
        info = wd.next_forced_wipe(dt("2026-08-06T18:01:00Z"), self.cfg)
        self.assertEqual(info["cycle"], "2026-08")
        self.assertEqual(wd._utc_iso(info["wipe_utc_dt"]), "2026-08-06T18:00:00Z")

    def test_highlight_moves_to_next_month_after_window(self):
        info = wd.next_forced_wipe(dt("2026-08-06T21:01:00Z"), self.cfg)
        self.assertEqual(info["cycle"], "2026-09")
        self.assertEqual(wd._utc_iso(info["wipe_utc_dt"]), "2026-09-03T18:00:00Z")

    def test_london_schedule_tracks_dst(self):
        summer = wd._forced_wipe_schedule(
            dt("2026-08-01T00:00:00Z"), self.cfg
        )
        winter = wd._forced_wipe_schedule(
            dt("2026-01-01T00:00:00Z"), self.cfg
        )
        self.assertEqual(wd._utc_iso(summer["wipe_utc_dt"]), "2026-08-06T18:00:00Z")
        self.assertEqual(wd._utc_iso(winter["wipe_utc_dt"]), "2026-01-01T19:00:00Z")

    def test_update_parser_retains_build_ids(self):
        result = wd.parse_update_check(
            "\x1b[31mUpdate available\x1b[0m\n"
            "* Local build: 12345678\n"
            "* Remote build: 12345679\n",
            command="check-update",
        )
        self.assertIs(result.verdict, True)
        self.assertEqual(result.local_build, "12345678")
        self.assertEqual(result.remote_build, "12345679")
        self.assertEqual(result.command, "check-update")

    def test_proc_start_time_parser_uses_linux_boot_clock(self):
        fields_after_comm = ["S"] + (["0"] * 18) + ["500"]
        proc_pid_stat = "123 (RustDedicated) " + " ".join(fields_after_comm)
        proc_stat = io.StringIO("cpu 1 2 3\nbtime 1000\n")
        with mock.patch.object(
            wd.Path,
            "read_text",
            return_value=proc_pid_stat,
        ), mock.patch.object(
            wd.os,
            "sysconf",
            return_value=100,
        ), mock.patch(
            "builtins.open",
            return_value=proc_stat,
        ):
            started = wd._proc_started_at_utc(123)
        self.assertEqual(started, "1970-01-01T00:16:45Z")


class ForcedWipeCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = str(Path(self.tmp.name) / "forced_wipe.json")
        self.cfg = copy.deepcopy(wd.DEFAULTS)
        self.cfg.update(
            {
                "forced_wipe_action": "full-wipe",
                "forced_wipe_state_file": self.state_path,
                "forced_wipe_early_release_tolerance_minutes": 15,
                "forced_wipe_action_window_minutes": 360,
            }
        )

    def coordinator(self):
        return wd.ForcedWipeCoordinator(self.cfg, persist=True)

    def test_earlier_same_day_update_becomes_fence_not_candidate(self):
        c = self.coordinator()

        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T12:00:00Z"),
        )
        early = c.observe_update(
            wd.UpdateCheckResult(True, "100", "200"),
            dt("2026-08-06T17:30:00Z"),
        )
        self.assertFalse(early.pending)
        self.assertEqual(c.state["prewipe_remote_build"], "200")

        monthly = c.observe_update(
            wd.UpdateCheckResult(True, "100", "300"),
            dt("2026-08-06T17:50:00Z"),
        )
        self.assertTrue(monthly.armed_now)
        self.assertTrue(monthly.pending)
        self.assertTrue(monthly.hold)
        self.assertFalse(monthly.action_due)
        self.assertEqual(monthly.candidate_remote_build, "300")

        due = c.observe_update(
            wd.UpdateCheckResult(True, "100", "300"),
            dt("2026-08-06T18:00:00Z"),
        )
        self.assertTrue(due.pending)
        self.assertTrue(due.action_due)
        self.assertFalse(due.hold)

        due_without_build_output = c.observe_update(
            wd.UpdateCheckResult(None),
            dt("2026-08-06T18:01:00Z"),
        )
        self.assertTrue(due_without_build_output.pending)
        self.assertTrue(due_without_build_output.action_due)

    def test_late_start_without_fence_refuses_to_arm(self):
        c = self.coordinator()
        decision = c.observe_update(
            wd.UpdateCheckResult(True, "100", "300"),
            dt("2026-08-06T18:05:00Z"),
        )
        self.assertFalse(decision.armed_now)
        self.assertFalse(decision.pending)
        self.assertIn("refusing to arm", decision.reason)
        self.assertEqual(c.state["prewipe_remote_build"], "300")

    def test_completed_cycle_ignores_later_hotfix(self):
        c = self.coordinator()
        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T12:00:00Z"),
        )
        c.observe_update(
            wd.UpdateCheckResult(True, "100", "300"),
            dt("2026-08-06T17:50:00Z"),
        )
        c.mark_wipe_done(dt("2026-08-06T18:05:00Z"))
        self.assertTrue(c.finish_if_running(dt("2026-08-06T18:10:00Z")))

        hotfix = c.observe_update(
            wd.UpdateCheckResult(True, "300", "301"),
            dt("2026-08-06T19:00:00Z"),
        )
        self.assertFalse(hotfix.armed_now)
        self.assertFalse(hotfix.pending)

    def test_window_end_fallback_is_disabled_by_default(self):
        c = self.coordinator()
        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T12:00:00Z"),
        )
        decision = c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-07T00:00:00Z"),
        )
        self.assertFalse(decision.armed_now)
        self.assertFalse(decision.pending)

    def test_window_end_fallback_arms_configured_action_at_cutoff(self):
        self.cfg["forced_wipe_fallback_at_window_end"] = True
        c = self.coordinator()
        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T12:00:00Z"),
        )

        before = c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T23:59:59Z"),
        )
        self.assertFalse(before.armed_now)
        self.assertFalse(before.pending)

        due = c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-07T00:00:00Z"),
        )
        self.assertTrue(due.armed_now)
        self.assertTrue(due.pending)
        self.assertTrue(due.action_due)
        self.assertFalse(due.hold)
        self.assertEqual(due.armed_trigger, "window-end-fallback")
        self.assertEqual(due.candidate_remote_build, "")
        self.assertEqual(c.state["armed_action"], "full-wipe")
        self.assertEqual(c.state["armed_trigger"], "window-end-fallback")

        reloaded = self.coordinator()
        self.assertTrue(reloaded.state["pending"])
        self.assertTrue(reloaded.needs_recovery(dt("2026-08-07T00:01:00Z")))

    def test_window_end_fallback_refuses_retroactive_late_start(self):
        self.cfg["forced_wipe_fallback_at_window_end"] = True
        c = self.coordinator()
        decision = c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-27T12:00:00Z"),
        )
        self.assertFalse(decision.armed_now)
        self.assertFalse(decision.pending)
        self.assertIn("not observed before the cutoff", decision.reason)

    def test_manual_wipe_earlier_on_facepunch_day_suppresses_fallback(self):
        self.cfg["forced_wipe_fallback_at_window_end"] = True
        c = self.coordinator()
        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T08:00:00Z"),
        )
        result = c.mark_manual_complete(
            dt("2026-08-06T10:05:00Z"),
            wiped_at=dt("2026-08-06T10:00:00Z"),
            wipe_kind="full-wipe",
        )
        self.assertTrue(result["completed_cycle"])

        decision = c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-07T00:00:00Z"),
        )
        self.assertFalse(decision.armed_now)
        self.assertFalse(decision.pending)
        self.assertTrue(c.state["completed"])

    def test_completed_fallback_cannot_rearm_on_later_update(self):
        self.cfg["forced_wipe_fallback_at_window_end"] = True
        c = self.coordinator()
        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T12:00:00Z"),
        )
        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-07T00:00:00Z"),
        )
        c.mark_wipe_done(dt("2026-08-07T00:05:00Z"))
        self.assertTrue(c.finish_if_running(dt("2026-08-07T00:10:00Z")))

        later = c.observe_update(
            wd.UpdateCheckResult(True, "100", "101"),
            dt("2026-08-07T01:00:00Z"),
        )
        self.assertFalse(later.armed_now)
        self.assertFalse(later.pending)

    def test_wipe_done_survives_restart(self):
        c = self.coordinator()
        c.state.update(
            {
                "cycle": "2000-01",
                "scheduled_utc": "2000-01-06T19:00:00Z",
                "candidate_remote_build": "300",
                "armed_action": "full-wipe",
                "pending": True,
                "wipe_done": True,
                "wipe_done_at": "2000-01-06T19:05:00Z",
            }
        )
        c._save(dt("2000-01-06T19:05:00Z"))

        reloaded = self.coordinator()
        self.assertTrue(reloaded.state["wipe_done"])
        self.assertTrue(reloaded.needs_recovery(datetime.now(UTC)))


class ForcedWipeReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(wd.DEFAULTS)
        self.cfg.update(
            {
                "forced_wipe_action": "off",
                "forced_wipe_reminder_enabled": True,
                "forced_wipe_reminder_repeat_minutes": 30,
                "forced_wipe_state_file": str(
                    Path(self.tmp.name) / "forced_wipe.json"
                ),
            }
        )

    def coordinator(self):
        return wd.ForcedWipeCoordinator(self.cfg, persist=True)

    def test_due_reminder_repeats_from_persisted_timestamp(self):
        c = self.coordinator()
        first_at = dt("2026-08-06T18:00:00Z")
        first = c.reminder_status(first_at)
        self.assertTrue(first["due"])
        self.assertTrue(first["send_due"])

        with mock.patch.object(wd, "alert") as alert_mock, mock.patch.object(
            wd, "log"
        ):
            self.assertTrue(
                wd.maybe_emit_forced_wipe_reminder(
                    c, self.cfg, now_utc=first_at
                )
            )
            alert_mock.assert_called_once()

        reloaded = self.coordinator()
        self.assertFalse(
            reloaded.reminder_status(
                dt("2026-08-06T18:29:59Z")
            )["send_due"]
        )
        self.assertTrue(
            reloaded.reminder_status(
                dt("2026-08-06T18:30:00Z")
            )["send_due"]
        )

    def test_manual_wipe_timestamp_suppresses_cycle_reminder(self):
        c = self.coordinator()
        now = dt("2026-08-06T20:00:00Z")
        result = c.mark_manual_complete(
            now,
            wiped_at=dt("2026-08-06T18:23:00Z"),
            wipe_kind="full-wipe",
        )
        self.assertTrue(result["completed_cycle"])
        self.assertEqual(result["last_wipe_at"], "2026-08-06T18:23:00Z")
        self.assertEqual(result["last_wipe_age"], "1 hour, 37 minutes ago")
        self.assertFalse(c.reminder_status(now)["due"])

        status = c.status(now)
        self.assertEqual(status["last_wipe_source"], "manual")
        self.assertEqual(status["last_wipe_kind"], "full-wipe")

    def test_last_wipe_survives_monthly_state_rollover(self):
        c = self.coordinator()
        c.mark_manual_complete(
            dt("2026-08-06T20:00:00Z"),
            wiped_at=dt("2026-08-06T18:23:00Z"),
        )

        september = c.status(dt("2026-09-01T00:00:00Z"))
        self.assertEqual(september["cycle"], "2026-09")
        self.assertEqual(
            september["last_wipe_at"],
            "2026-08-06T18:23:00Z",
        )
        self.assertFalse(september["completed"])

        due = c.reminder_status(dt("2026-09-03T18:00:00Z"))
        self.assertTrue(due["due"])

    def test_unknown_wipe_timestamp_has_explicit_fallback(self):
        c = self.coordinator()
        now = dt("2026-08-06T18:00:00Z")
        status = c.reminder_status(now)
        self.assertEqual(
            status["last_wipe_summary"],
            "unknown (no wipe timestamp recorded)",
        )

    def test_restart_timestamp_is_persisted_across_cycle_rollover(self):
        c = self.coordinator()
        observed = c.observe_server_restart(
            "2026-08-01T12:34:56Z",
            dt("2026-08-01T13:00:00Z"),
            source="rust-process-start",
        )
        self.assertTrue(observed)

        reloaded = self.coordinator()
        september = reloaded.status(dt("2026-09-01T00:00:00Z"))
        self.assertEqual(
            september["last_restart_at"],
            "2026-08-01T12:34:56Z",
        )
        self.assertEqual(
            september["last_restart_source"],
            "rust-process-start",
        )

    def test_alert_footnote_uses_long_elapsed_time(self):
        c = self.coordinator()
        c.state["last_wipe_at"] = "2026-07-31T18:09:00Z"
        c.state["last_wipe_source"] = "manual"
        c.state["last_restart_at"] = "2026-07-31T18:09:00Z"
        with mock.patch.object(
            wd,
            "get_server_process_info",
            return_value={"started_at_utc": ""},
        ):
            lines = wd._build_alert_status_footnotes(
                self.cfg,
                coordinator=c,
                now_utc=dt("2026-08-06T18:00:00Z"),
            )
        self.assertEqual(
            lines,
            [
                "Server last wiped: 2026-07-31 18:09:00 UTC "
                "(manual record) "
                "(5 days, 23 hours, 51 minutes ago)",
                "Server last restarted: 2026-07-31 18:09:00 UTC "
                "(5 days, 23 hours, 51 minutes ago)",
            ],
        )

    def test_alert_footnote_handles_both_unknown_timestamps(self):
        c = self.coordinator()
        with mock.patch.object(
            wd,
            "get_server_process_info",
            return_value={"started_at_utc": ""},
        ):
            lines = wd._build_alert_status_footnotes(
                self.cfg,
                coordinator=c,
                now_utc=dt("2026-08-01T00:00:00Z"),
            )
        self.assertEqual(
            lines,
            [
                "Server last wiped: unknown (no wipe timestamp recorded)",
                "Server last restarted: unknown "
                "(no Rust process start timestamp recorded)",
            ],
        )


class ForcedWipeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(wd.DEFAULTS)
        self.cfg.update(
            {
                "forced_wipe_action": "full-wipe",
                "forced_wipe_state_file": str(
                    Path(self.tmp.name) / "forced_wipe.json"
                ),
                "dry_run": False,
            }
        )

    def test_start_failure_retries_start_without_second_wipe(self):
        c = wd.ForcedWipeCoordinator(self.cfg, persist=True)
        c.state.update(
            {
                "cycle": "2000-01",
                "scheduled_utc": "2000-01-06T19:00:00Z",
                "prewipe_remote_build": "100",
                "candidate_remote_build": "300",
                "armed_action": "full-wipe",
                "pending": True,
            }
        )
        c._save(dt("2000-01-06T19:00:00Z"))

        calls = []
        start_attempts = 0

        def run_step(_cfg, _server_dir, _rustserver_path, step, fp=None):
            nonlocal start_attempts
            calls.append(step)
            if step == "start":
                start_attempts += 1
                if start_attempts == 1:
                    return (False, "simulated start failure")
            return (True, "")

        current = wd.UpdateCheckResult(False, "300", "300", "check-update")
        with mock.patch.object(
            wd, "_run_lgsm_step_checked", side_effect=run_step
        ), mock.patch.object(
            wd, "check_server_update_via_lgsm", return_value=current
        ), mock.patch.object(
            wd, "alert"
        ):
            first = wd.execute_forced_wipe_sequence(
                self.cfg,
                "/server",
                "/server/rustserver",
                c,
                server_already_down=False,
            )
            self.assertFalse(first)
            self.assertTrue(c.state["wipe_done"])
            self.assertTrue(c.state["last_wipe_at"])
            self.assertEqual(c.state["last_wipe_source"], "automatic")
            self.assertEqual(c.state["last_wipe_kind"], "full-wipe")

            second = wd.execute_forced_wipe_sequence(
                self.cfg,
                "/server",
                "/server/rustserver",
                c,
                server_already_down=True,
            )
            self.assertTrue(second)

        self.assertEqual(
            calls,
            ["stop", "backup", "update", "mu", "full-wipe", "start", "start"],
        )
        self.assertEqual(calls.count("full-wipe"), 1)

    def test_ambiguous_failed_wipe_is_never_repeated_automatically(self):
        c = wd.ForcedWipeCoordinator(self.cfg, persist=True)
        c.state.update(
            {
                "cycle": "2000-01",
                "scheduled_utc": "2000-01-06T19:00:00Z",
                "prewipe_remote_build": "100",
                "candidate_remote_build": "300",
                "armed_action": "full-wipe",
                "pending": True,
            }
        )
        c._save(dt("2000-01-06T19:00:00Z"))

        calls = []

        def run_step(_cfg, _server_dir, _rustserver_path, step, fp=None):
            calls.append(step)
            if step == "full-wipe":
                return (False, "simulated ambiguous wipe failure")
            return (True, "")

        current = wd.UpdateCheckResult(False, "300", "300", "check-update")
        with mock.patch.object(
            wd, "_run_lgsm_step_checked", side_effect=run_step
        ), mock.patch.object(
            wd, "check_server_update_via_lgsm", return_value=current
        ), mock.patch.object(
            wd, "alert"
        ):
            first = wd.execute_forced_wipe_sequence(
                self.cfg,
                "/server",
                "/server/rustserver",
                c,
                server_already_down=False,
            )
            self.assertFalse(first)
            self.assertTrue(c.state["wipe_started_at"])
            self.assertFalse(c.state["wipe_done"])

            second = wd.execute_forced_wipe_sequence(
                self.cfg,
                "/server",
                "/server/rustserver",
                c,
                server_already_down=True,
            )
            self.assertFalse(second)

        self.assertEqual(calls.count("full-wipe"), 1)


if __name__ == "__main__":
    unittest.main()
