import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import rust_watchdog as wd


UTC = timezone.utc


def dt(value: str) -> datetime:
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(UTC)


def serverinfo_frame(save_created_time: str) -> str:
    message = json.dumps(
        {
            "Hostname": "HORSLAND - THE ONE AND ONLY",
            "Uptime": 394044,
            "SaveCreatedTime": save_created_time,
            "Version": 2631,
        },
        indent=2,
    )
    return json.dumps(
        {
            "Message": message,
            "Identifier": 496469688,
            "Type": "Generic",
            "Stacktrace": "",
        }
    )


class ServerInfoSaveCreatedTimeTests(unittest.TestCase):
    def test_extracts_nested_message_json_from_real_response_shape(self):
        response = serverinfo_frame("07/02/2026 18:25:08")
        self.assertEqual(
            wd.extract_serverinfo_save_created_time(response),
            "2026-07-02T18:25:08Z",
        )

    def test_accepts_direct_serverinfo_json_and_iso_timestamp(self):
        response = json.dumps(
            {"SaveCreatedTime": "2026-07-02T18:25:08+00:00"}
        )
        self.assertEqual(
            wd.extract_serverinfo_save_created_time(response),
            "2026-07-02T18:25:08Z",
        )

    def test_invalid_or_missing_timestamp_is_rejected(self):
        self.assertEqual(
            wd.extract_serverinfo_save_created_time(
                serverinfo_frame("not-a-date")
            ),
            "",
        )
        self.assertEqual(
            wd.extract_serverinfo_save_created_time(
                json.dumps({"Message": json.dumps({"Uptime": 10})})
            ),
            "",
        )
        self.assertEqual(
            wd.extract_serverinfo_save_created_time("not-json"),
            "",
        )


