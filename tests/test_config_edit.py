import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import rust_watchdog as wd


class ConfigEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "rust_watchdog.json"

    def write_config(self, document):
        self.config_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_home_user_rewrites_only_absolute_home_path_values(self):
        original = {
            "server_dir": "/home/oldrust",
            "identity": "oldrust",
            "lockfile": "/home/another-user/watchdog/data/lock",
            "nested": {
                "paths": [
                    "/home/third/.config/rust",
                    "relative/path",
                    "prefix /home/not-a-path/value",
                    "/homeuser/not-a-home-directory",
                ]
            },
        }
        self.write_config(original)
        os.chmod(self.config_path, 0o640)

        result = wd.edit_config_file(
            str(self.config_path),
            change_home_user="rustnewone",
        )

        self.assertTrue(result["changed"])
        self.assertEqual(len(result["home_matches"]), 3)
        self.assertEqual(
            [match["path"] for match in result["home_matches"]],
            ["$.server_dir", "$.lockfile", "$.nested.paths[0]"],
        )
        updated = self.read_config()
        self.assertEqual(updated["server_dir"], "/home/rustnewone")
        self.assertEqual(
            updated["lockfile"],
            "/home/rustnewone/watchdog/data/lock",
        )
        self.assertEqual(
            updated["nested"]["paths"][0],
            "/home/rustnewone/.config/rust",
        )
        self.assertEqual(updated["identity"], "oldrust")
        self.assertEqual(
            updated["nested"]["paths"][2],
            "prefix /home/not-a-path/value",
        )
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o640)

        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.is_file())
        self.assertEqual(
            json.loads(backup_path.read_text(encoding="utf-8")),
            original,
        )

    def test_same_home_user_reports_matches_without_rewriting(self):
        self.write_config(
            {
                "server_dir": "/home/rustserver",
                "logfile": "/home/rustserver/watchdog.log",
            }
        )
        result = wd.edit_config_file(
            str(self.config_path),
            change_home_user="rustserver",
        )
        self.assertFalse(result["changed"])
        self.assertEqual(len(result["home_matches"]), 2)
        self.assertEqual(result["backup_path"], "")

    def test_invalid_home_user_is_rejected_without_touching_config(self):
        original = {"server_dir": "/home/rustserver"}
        self.write_config(original)
        with self.assertRaises(ValueError):
            wd.edit_config_file(
                str(self.config_path),
                change_home_user="../root",
            )
        self.assertEqual(self.read_config(), original)

    def test_forced_wipe_full_wins_when_both_switches_are_enabled(self):
        self.write_config({"forced_wipe_action": "off"})
        result = wd.edit_config_file(
            str(self.config_path),
            full_wipe_wipeday=True,
            map_wipe_wipeday=True,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            self.read_config()["forced_wipe_action"],
            "full-wipe",
        )
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("full-wipe takes precedence", result["warnings"][0])

    def test_boolean_switches_can_move_from_full_to_map(self):
        self.write_config({"forced_wipe_action": "full-wipe"})
        result = wd.edit_config_file(
            str(self.config_path),
            full_wipe_wipeday=False,
            map_wipe_wipeday=True,
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            self.read_config()["forced_wipe_action"],
            "map-wipe",
        )
        self.assertEqual(result["warnings"], [])

    def test_primary_action_interface_sets_single_config_value(self):
        self.write_config({"forced_wipe_action": "off"})
        result = wd.edit_config_file(
            str(self.config_path),
            set_forced_wipe_action="map-wipe",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(
            self.read_config()["forced_wipe_action"],
            "map-wipe",
        )

    def test_cli_alias_combines_edits_and_exits_before_watchdog_start(self):
        self.write_config(
            {
                "server_dir": "/home/oldrust",
                "forced_wipe_action": "off",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(wd.__file__).resolve()),
                "--config",
                str(self.config_path),
                "--changeuser",
                "rustnewone",
                "--map-wipe-wipeday",
                "on",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("HOME PATH MATCHES: 1", completed.stdout)
        self.assertIn(
            "CONFIG CHANGE: $.forced_wipe_action: off -> map-wipe",
            completed.stdout,
        )
        updated = self.read_config()
        self.assertEqual(updated["server_dir"], "/home/rustnewone")
        self.assertEqual(updated["forced_wipe_action"], "map-wipe")


if __name__ == "__main__":
    unittest.main()