class RconWipeLedgerTests(unittest.TestCase):
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
                "server_dir": self.tmp.name,
                "identity": "rustserver",
                "wipe_timestamp_rcon_enabled": True,
                "wipe_timestamp_filesystem_fallback_enabled": True,
            }
        )

    def coordinator(self):
        return wd.ForcedWipeCoordinator(self.cfg, persist=True)

    def write_identity_file(self, name: str, timestamp: str) -> Path:
        path = (
            Path(self.tmp.name)
            / "serverfiles"
            / "server"
            / "rustserver"
            / name
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
        epoch = dt(timestamp).timestamp()
        os.utime(path, (epoch, epoch))
        return path

    def test_refresh_initializes_and_persists_last_wipe_ledger(self):
        c = self.coordinator()
        response = serverinfo_frame("07/02/2026 18:25:08")

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(True, response),
        ) as rcon_send:
            result = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )

        self.assertEqual(
            result,
            (True, "2026-07-02T18:25:08Z", True),
        )
        rcon_send.assert_called_once_with(
            self.cfg,
            "serverinfo",
            fp=None,
        )
        self.assertEqual(
            c.state["last_wipe_at"],
            "2026-07-02T18:25:08Z",
        )
        self.assertEqual(
            c.state["last_wipe_source"],
            "rcon-save-created",
        )
        self.assertEqual(c.state["last_wipe_kind"], "unknown")

        with mock.patch.object(
            wd,
            "get_server_process_info",
            return_value={"started_at_utc": ""},
        ):
            footnotes = wd._build_alert_status_footnotes(
                self.cfg,
                coordinator=c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )
        self.assertEqual(
            footnotes[0],
            "Server last wiped: 2026-07-02 18:25:08 UTC "
            "(RCON) "
            "(24 days, 19 hours, 41 minutes ago)",
        )

        reloaded = self.coordinator()
        self.assertEqual(
            reloaded.state["last_wipe_at"],
            "2026-07-02T18:25:08Z",
        )

    def test_same_timestamp_preserves_explicit_kind_and_source(self):
        c = self.coordinator()
        c.state["last_wipe_at"] = "2026-07-02T18:25:08Z"
        c.state["last_wipe_source"] = "manual"
        c.state["last_wipe_kind"] = "full-wipe"
        c._save(dt("2026-07-02T18:30:00Z"))

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(
                True,
                serverinfo_frame("07/02/2026 18:25:08"),
            ),
        ):
            result = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )

        self.assertEqual(
            result,
            (True, "2026-07-02T18:25:08Z", True),
        )
        self.assertEqual(c.state["last_wipe_source"], "manual")
        self.assertEqual(c.state["last_wipe_kind"], "full-wipe")
        self.assertTrue(c.state["completed"])

    def test_nearby_newer_save_time_preserves_explicit_wipe_metadata(self):
        c = self.coordinator()
        c.state["last_wipe_at"] = "2026-08-06T18:23:00Z"
        c.state["last_wipe_source"] = "automatic"
        c.state["last_wipe_kind"] = "full-wipe"
        c._save(dt("2026-08-06T18:23:00Z"))

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(
                True,
                serverinfo_frame("08/06/2026 18:25:08"),
            ),
        ):
            result = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-08-06T18:30:00Z"),
            )

        self.assertEqual(
            result,
            (True, "2026-08-06T18:25:08Z", True),
        )
        self.assertEqual(
            c.state["last_wipe_at"],
            "2026-08-06T18:25:08Z",
        )
        self.assertEqual(c.state["last_wipe_source"], "automatic")
        self.assertEqual(c.state["last_wipe_kind"], "full-wipe")

    def test_older_save_timestamp_never_regresses_newer_ledger(self):
        c = self.coordinator()
        c.state["last_wipe_at"] = "2026-07-20T10:00:00Z"
        c.state["last_wipe_source"] = "manual"
        c.state["last_wipe_kind"] = "map-wipe"

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(
                True,
                serverinfo_frame("07/02/2026 18:25:08"),
            ),
        ):
            result = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )

        self.assertEqual(
            result,
            (True, "2026-07-02T18:25:08Z", False),
        )
        self.assertEqual(
            c.state["last_wipe_at"],
            "2026-07-20T10:00:00Z",
        )
        self.assertEqual(c.state["last_wipe_kind"], "map-wipe")

    def test_detected_current_cycle_wipe_cancels_pending_action(self):
        c = self.coordinator()
        c.observe_update(
            wd.UpdateCheckResult(False, "100", "100"),
            dt("2026-08-06T12:00:00Z"),
        )
        armed = c.observe_update(
            wd.UpdateCheckResult(True, "100", "300"),
            dt("2026-08-06T17:50:00Z"),
        )
        self.assertTrue(armed.pending)

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(
                True,
                serverinfo_frame("08/06/2026 18:05:00"),
            ),
        ):
            result = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-08-06T18:10:00Z"),
            )

        self.assertEqual(
            result,
            (True, "2026-08-06T18:05:00Z", True),
        )
        self.assertFalse(c.state["pending"])
        self.assertTrue(c.state["wipe_done"])
        self.assertTrue(c.state["start_done"])
        self.assertTrue(c.state["completed"])
        self.assertEqual(
            c.state["completion_source"],
            "rcon-save-created",
        )
        self.assertFalse(c.needs_recovery(dt("2026-08-06T18:11:00Z")))

    def test_detected_current_cycle_wipe_suppresses_off_mode_reminder(self):
        self.cfg["forced_wipe_action"] = "off"
        c = self.coordinator()

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(
                True,
                serverinfo_frame("07/02/2026 18:25:08"),
            ),
        ):
            result = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )

        self.assertTrue(result[0])
        self.assertTrue(c.state["completed"])
        reminder = c.reminder_status(dt("2026-07-27T14:07:00Z"))
        self.assertFalse(reminder["due"])
        self.assertFalse(reminder["send_due"])

    def test_failed_or_future_rcon_observation_does_not_mutate_ledger(self):
        c = self.coordinator()
        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(False, "simulated RCON failure"),
        ):
            failed = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )
        self.assertFalse(failed[0])
        self.assertEqual(c.state["last_wipe_at"], "")

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(
                True,
                serverinfo_frame("07/28/2026 18:25:08"),
            ),
        ):
            future = wd._refresh_server_wipe_ledger_from_rcon(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )
        self.assertFalse(future[0])
        self.assertIn("future", future[2])
        self.assertEqual(c.state["last_wipe_at"], "")

    def test_filesystem_map_mtime_falls_back_when_rcon_fails(self):
        c = self.coordinator()
        map_path = self.write_identity_file(
            "proceduralmap.4500.12345.2631.map",
            "2026-07-02T18:24:55Z",
        )

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(False, "simulated RCON failure"),
        ):
            result = wd._refresh_server_wipe_ledger(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )

        self.assertEqual(
            result,
            (True, "2026-07-02T18:24:55Z", True),
        )
        self.assertEqual(
            c.state["last_wipe_at"],
            "2026-07-02T18:24:55Z",
        )
        self.assertEqual(
            c.state["last_wipe_source"],
            "filesystem-map-mtime",
        )
        self.assertEqual(c.state["last_wipe_kind"], "unknown")
        self.assertEqual(
            wd._filesystem_map_wipe_timestamp(self.cfg),
            ("2026-07-02T18:24:55Z", str(map_path)),
        )
        with mock.patch.object(
            wd,
            "get_server_process_info",
            return_value={"started_at_utc": ""},
        ):
            footnotes = wd._build_alert_status_footnotes(
                self.cfg,
                coordinator=c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )
        self.assertEqual(
            footnotes[0],
            "Server last wiped: 2026-07-02 18:24:55 UTC "
            "(map file mtime) "
            "(24 days, 19 hours, 41 minutes ago)",
        )

    def test_alert_wipe_source_labels_cover_persisted_record_types(self):
        c = self.coordinator()
        c.state["last_wipe_at"] = "2026-07-02T18:25:08Z"

        expected = {
            "manual": "manual record",
            "automatic": "watchdog automatic wipe",
            "legacy-import": "legacy-import",
            "": "",
        }
        with mock.patch.object(
            wd,
            "get_server_process_info",
            return_value={"started_at_utc": ""},
        ):
            for source, label in expected.items():
                with self.subTest(source=source):
                    c.state["last_wipe_source"] = source
                    footnotes = wd._build_alert_status_footnotes(
                        self.cfg,
                        coordinator=c,
                        now_utc=dt("2026-07-27T14:06:28Z"),
                    )
                    suffix = f" ({label})" if label else ""
                    self.assertEqual(
                        footnotes[0],
                        "Server last wiped: "
                        "2026-07-02 18:25:08 UTC"
                        f"{suffix} "
                        "(24 days, 19 hours, 41 minutes ago)",
                    )

    def test_rcon_wins_over_newer_filesystem_map_timestamp(self):
        c = self.coordinator()
        self.write_identity_file(
            "proceduralmap.4500.12345.2631.map",
            "2026-07-20T10:00:00Z",
        )

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(
                True,
                serverinfo_frame("07/02/2026 18:25:08"),
            ),
        ):
            result = wd._refresh_server_wipe_ledger(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )

        self.assertEqual(
            result,
            (True, "2026-07-02T18:25:08Z", True),
        )
        self.assertEqual(
            c.state["last_wipe_at"],
            "2026-07-02T18:25:08Z",
        )
        self.assertEqual(
            c.state["last_wipe_source"],
            "rcon-save-created",
        )

    def test_filesystem_fallback_never_uses_active_save_mtime(self):
        c = self.coordinator()
        self.write_identity_file(
            "proceduralmap.4500.12345.2631.sav",
            "2026-07-27T14:00:00Z",
        )

        with mock.patch.object(
            wd,
            "rcon_send",
            return_value=(False, "simulated RCON failure"),
        ):
            result = wd._refresh_server_wipe_ledger(
                self.cfg,
                c,
                now_utc=dt("2026-07-27T14:06:28Z"),
            )

        self.assertFalse(result[0])
        self.assertIn("no .map file", result[2])
        self.assertEqual(c.state["last_wipe_at"], "")


if __name__ == "__main__":
    unittest.main()
