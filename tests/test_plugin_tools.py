import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from unittest import mock


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import oxide_plugins_inventory as inventory  # noqa: E402
import oxide_plugin_updater as checker  # noqa: E402
import umod_plugins_check as legacy_checker  # noqa: E402


def plugin_source(
    *,
    name="Example Plugin",
    author="Example Author",
    version="1.0.0",
    class_name="ExamplePlugin",
    padding=200,
):
    return (
        "using System;\n"
        "namespace Oxide.Plugins\n"
        "{\n"
        f'    [Info("{name}", "{author}", "{version}")]\n'
        f"    public class {class_name} : RustPlugin\n"
        "    {\n"
        "        private void Init() { }\n"
        "    }\n"
        "}\n"
        + ("/" * padding)
        + "\n"
    )


class PluginToolDefaultPathTests(unittest.TestCase):
    def test_both_tools_define_the_linuxgsm_oxide_default(self):
        expected = Path.home() / "serverfiles" / "oxide" / "plugins"
        self.assertEqual(inventory.DEFAULT_PLUGINS_DIR, expected)
        self.assertEqual(checker.DEFAULT_PLUGINS_DIR, expected)

    def test_inventory_uses_default_without_directory_argument(self):
        with mock.patch.object(
            inventory,
            "scan_plugins",
            return_value=[],
        ) as scan, mock.patch.object(
            sys,
            "argv",
            ["oxide_plugins_inventory.py"],
        ), contextlib.redirect_stdout(io.StringIO()):
            rc = inventory.main()

        self.assertEqual(rc, 0)
        scan.assert_called_once_with(
            inventory.DEFAULT_PLUGINS_DIR,
            recursive=False,
        )

    def test_update_checker_uses_default_without_directory_argument(self):
        with mock.patch.object(
            checker,
            "scan_plugins",
            return_value=[],
        ) as scan, mock.patch.object(
            sys,
            "argv",
            ["umod_plugins_check.py", "--no-log", "--no-state"],
        ), contextlib.redirect_stdout(io.StringIO()):
            rc = checker.main()

        self.assertEqual(rc, 0)
        scan.assert_called_once_with(
            checker.DEFAULT_PLUGINS_DIR,
            recursive=False,
        )

    def test_explicit_directory_still_overrides_the_default(self):
        chosen = Path("/srv/rust/custom-oxide/plugins")
        with mock.patch.object(
            inventory,
            "scan_plugins",
            return_value=[],
        ) as scan, mock.patch.object(
            sys,
            "argv",
            ["oxide_plugins_inventory.py", str(chosen)],
        ), contextlib.redirect_stdout(io.StringIO()):
            rc = inventory.main()

        self.assertEqual(rc, 0)
        scan.assert_called_once_with(chosen, recursive=False)

        with mock.patch.object(
            checker,
            "scan_plugins",
            return_value=[],
        ) as scan, mock.patch.object(
            sys,
            "argv",
            [
                "oxide_plugin_updater.py",
                str(chosen),
                "--no-log",
                "--no-state",
            ],
        ), contextlib.redirect_stdout(io.StringIO()):
            rc = checker.main()

        self.assertEqual(rc, 0)
        scan.assert_called_once_with(chosen, recursive=False)


class PluginUpdaterConfigTests(unittest.TestCase):
    def test_default_config_enables_one_post_update_reload(self):
        config = checker.load_updater_config(checker.CONFIG_FILE_DEFAULT)

        self.assertTrue(
            config["updates"]["reload_plugins_after_updates"]
        )
        self.assertEqual(
            config["updates"]["backup_directory"],
            "data/plugin-backups",
        )
        self.assertEqual(
            config["logging"]["file"],
            "../log/oxide_plugin_updater.log",
        )
        self.assertEqual(
            config["state"]["file"],
            "data/state/plugin_history.json",
        )
        self.assertEqual(
            config["cache"]["file"],
            "data/cache/oxide_plugin_updater_cache.json",
        )
        self.assertEqual(
            config["network"]["minimum_interval_seconds"],
            1.5,
        )
        self.assertEqual(
            config["network"]["maximum_interval_seconds"],
            3.0,
        )

    def test_invalid_network_interval_range_is_rejected(self):
        config = checker.copy.deepcopy(checker.CONFIG_DEFAULTS)
        config["network"]["minimum_interval_seconds"] = 3.0
        config["network"]["maximum_interval_seconds"] = 1.5

        with self.assertRaisesRegex(
            ValueError,
            "maximum_interval_seconds",
        ):
            checker.validate_updater_config(config)

    def test_config_paths_are_resolved_relative_to_config_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "tools" / "custom-updater.json"
            config_path.parent.mkdir()
            config_path.write_text(
                checker.json.dumps(
                    {
                        "plugins_directory": "../server/oxide/plugins",
                        "cache": {"file": "../runtime/cache.json"},
                        "updates": {
                            "backup_directory": "../runtime/backups",
                            "reload_plugins_after_updates": False,
                        },
                        "logging": {"file": "../runtime/updater.log"},
                    }
                ),
                encoding="utf-8",
            )
            config = checker.load_updater_config(config_path)

            self.assertEqual(
                checker._resolve_config_path(
                    config["plugins_directory"],
                    config_path,
                ),
                root / "server" / "oxide" / "plugins",
            )
            self.assertEqual(
                checker._resolve_config_path(
                    config["updates"]["backup_directory"],
                    config_path,
                ),
                root / "runtime" / "backups",
            )
            self.assertFalse(
                config["updates"]["reload_plugins_after_updates"]
            )

    def test_legacy_checker_refuses_update_mode_and_names_new_program(self):
        with mock.patch.object(
            checker,
            "install_update",
        ) as install, contextlib.redirect_stderr(io.StringIO()) as stderr:
            rc = checker.main(
                [
                    "/srv/oxide/plugins",
                    "--update",
                    "--no-log",
                ],
                legacy_check_only=True,
            )

        self.assertEqual(rc, 2)
        install.assert_not_called()
        rendered = stderr.getvalue()
        self.assertIn("umod_plugins_check.py is check-only", rendered)
        self.assertIn("tools/oxide_plugin_updater.py", rendered)

    def test_invalid_boolean_configuration_is_rejected_cleanly(self):
        config = checker.load_updater_config(checker.CONFIG_FILE_DEFAULT)
        config["updates"]["reload_plugins_after_updates"] = "false"

        with self.assertRaisesRegex(
            ValueError,
            "reload_plugins_after_updates must be true or false",
        ):
            checker.validate_updater_config(config)

    def test_set_plugins_directory_preserves_config_and_makes_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "oxide_plugin_updater.json"
            plugins_dir = root / "serverfiles" / "oxide" / "plugins"
            plugins_dir.mkdir(parents=True)
            original = {
                "plugins_directory": "/old/oxide/plugins",
                "network": {"maximum_retries": 3},
                "custom_future_key": {"preserve": True},
            }
            config_path.write_text(
                checker.json.dumps(original, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(config_path, 0o640)

            result = checker.set_plugins_directory_config(
                config_path,
                plugins_dir,
            )

            self.assertTrue(result["changed"])
            self.assertFalse(result["created"])
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o640)
            saved = checker.json.loads(
                config_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                saved["plugins_directory"],
                str(plugins_dir.resolve()),
            )
            self.assertEqual(saved["network"], {"maximum_retries": 3})
            self.assertEqual(
                saved["custom_future_key"],
                {"preserve": True},
            )
            backup = result["backup_path"]
            self.assertIsNotNone(backup)
            self.assertEqual(
                Path(backup).parent,
                root / "data" / "config-backups",
            )
            self.assertEqual(
                checker.json.loads(
                    Path(backup).read_text(encoding="utf-8")
                ),
                original,
            )

    def test_set_plugins_directory_can_create_minimal_custom_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "custom-updater.json"
            plugins_dir = root / "oxide" / "plugins"
            plugins_dir.mkdir(parents=True)

            result = checker.set_plugins_directory_config(
                config_path,
                plugins_dir,
            )

            self.assertTrue(result["created"])
            self.assertIsNone(result["backup_path"])
            self.assertEqual(
                checker.json.loads(
                    config_path.read_text(encoding="utf-8")
                ),
                {"plugins_directory": str(plugins_dir.resolve())},
            )

    def test_set_plugins_directory_rejects_missing_path_without_editing(self):
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "oxide_plugin_updater.json"
            original = {"plugins_directory": "/old/oxide/plugins"}
            config_path.write_text(
                checker.json.dumps(original) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "plugin directory does not exist",
            ):
                checker.set_plugins_directory_config(
                    config_path,
                    Path(td) / "missing",
                )

            self.assertEqual(
                checker.json.loads(
                    config_path.read_text(encoding="utf-8")
                ),
                original,
            )
            self.assertEqual(
                list(config_path.parent.glob("*.bak.*")),
                [],
            )

    def test_set_plugins_directory_cli_saves_and_exits_before_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "custom-updater.json"
            plugins_dir = root / "oxide" / "plugins"
            plugins_dir.mkdir(parents=True)

            with mock.patch.object(
                checker,
                "scan_plugins",
            ) as scan, contextlib.redirect_stdout(io.StringIO()) as stdout:
                rc = checker.main(
                    [
                        "--config",
                        str(config_path),
                        "--set-plugins-directory",
                        str(plugins_dir),
                    ]
                )

            self.assertEqual(rc, 0)
            scan.assert_not_called()
            self.assertIn("SAVED:", stdout.getvalue())
            self.assertEqual(
                checker.json.loads(
                    config_path.read_text(encoding="utf-8")
                )["plugins_directory"],
                str(plugins_dir.resolve()),
            )

    def test_view_config_resolves_paths_and_redacts_rcon_password(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "custom-updater.json"
            config_path.write_text(
                checker.json.dumps(
                    {
                        "plugins_directory": "server/oxide/plugins",
                        "rcon": {"password": "do-not-print-this"},
                    }
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                rc = checker.main(
                    ["--config", str(config_path), "--view-config"]
                )

            self.assertEqual(rc, 0)
            rendered = stdout.getvalue()
            self.assertIn(
                str(root / "server" / "oxide" / "plugins"),
                rendered,
            )
            self.assertIn("<redacted>", rendered)
            self.assertNotIn("do-not-print-this", rendered)

    def test_missing_plugin_directory_prints_both_recovery_commands(self):
        missing = Path("/srv/rust/missing-oxide/plugins")
        with mock.patch.object(
            checker,
            "scan_plugins",
            side_effect=FileNotFoundError,
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            rc = checker.main(
                [
                    str(missing),
                    "--no-log",
                    "--no-state",
                ]
            )

        self.assertEqual(rc, 2)
        rendered = stderr.getvalue()
        self.assertIn(
            f"ERROR: Oxide plugin directory not found: {missing}",
            rendered,
        )
        self.assertIn("--set-plugins-directory", rendered)
        self.assertIn(
            "python3 tools/oxide_plugin_updater.py "
            "/path/to/oxide/plugins",
            rendered,
        )


class PluginPackageStateTests(unittest.TestCase):
    def plugin(self, *, version="1.0.0", sha256="a" * 64):
        return {
            "file": "/srv/oxide/plugins/ExamplePlugin.cs",
            "filename": "ExamplePlugin.cs",
            "name": "Example Plugin",
            "author": "Example Author",
            "version": version,
            "mtime": "2026-07-27T20:00:00+03:00",
            "size_bytes": 500,
            "sha256": sha256,
        }

    def row(self, *, remote="1.1.0", status="OUTDATED"):
        return {
            "filename": "ExamplePlugin.cs",
            "source": "umod",
            "local": "1.0.0",
            "remote": remote,
            "status": status,
            "remote_url": "https://umod.org/plugins/example-plugin",
        }

    def test_known_outdated_tuple_is_retained_without_duplicate_events(self):
        state = checker._empty_plugin_state()
        first = checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(),
            row=self.row(),
            history_limit=50,
            observed_at="2026-07-27T20:00:00+03:00",
        )
        second = checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(),
            row=self.row(),
            history_limit=50,
            observed_at="2026-07-27T21:00:00+03:00",
        )

        entry = state["plugins"]["ExamplePlugin.cs"]
        self.assertEqual(first, "new")
        self.assertEqual(second, "known")
        self.assertEqual(len(entry["history"]), 1)
        self.assertEqual(entry["active_outdated"]["checks_seen"], 2)
        self.assertEqual(
            entry["active_outdated"]["first_seen_at"],
            "2026-07-27T20:00:00+03:00",
        )

    def test_changed_remote_and_resolution_are_historical_events(self):
        state = checker._empty_plugin_state()
        checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(),
            row=self.row(),
            history_limit=50,
            observed_at="2026-07-27T20:00:00+03:00",
        )
        changed = checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(),
            row=self.row(remote="1.2.0"),
            history_limit=50,
            observed_at="2026-07-28T20:00:00+03:00",
        )
        resolved_row = self.row(
            remote="1.2.0",
            status=checker.CHECK_STATUS_CURRENT,
        )
        resolved_row["local"] = "1.2.0"
        checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(version="1.2.0", sha256="b" * 64),
            row=resolved_row,
            history_limit=50,
            observed_at="2026-07-28T21:00:00+03:00",
        )

        entry = state["plugins"]["ExamplePlugin.cs"]
        self.assertEqual(changed, "changed")
        self.assertEqual(
            [event["event"] for event in entry["history"]],
            [
                "outdated_detected",
                "outdated_changed",
                "outdated_resolved",
            ],
        )
        self.assertIsNone(entry["active_outdated"])

    def test_legacy_ok_status_still_resolves_an_outdated_state(self):
        state = checker._empty_plugin_state()
        checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(),
            row=self.row(),
            history_limit=50,
        )

        resolved_row = self.row(remote="1.1.0", status="OK")
        resolved_row["local"] = "1.1.0"
        checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(version="1.1.0", sha256="b" * 64),
            row=resolved_row,
            history_limit=50,
        )

        self.assertIsNone(
            state["plugins"]["ExamplePlugin.cs"]["active_outdated"]
        )

    def test_state_round_trip_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tools" / "data" / "state" / "history.json"
            state = checker._empty_plugin_state()
            checker.observe_plugin_state(
                state,
                key="ExamplePlugin.cs",
                plugin=self.plugin(),
                row=self.row(),
                history_limit=50,
            )

            checker.save_plugin_state(path, state)
            loaded = checker.load_plugin_state(path)

        self.assertEqual(loaded["schema_version"], 1)
        self.assertIn("ExamplePlugin.cs", loaded["plugins"])
        self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_network_error_does_not_erase_previously_known_outdated(self):
        state = checker._empty_plugin_state()
        checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(),
            row=self.row(),
            history_limit=50,
        )
        error_row = self.row(status="ERROR: HTTP 429")
        error_row["remote"] = "-"

        classification = checker.observe_plugin_state(
            state,
            key="ExamplePlugin.cs",
            plugin=self.plugin(),
            row=error_row,
            history_limit=50,
        )

        self.assertEqual(classification, "")
        self.assertIsInstance(
            state["plugins"]["ExamplePlugin.cs"]["active_outdated"],
            dict,
        )


class PluginCacheTests(unittest.TestCase):
    def test_non_object_cache_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text("[]", encoding="utf-8")

            self.assertEqual(checker.load_cache(path), {})

    def test_override_cache_fetches_fresh_result_and_replaces_valid_entry(self):
        local = {
            "file": "/srv/oxide/plugins/ExamplePlugin.cs",
            "filename": "ExamplePlugin.cs",
            "name": "Example Plugin",
            "author": "Example Author",
            "version": "1.0.0",
            "size_bytes": 500,
        }
        fresh = checker.HttpResult(
            data={
                "latest_release_version": "1.2.0",
                "url": "https://umod.org/plugins/example-plugin",
            },
            headers={},
        )

        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            checker.save_cache(
                cache_path,
                {
                    "umod:json:ExamplePlugin": {
                        "ts": int(checker.time.time()),
                        "data": {
                            "latest_release_version": "1.1.0",
                            "url": "https://umod.org/plugins/example-plugin",
                        },
                    }
                },
            )
            with mock.patch.object(
                checker,
                "scan_plugins",
                return_value=[local],
            ), mock.patch.object(
                checker,
                "http_get_json",
                return_value=fresh,
            ) as fetch, contextlib.redirect_stdout(
                io.StringIO()
            ) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                rc = checker.main(
                    [
                        "/srv/oxide/plugins",
                        "--no-check-chaos",
                        "--cache",
                        str(cache_path),
                        "--override-cache",
                        "--no-log",
                        "--no-state",
                        "--color",
                        "never",
                    ]
                )

            self.assertEqual(rc, 1)
            fetch.assert_called_once()
            self.assertIn("1.2.0", stdout.getvalue())
            self.assertIn("Cache override enabled", stderr.getvalue())
            self.assertEqual(
                checker.load_cache(cache_path)[
                    "umod:json:ExamplePlugin"
                ]["data"]["latest_release_version"],
                "1.2.0",
            )


class PluginNetworkBackoffTests(unittest.TestCase):
    def test_randomized_pacing_uses_one_shared_request_clock(self):
        checker._LAST_NETWORK_REQUEST_STARTED = 0.0
        with mock.patch.object(
            checker.random,
            "uniform",
            side_effect=[1.5, 2.0],
        ) as uniform, mock.patch.object(
            checker.time,
            "monotonic",
            side_effect=[100.0, 100.0, 100.5, 102.0],
        ), mock.patch.object(checker.time, "sleep") as sleep:
            first_wait = checker.pace_network_request(1.5, 3.0)
            second_wait = checker.pace_network_request(1.5, 3.0)

        self.assertEqual(first_wait, 0.0)
        self.assertEqual(second_wait, 1.5)
        uniform.assert_has_calls(
            [mock.call(1.5, 3.0), mock.call(1.5, 3.0)]
        )
        sleep.assert_called_once_with(1.5)

    def test_retry_header_and_exponential_fallback_are_bounded(self):
        self.assertEqual(
            checker.rate_limit_delay(
                {"Retry-After": "45"},
                attempt=1,
                fallback_backoff_s=30,
                max_backoff_s=300,
            ),
            45,
        )
        self.assertEqual(
            checker.rate_limit_delay(
                {},
                attempt=3,
                fallback_backoff_s=30,
                max_backoff_s=300,
            ),
            120,
        )
        with self.assertRaisesRegex(RuntimeError, "aborting"):
            checker.rate_limit_delay(
                {"Retry-After": "900"},
                attempt=1,
                fallback_backoff_s=30,
                max_backoff_s=300,
            )

    def test_wait_notices_persist_when_animation_is_unavailable(self):
        stderr = io.StringIO()
        with mock.patch.object(
            checker.time,
            "sleep",
        ) as sleep, contextlib.redirect_stderr(stderr):
            checker.wait_with_activity(2, "HTTP 429 rate limited")

        sleep.assert_called_once_with(2.0)
        rendered = stderr.getvalue()
        self.assertRegex(
            rendered,
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"\[WAIT\] HTTP 429 rate limited; backing off for 2 seconds",
        )
        self.assertIn(
            "[CONTINUE] Cooldown complete; continuing plugin checks",
            rendered,
        )

    def test_spinner_is_the_unbracketed_classic_sequence(self):
        self.assertEqual(checker.ActivitySpinner.FRAMES, ("\\", "|", "/", "-"))

    def test_spinner_renders_target_immediately_on_entry(self):
        stderr = io.StringIO()
        stderr.isatty = lambda: True
        with mock.patch.object(
            checker.sys,
            "stderr",
            stderr,
        ), mock.patch.object(
            checker.threading,
            "Thread",
        ) as thread:
            with checker.ActivitySpinner(
                "[ 1/52] Verifying Admin No Loot"
            ):
                pass

        self.assertTrue(
            stderr.getvalue().startswith(
                "\r\\ [ 1/52] Verifying Admin No Loot"
            )
        )
        thread.return_value.start.assert_called_once_with()

    def test_persistent_rate_limit_opens_circuit_for_remaining_plugins(self):
        locals_ = [
            {
                "file": f"/srv/oxide/plugins/Example{n}.cs",
                "filename": f"Example{n}.cs",
                "name": f"Example {n}",
                "author": "Example Author",
                "version": "1.0.0",
                "size_bytes": 500,
                "sha256": str(n) * 64,
            }
            for n in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=locals_,
        ), mock.patch.object(
            checker,
            "http_get_json",
            side_effect=RuntimeError(
                "HTTP 429 rate-limited after retries"
            ),
        ) as get_json, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            rc = checker.main(
                [
                    "/srv/oxide/plugins",
                    "--no-check-chaos",
                    "--cache",
                    str(Path(td) / "cache.json"),
                    "--state-file",
                    str(Path(td) / "state.json"),
                    "--color",
                    "never",
                    "--no-log",
                ]
            )

        self.assertEqual(rc, 2)
        get_json.assert_called_once()
        self.assertIn(
            "skipping further uncached remote requests",
            stderr.getvalue(),
        )


class PluginUpdateValidationTests(unittest.TestCase):
    def candidate(self, path=Path("/srv/oxide/plugins/ExamplePlugin.cs")):
        return checker.UpdateCandidate(
            filename="ExamplePlugin.cs",
            path=path,
            name="Example Plugin",
            local_version="1.0.0",
            remote_version="1.1.0",
            download_url="https://umod.org/plugins/ExamplePlugin.cs",
        )

    def test_matching_newer_csharp_source_passes_validation(self):
        installed = plugin_source(version="1.0.0").encode()
        downloaded = plugin_source(version="1.1.0").encode()

        result = checker.validate_plugin_download(
            self.candidate(),
            installed,
            downloaded,
            headers={"Content-Type": "text/x-csharp"},
            final_url="https://umod.org/plugins/ExamplePlugin.cs",
            allow_large_shrink=False,
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(result.candidate_version, "1.1.0")
        self.assertNotEqual(result.old_sha256, result.new_sha256)

    def test_force_accepts_identical_same_version_source(self):
        source = plugin_source(version="1.0.0").encode()
        candidate = self.candidate()
        candidate.remote_version = "1.0.0"

        normal = checker.validate_plugin_download(
            candidate,
            source,
            source,
            headers={"Content-Type": "text/x-csharp"},
            final_url="https://umod.org/plugins/ExamplePlugin.cs",
            allow_large_shrink=False,
        )
        forced = checker.validate_plugin_download(
            candidate,
            source,
            source,
            headers={"Content-Type": "text/x-csharp"},
            final_url="https://umod.org/plugins/ExamplePlugin.cs",
            allow_large_shrink=False,
            force_reinstall=True,
        )

        self.assertTrue(normal.errors)
        self.assertIn(
            "is not definitely newer than installed version",
            normal.errors[0],
        )
        self.assertEqual(forced.errors, [])
        self.assertEqual(forced.old_sha256, forced.new_sha256)

    def test_force_identical_source_records_revalidation_without_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugins_dir = root / "oxide" / "plugins"
            plugins_dir.mkdir(parents=True)
            target = plugins_dir / "ExamplePlugin.cs"
            source = plugin_source(version="1.0.0").encode()
            target.write_bytes(source)
            candidate = self.candidate(target)
            candidate.remote_version = "1.0.0"
            package_state = checker._empty_plugin_state()
            response = checker.HttpBytesResult(
                data=source,
                headers={"Content-Type": "text/x-csharp"},
                final_url="https://umod.org/plugins/ExamplePlugin.cs",
            )

            with mock.patch.object(
                checker,
                "http_get_bytes",
                return_value=response,
            ), contextlib.redirect_stdout(io.StringIO()):
                result = checker.install_update(
                    candidate,
                    plugins_dir=plugins_dir,
                    backup_root=root / "backups",
                    timeout_s=1,
                    min_interval_s=0,
                    max_retries=0,
                    debug_headers=False,
                    allow_large_shrink=False,
                    use_color=False,
                    package_state=package_state,
                    force_reinstall=True,
                )

            self.assertTrue(result)
            self.assertFalse(result.source_changed)
            self.assertEqual(target.read_bytes(), source)
            self.assertFalse((root / "backups").exists())
            history = package_state["plugins"]["ExamplePlugin.cs"]["history"]
            self.assertEqual(history[-1]["event"], "source_revalidated")
            self.assertEqual(
                history[-1]["sha256"],
                checker.hashlib.sha256(source).hexdigest(),
            )

    def test_html_response_is_refused(self):
        result = checker.validate_plugin_download(
            self.candidate(),
            plugin_source(version="1.0.0").encode(),
            (b"<html><body>429 Too Many Requests</body></html>" * 10),
            headers={"Content-Type": "text/html; charset=UTF-8"},
            final_url="https://umod.org/plugins/ExamplePlugin.cs",
            allow_large_shrink=False,
        )

        self.assertTrue(
            any("HTML" in error or "Content-Type" in error for error in result.errors)
        )

    def test_suspiciously_smaller_source_requires_explicit_override(self):
        installed = plugin_source(version="1.0.0", padding=4000).encode()
        downloaded = plugin_source(version="1.1.0", padding=200).encode()

        refused = checker.validate_plugin_download(
            self.candidate(),
            installed,
            downloaded,
            headers={"Content-Type": "text/x-csharp"},
            final_url="https://umod.org/plugins/ExamplePlugin.cs",
            allow_large_shrink=False,
        )
        allowed = checker.validate_plugin_download(
            self.candidate(),
            installed,
            downloaded,
            headers={"Content-Type": "text/x-csharp"},
            final_url="https://umod.org/plugins/ExamplePlugin.cs",
            allow_large_shrink=True,
        )

        self.assertTrue(any("--allow-large-shrink" in error for error in refused.errors))
        self.assertEqual(allowed.errors, [])
        self.assertTrue(allowed.warnings)

    def test_backup_tree_must_not_be_inside_live_plugin_tree(self):
        plugins = Path("/srv/rust/serverfiles/oxide/plugins")
        self.assertTrue(
            checker.path_is_within(plugins / "disabled" / "backups", plugins)
        )
        self.assertFalse(
            checker.path_is_within(
                Path("/srv/watchdog/data/plugin-backups"),
                plugins,
            )
        )

    def test_install_is_atomic_and_archives_old_version_by_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugins_dir = root / "oxide" / "plugins"
            plugins_dir.mkdir(parents=True)
            target = plugins_dir / "ExamplePlugin.cs"
            old_source = plugin_source(version="1.0.0").encode()
            new_source = plugin_source(version="1.1.0").encode()
            target.write_bytes(old_source)
            os.chmod(target, 0o640)

            response = checker.HttpBytesResult(
                data=new_source,
                headers={"Content-Type": "text/x-csharp"},
                final_url="https://umod.org/plugins/ExamplePlugin.cs",
            )
            package_state = checker._empty_plugin_state()
            audit_path = root / "log" / "updates.log"
            output = io.StringIO()
            with mock.patch.object(
                checker,
                "http_get_bytes",
                return_value=response,
            ), contextlib.redirect_stdout(output):
                ok = checker.install_update(
                    self.candidate(target),
                    plugins_dir=plugins_dir,
                    backup_root=root / "backups",
                    timeout_s=1,
                    min_interval_s=0,
                    max_retries=0,
                    debug_headers=False,
                    allow_large_shrink=False,
                    use_color=False,
                    audit=checker.AuditLogger(audit_path),
                    package_state=package_state,
                )

            self.assertTrue(ok)
            self.assertEqual(target.read_bytes(), new_source)
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            backups = list(
                (root / "backups" / "ExamplePlugin" / "1.0.0").glob(
                    "ExamplePlugin-*.cs"
                )
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), old_source)
            self.assertIn("Updated: YES", output.getvalue())
            audit_record = checker.json.loads(
                audit_path.read_text(encoding="utf-8")
            )
            self.assertEqual(audit_record["plugin"], "ExamplePlugin.cs")
            self.assertEqual(audit_record["source"], "umod")
            self.assertEqual(audit_record["local_version"], "1.0.0")
            self.assertEqual(audit_record["remote_version"], "1.1.0")
            self.assertEqual(audit_record["old_size"], len(old_source))
            self.assertEqual(audit_record["new_size"], len(new_source))
            self.assertEqual(
                audit_record["old_sha256"],
                checker.hashlib.sha256(old_source).hexdigest(),
            )
            self.assertEqual(
                audit_record["new_sha256"],
                checker.hashlib.sha256(new_source).hexdigest(),
            )
            self.assertEqual(
                audit_record["download_url"],
                "https://umod.org/plugins/ExamplePlugin.cs",
            )
            self.assertEqual(audit_record["backup"], str(backups[0]))
            history = package_state["plugins"]["ExamplePlugin.cs"]["history"]
            self.assertEqual(history[-1]["event"], "update_installed")
            self.assertEqual(history[-1]["from_version"], "1.0.0")
            self.assertEqual(history[-1]["to_version"], "1.1.0")
            self.assertEqual(
                history[-1]["new_sha256"],
                checker.hashlib.sha256(new_source).hexdigest(),
            )

    def test_update_refuses_file_changed_during_download(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugins_dir = root / "oxide" / "plugins"
            plugins_dir.mkdir(parents=True)
            target = plugins_dir / "ExamplePlugin.cs"
            target.write_text(plugin_source(version="1.0.0"), encoding="utf-8")
            response = checker.HttpBytesResult(
                data=plugin_source(version="1.1.0").encode(),
                headers={"Content-Type": "text/x-csharp"},
                final_url="https://umod.org/plugins/ExamplePlugin.cs",
            )

            def change_installed_file(*_args, **_kwargs):
                target.write_text(
                    plugin_source(version="1.0.0", padding=350),
                    encoding="utf-8",
                )
                return response

            with mock.patch.object(
                checker,
                "http_get_bytes",
                side_effect=change_installed_file,
            ), contextlib.redirect_stdout(io.StringIO()) as output:
                ok = checker.install_update(
                    self.candidate(target),
                    plugins_dir=plugins_dir,
                    backup_root=root / "backups",
                    timeout_s=1,
                    min_interval_s=0,
                    max_retries=0,
                    debug_headers=False,
                    allow_large_shrink=False,
                    use_color=False,
                )

            self.assertFalse(ok)
            self.assertIn("changed while its update was downloading", output.getvalue())
            self.assertEqual(
                inventory.extract_plugin_info(target.read_text(encoding="utf-8"))[2],
                "1.0.0",
            )
            self.assertFalse((root / "backups").exists())

    def test_update_refuses_file_changed_after_inventory_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugins_dir = root / "oxide" / "plugins"
            plugins_dir.mkdir(parents=True)
            target = plugins_dir / "ExamplePlugin.cs"
            scanned_source = plugin_source(
                version="1.0.0",
                padding=250,
            ).encode()
            changed_source = plugin_source(
                version="1.0.0",
                padding=350,
            ).encode()
            target.write_bytes(changed_source)
            candidate = self.candidate(target)
            candidate.local_sha256 = checker.hashlib.sha256(
                scanned_source
            ).hexdigest()
            output = io.StringIO()

            with mock.patch.object(
                checker,
                "http_get_bytes",
            ) as download, contextlib.redirect_stdout(output):
                ok = checker.install_update(
                    candidate,
                    plugins_dir=plugins_dir,
                    backup_root=root / "backups",
                    timeout_s=1,
                    min_interval_s=0,
                    max_retries=0,
                    debug_headers=False,
                    allow_large_shrink=False,
                    use_color=False,
                )

            self.assertFalse(ok)
            download.assert_not_called()
            self.assertIn(
                "changed after the inventory scan",
                output.getvalue(),
            )
            self.assertEqual(target.read_bytes(), changed_source)


class PluginUpdateOutputTests(unittest.TestCase):
    def test_plugin_activation_completion_is_exact_and_terminal(self):
        pending = (
            "Plugin compilation is already queued: HeliRide\n"
            "UberTool was compiled successfully in 500ms\n"
            "Unloaded plugin Heli Ride v1.1.17 by ColonBlow\n"
            "[Heli Ride] Config Loaded\n"
            "HeliRide was compiled successfully in 625ms"
        )
        succeeded = (
            pending
            + "\nLoaded plugin Heli Ride v1.1.17 by ColonBlow"
        )
        failed = (
            pending
            + "\nError while compiling HeliRide: missing ClientRPCPlayer"
        )

        self.assertFalse(
            checker._plugin_activation_completed(
                pending,
                "HeliRide",
                "Heli Ride",
            )
        )
        self.assertTrue(
            checker._plugin_activation_completed(
                succeeded,
                "HeliRide",
                "Heli Ride",
            )
        )
        self.assertTrue(
            checker._plugin_activation_completed(
                failed,
                "HeliRide",
                "Heli Ride",
            )
        )
        self.assertEqual(
            checker._plugin_loaded_success(
                succeeded,
                "HeliRide",
                "Heli Ride",
            ),
            "Loaded plugin Heli Ride v1.1.17 by ColonBlow",
        )
        self.assertEqual(
            checker._reload_compile_failure(failed, "HeliRide"),
            "Error while compiling HeliRide: missing ClientRPCPlayer",
        )

    def test_force_requires_a_single_plugin_target(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                checker.main(["--no-config", "--force"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--force requires --update-plugin", stderr.getvalue())

    def test_verify_modes_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                checker.main(
                    [
                        "--no-config",
                        "--verify-plugin",
                        "HeliRide",
                        "--verify-all-plugins",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("are mutually exclusive", stderr.getvalue())

    def test_table_header_is_bracketed_by_terminal_width_rules(self):
        rows = [
            {
                "filename": "ExamplePlugin.cs",
                "source": "umod",
                "local": "1.0.0",
                "remote": "1.1.0",
                "status": "OUTDATED",
                "remote_url": "https://umod.org/plugins/example-plugin",
            }
        ]
        output = io.StringIO()
        with mock.patch.object(
            checker.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((47, 24)),
        ), contextlib.redirect_stdout(output):
            checker.print_table(rows, use_color=False)

        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "-" * 47)
        self.assertTrue(lines[1].startswith("filename"))
        self.assertIn("remote url", lines[1])
        self.assertEqual(lines[2], "-" * 47)

    def test_default_check_prints_update_offer_without_installing(self):
        local = {
            "file": "/srv/oxide/plugins/ExamplePlugin.cs",
            "filename": "ExamplePlugin.cs",
            "name": "Example Plugin",
            "author": "Example Author",
            "version": "1.0.0",
            "size_bytes": 500,
        }
        remote = checker.HttpResult(
            data={
                "latest_release_version": "1.1.0",
                "url": "https://umod.org/plugins/example-plugin",
                "download_url": "https://umod.org/plugins/ExamplePlugin.cs",
            },
            headers={},
        )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=[local],
        ), mock.patch.object(
            checker,
            "http_get_json",
            return_value=remote,
        ), mock.patch.object(
            checker,
            "install_update",
        ) as install, mock.patch.object(
            sys,
            "argv",
            [
                "umod_plugins_check.py",
                "/srv/oxide/plugins",
                "--no-check-chaos",
                "--cache",
                str(Path(td) / "cache.json"),
                "--color",
                "never",
                "--no-log",
                "--no-state",
            ],
        ), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ):
            rc = checker.main()

        self.assertEqual(rc, 1)
        install.assert_not_called()
        rendered = stdout.getvalue()
        self.assertIn("Plugins that can be auto-updated (1):", rendered)
        self.assertIn("ExamplePlugin.cs: 1.0.0 -> 1.1.0", rendered)
        self.assertIn("--update", rendered)

    def test_unexpected_metadata_shape_is_reported_without_traceback(self):
        local = {
            "file": "/srv/oxide/plugins/ExamplePlugin.cs",
            "filename": "ExamplePlugin.cs",
            "name": "Example Plugin",
            "author": "Example Author",
            "version": "1.0.0",
            "size_bytes": 500,
        }
        remote = checker.HttpResult(data=[], headers={})

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=[local],
        ), mock.patch.object(
            checker,
            "http_get_json",
            return_value=remote,
        ), contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(io.StringIO()):
            rc = checker.main(
                [
                    "/srv/oxide/plugins",
                    "--no-check-chaos",
                    "--cache",
                    str(Path(td) / "cache.json"),
                    "--no-log",
                    "--no-state",
                    "--color",
                    "never",
                ]
            )

        self.assertEqual(rc, 2)
        self.assertIn(
            "ERROR: unexpected uMod metadata shape",
            stdout.getvalue(),
        )

    def test_update_run_reports_success_failure_and_manual_counts(self):
        locals_ = [
            {
                "file": f"/srv/oxide/plugins/Example{n}.cs",
                "filename": f"Example{n}.cs",
                "name": f"Example {n}",
                "author": "Example Author",
                "version": version,
                "size_bytes": 500,
            }
            for n, version in ((1, "1.0.0"), (2, "1.0.0"), (3, "custom"))
        ]

        def remote_for(url, **_kwargs):
            stem = Path(urlparse(url).path).stem
            return checker.HttpResult(
                data={
                    "latest_release_version": "1.1.0",
                    "url": f"https://umod.org/plugins/{stem.lower()}",
                    "download_url": f"https://umod.org/plugins/{stem}.cs",
                },
                headers={},
            )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=locals_,
        ), mock.patch.object(
            checker,
            "http_get_json",
            side_effect=remote_for,
        ), mock.patch.object(
            checker,
            "install_update",
            side_effect=[True, False],
        ), mock.patch.object(
            checker,
            "reload_updated_plugins",
            return_value=(True, "reload complete"),
        ) as reload_plugins, mock.patch.object(
            sys,
            "argv",
            [
                "oxide_plugin_updater.py",
                "/srv/oxide/plugins",
                "--no-check-chaos",
                "--cache",
                str(Path(td) / "cache.json"),
                "--color",
                "never",
                "--update",
                "--no-log",
                "--no-state",
            ],
        ), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ):
            rc = checker.main()

        self.assertEqual(rc, 2)
        rendered = stdout.getvalue()
        self.assertIn("Source updates:  1", rendered)
        self.assertIn("Failed/refused:  1", rendered)
        self.assertIn("Manual-only:   1", rendered)
        self.assertIn("Plugin activation: OK", rendered)
        self.assertIn(
            "3 plugins found in directory: /srv/oxide/plugins",
            rendered,
        )
        self.assertIn("1 plugin source updated.", rendered)
        self.assertIn("2 plugins remain outdated.", rendered)
        self.assertIn("Plugins that still need to be updated (2):", rendered)
        self.assertIn("Example2.cs: 1.0.0 -> 1.1.0 [umod; failed/refused]", rendered)
        self.assertIn("Example3.cs: custom -> 1.1.0 [umod; manual]", rendered)
        self.assertIn(
            "[url: https://umod.org/plugins/example2 ]",
            rendered,
        )
        reload_plugins.assert_called_once()

    def test_reload_can_be_deferred_by_cli(self):
        local = {
            "file": "/srv/oxide/plugins/ExamplePlugin.cs",
            "filename": "ExamplePlugin.cs",
            "name": "Example Plugin",
            "author": "Example Author",
            "version": "1.0.0",
            "size_bytes": 500,
        }
        remote = checker.HttpResult(
            data={
                "latest_release_version": "1.1.0",
                "url": "https://umod.org/plugins/example-plugin",
                "download_url": "https://umod.org/plugins/ExamplePlugin.cs",
            },
            headers={},
        )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=[local],
        ), mock.patch.object(
            checker,
            "http_get_json",
            return_value=remote,
        ), mock.patch.object(
            checker,
            "install_update",
            return_value=True,
        ), mock.patch.object(
            checker,
            "reload_updated_plugins",
        ) as reload_plugins, mock.patch.object(
            sys,
            "argv",
            [
                "oxide_plugin_updater.py",
                "/srv/oxide/plugins",
                "--no-check-chaos",
                "--cache",
                str(Path(td) / "cache.json"),
                "--color",
                "never",
                "--update",
                "--no-reload-plugins-after-updates",
                "--no-log",
                "--no-state",
            ],
        ), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ):
            rc = checker.main()

        self.assertEqual(rc, 0)
        reload_plugins.assert_not_called()
        self.assertIn("Plugin activation: deferred", stdout.getvalue())

    def test_update_plugin_force_targets_one_same_version_plugin(self):
        locals_ = [
            {
                "file": "/srv/oxide/plugins/HeliRide.cs",
                "filename": "HeliRide.cs",
                "name": "Heli Ride",
                "author": "Example Author",
                "version": "1.0.0",
                "size_bytes": 500,
                "sha256": "a" * 64,
            },
            {
                "file": "/srv/oxide/plugins/UberTool.cs",
                "filename": "UberTool.cs",
                "name": "Uber Tool",
                "author": "Example Author",
                "version": "1.0.0",
                "size_bytes": 500,
                "sha256": "b" * 64,
            },
        ]
        remote = checker.HttpResult(
            data={
                "latest_release_version": "1.0.0",
                "url": "https://umod.org/plugins/heli-ride",
                "download_url": "https://umod.org/plugins/HeliRide.cs",
            },
            headers={},
        )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=locals_,
        ), mock.patch.object(
            checker,
            "http_get_json",
            return_value=remote,
        ) as get_json, mock.patch.object(
            checker,
            "load_chaos_manifest",
        ) as load_chaos, mock.patch.object(
            checker,
            "install_update",
            return_value=checker.InstallResult(
                True,
                source_changed=False,
            ),
        ) as install, mock.patch.object(
            checker,
            "reload_updated_plugins",
            return_value=(True, "activation complete"),
        ) as activate, mock.patch.object(
            sys,
            "argv",
            [
                "oxide_plugin_updater.py",
                "/srv/oxide/plugins",
                "--cache",
                str(Path(td) / "cache.json"),
                "--color",
                "never",
                "--progress",
                "--update-plugin",
                "heliride.cs",
                "--force",
                "--no-log",
                "--no-state",
            ],
        ), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            rc = checker.main()

        self.assertEqual(rc, 0)
        get_json.assert_called_once()
        load_chaos.assert_not_called()
        install.assert_called_once()
        candidate = install.call_args.args[0]
        self.assertEqual(candidate.filename, "HeliRide.cs")
        self.assertTrue(install.call_args.kwargs["force_reinstall"])
        activate.assert_called_once()
        self.assertEqual(
            activate.call_args.args[1],
            [("HeliRide.cs", "1.0.0")],
        )
        self.assertIsNotNone(activate.call_args.kwargs["progress"])
        self.assertEqual(
            activate.call_args.kwargs["activation_records"],
            [],
        )
        rendered = stdout.getvalue()
        self.assertIn("UP TO DATE", rendered)
        self.assertIn("-- UP TO DATE (net)", stderr.getvalue())
        self.assertIn("1 plugin found in directory", rendered)
        self.assertIn("0 plugin sources updated.", rendered)
        self.assertIn(
            "1 plugin source was already identical and revalidated.",
            rendered,
        )
        self.assertNotIn("Backups:", rendered)

    def test_verify_plugin_targets_one_without_contacting_upstream(self):
        locals_ = [
            {
                "file": "/srv/oxide/plugins/HeliRide.cs",
                "filename": "HeliRide.cs",
                "name": "Heli Ride",
                "version": "1.0.0",
            },
            {
                "file": "/srv/oxide/plugins/UberTool.cs",
                "filename": "UberTool.cs",
                "name": "Uber Tool",
                "version": "2.0.0",
            },
        ]

        def activate_one(
            _rcon_config,
            _plugins,
            *,
            progress,
            activation_records,
        ):
            record = {
                "plugin": "HeliRide.cs",
                "command": "oxide.load HeliRide",
                "status": "OK",
                "response": "HeliRide was compiled successfully",
            }
            activation_records.append(record)
            progress(
                1,
                1,
                record["plugin"],
                record["status"],
                record["response"],
            )
            return True, "1 plugin activated and verified"

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=locals_,
        ), mock.patch.object(
            checker,
            "http_get_json",
        ) as get_json, mock.patch.object(
            checker,
            "load_chaos_manifest",
        ) as load_chaos, mock.patch.object(
            checker,
            "install_update",
        ) as install, mock.patch.object(
            checker,
            "reload_updated_plugins",
            side_effect=activate_one,
        ) as activate, contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(io.StringIO()):
            rc = checker.main(
                [
                    "/srv/oxide/plugins",
                    "--no-config",
                    "--cache",
                    str(Path(td) / "cache.json"),
                    "--color",
                    "never",
                    "--verify-plugin",
                    "heliride.cs",
                    "--no-log",
                    "--no-state",
                ]
            )

        self.assertEqual(rc, 0)
        get_json.assert_not_called()
        load_chaos.assert_not_called()
        install.assert_not_called()
        activate.assert_called_once()
        self.assertEqual(
            activate.call_args.args[1],
            [("HeliRide.cs", "1.0.0")],
        )
        self.assertEqual(
            activate.call_args.kwargs["activation_records"],
            [
                {
                    "plugin": "HeliRide.cs",
                    "command": "oxide.load HeliRide",
                    "status": "OK",
                    "response": "HeliRide was compiled successfully",
                }
            ],
        )
        rendered = stdout.getvalue()
        self.assertIn("Verifying 1 plugin", rendered)
        self.assertIn(
            "1 out of 1 plugin compiled/loaded successfully.",
            rendered,
        )
        self.assertIn("0 plugins failed to compile.", rendered)

    def test_verify_all_plugins_is_sequential_and_surfaces_failure(self):
        locals_ = [
            {
                "file": "/srv/oxide/plugins/HeliRide.cs",
                "filename": "HeliRide.cs",
                "name": "Heli Ride",
                "version": "1.0.0",
            },
            {
                "file": "/srv/oxide/plugins/UberTool.cs",
                "filename": "UberTool.cs",
                "name": "Uber Tool",
                "version": "2.0.0",
            },
        ]

        def activate_all(
            _rcon_config,
            _plugins,
            *,
            progress,
            activation_records,
        ):
            records = [
                {
                    "plugin": "HeliRide.cs",
                    "command": "oxide.reload HeliRide",
                    "status": "OK",
                    "response": (
                        "Loaded plugin Heli Ride v1.0.0 by ColonBlow"
                    ),
                },
                {
                    "plugin": "UberTool.cs",
                    "command": "oxide.load UberTool",
                    "status": "FAILED TO COMPILE",
                    "response": (
                        "UberTool - Failed to compile: bad overload | "
                        "Line: 855, Pos: 29"
                    ),
                },
            ]
            activation_records.extend(records)
            for index, record in enumerate(records, start=1):
                progress(
                    index,
                    len(records),
                    record["plugin"],
                    record["status"],
                    record["response"],
                )
            return (
                False,
                "UberTool.cs: UberTool - Failed to compile: bad overload",
            )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=locals_,
        ), mock.patch.object(
            checker,
            "http_get_json",
        ) as get_json, mock.patch.object(
            checker,
            "reload_updated_plugins",
            side_effect=activate_all,
        ) as activate, mock.patch.object(
            checker.shutil,
            "get_terminal_size",
            return_value=os.terminal_size((53, 24)),
        ), contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(io.StringIO()):
            rc = checker.main(
                [
                    "/srv/oxide/plugins",
                    "--no-config",
                    "--cache",
                    str(Path(td) / "cache.json"),
                    "--color",
                    "never",
                    "--verify-all",
                    "--no-log",
                    "--no-state",
                ]
            )

        self.assertEqual(rc, 2)
        get_json.assert_not_called()
        activate.assert_called_once()
        self.assertEqual(
            activate.call_args.args[1],
            [
                ("HeliRide.cs", "1.0.0"),
                ("UberTool.cs", "2.0.0"),
            ],
        )
        rendered = stdout.getvalue()
        self.assertIn("Verifying 2 plugins", rendered)
        self.assertIn("-" * 53, rendered)
        self.assertIn(
            "1 out of 2 plugins compiled/loaded successfully.",
            rendered,
        )
        self.assertIn(
            "[1/2] Loaded plugin Heli Ride v1.0.0 by ColonBlow "
            "(oxide.reload HeliRide)",
            rendered,
        )
        self.assertIn("1 plugin failed to compile.", rendered)
        self.assertIn("UberTool.cs -- FAILED TO COMPILE", rendered)
        self.assertIn("Command: oxide.load UberTool", rendered)
        self.assertIn(
            "Error: UberTool - Failed to compile: bad overload",
            rendered,
        )

    def test_update_plugin_refuses_an_ambiguous_recursive_name(self):
        locals_ = [
            {
                "file": f"/srv/oxide/plugins/{folder}/Kits.cs",
                "filename": "Kits.cs",
                "name": "Kits",
                "version": "1.0.0",
            }
            for folder in ("one", "two")
        ]
        with mock.patch.object(
            checker,
            "scan_plugins",
            return_value=locals_,
        ), mock.patch.object(
            sys,
            "argv",
            [
                "oxide_plugin_updater.py",
                "/srv/oxide/plugins",
                "--recursive",
                "--update-plugin",
                "Kits",
                "--no-log",
                "--no-state",
            ],
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ) as stderr:
            rc = checker.main()

        self.assertEqual(rc, 2)
        self.assertIn("Plugin name is ambiguous", stderr.getvalue())

    def test_targeted_check_loads_chaos_fallback_only_after_umod_miss(self):
        local = {
            "file": "/srv/oxide/plugins/HeliRide.cs",
            "filename": "HeliRide.cs",
            "name": "Heli Ride",
            "author": "Example Author",
            "version": "1.0.0",
            "size_bytes": 500,
            "sha256": "a" * 64,
        }
        chaos = {
            "heliride": {
                "ResourceVersion": "1.1.0",
                "ResourceURL": "https://chaoscode.io/resources/heli-ride.1/",
            }
        }

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=[local],
        ), mock.patch.object(
            checker,
            "http_get_json",
            side_effect=FileNotFoundError,
        ) as get_json, mock.patch.object(
            checker,
            "load_chaos_manifest",
            return_value=chaos,
        ) as load_chaos, mock.patch.object(
            checker,
            "install_update",
        ) as install, contextlib.redirect_stdout(
            io.StringIO()
        ) as stdout, contextlib.redirect_stderr(io.StringIO()):
            rc = checker.main(
                [
                    "/srv/oxide/plugins",
                    "--no-config",
                    "--cache",
                    str(Path(td) / "cache.json"),
                    "--color",
                    "never",
                    "--update-plugin",
                    "HeliRide",
                    "--no-log",
                    "--no-state",
                ]
            )

        self.assertEqual(rc, 1)
        get_json.assert_called_once()
        load_chaos.assert_called_once()
        install.assert_not_called()
        self.assertIn("HeliRide.cs  chaos", stdout.getvalue())

    def test_reload_failure_is_reported_after_successful_install(self):
        local = {
            "file": "/srv/oxide/plugins/ExamplePlugin.cs",
            "filename": "ExamplePlugin.cs",
            "name": "Example Plugin",
            "author": "Example Author",
            "version": "1.0.0",
            "size_bytes": 500,
        }
        remote = checker.HttpResult(
            data={
                "latest_release_version": "1.1.0",
                "url": "https://umod.org/plugins/example-plugin",
                "download_url": "https://umod.org/plugins/ExamplePlugin.cs",
            },
            headers={},
        )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            checker,
            "scan_plugins",
            return_value=[local],
        ), mock.patch.object(
            checker,
            "http_get_json",
            return_value=remote,
        ), mock.patch.object(
            checker,
            "install_update",
            return_value=True,
        ), mock.patch.object(
            checker,
            "reload_updated_plugins",
            return_value=(False, "RCON unavailable"),
        ) as reload_plugins, mock.patch.object(
            sys,
            "argv",
            [
                "oxide_plugin_updater.py",
                "/srv/oxide/plugins",
                "--no-check-chaos",
                "--cache",
                str(Path(td) / "cache.json"),
                "--color",
                "never",
                "--update",
                "--no-log",
                "--no-state",
            ],
        ), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(
            io.StringIO()
        ):
            rc = checker.main()

        self.assertEqual(rc, 2)
        reload_plugins.assert_called_once()
        rendered = stdout.getvalue()
        self.assertIn("Plugin activation: FAILED", rendered)
        self.assertIn("RCON unavailable", rendered)

    def test_reload_uses_environment_password_and_exact_oxide_command(self):
        rcon_send = mock.Mock(
            side_effect=[
                (
                    True,
                    '01 "Example Plugin" (1.1.0) by Example '
                    '- ExamplePlugin.cs',
                ),
                (True, "ExamplePlugin was compiled successfully"),
                (
                    True,
                    '01 "Example Plugin" (1.2.3) by Example '
                    '- ExamplePlugin.cs',
                ),
            ]
        )
        watchdog = mock.Mock(
            rcon_send=rcon_send,
            rcon_extract_message=lambda value: value,
        )
        with mock.patch.dict(
            os.environ,
            {"TEST_RUST_RCON_PASSWORD": "secret"},
        ), mock.patch.object(
            checker.importlib,
            "import_module",
            return_value=watchdog,
        ):
            activation_records = []
            ok, response = checker.reload_updated_plugins(
                {
                    "identity": "rustserver",
                    "host": "127.0.0.1",
                    "port": 28016,
                    "password": "",
                    "password_environment_variable":
                        "TEST_RUST_RCON_PASSWORD",
                },
                [("ExamplePlugin.cs", "1.2.3")],
                activation_records=activation_records,
            )

        self.assertTrue(ok)
        self.assertEqual(response, "1 plugin activated and verified")
        self.assertEqual(rcon_send.call_count, 3)
        self.assertEqual(
            rcon_send.call_args_list[0].args[1],
            "oxide.plugins",
        )
        cfg, command = rcon_send.call_args_list[1].args
        self.assertEqual(command, "oxide.reload ExamplePlugin")
        self.assertEqual(cfg["rcon_password"], "secret")
        self.assertEqual(cfg["identity"], "rustserver")
        self.assertEqual(
            rcon_send.call_args_list[2].args[1],
            "oxide.plugins",
        )
        self.assertEqual(
            activation_records,
            [
                {
                    "plugin": "ExamplePlugin.cs",
                    "command": "oxide.reload ExamplePlugin",
                    "status": "OK",
                    "response": "ExamplePlugin was compiled successfully",
                }
            ],
        )

    def test_individual_reload_reports_compile_failure_and_refreshes_inventory(self):
        rcon_send = mock.Mock(
            side_effect=[
                (True, "Kits - Failed to compile: previous error"),
                (
                    True,
                    "Kits - Failed to compile: bad overload | "
                    "Line: 1840, Pos: 33",
                ),
                (
                    True,
                    "Kits - Failed to compile: bad overload | "
                    "Line: 1840, Pos: 33",
                ),
            ]
        )
        watchdog = mock.Mock(
            rcon_send=rcon_send,
            rcon_extract_message=lambda value: value,
        )
        progress = mock.Mock()
        with mock.patch.object(
            checker.importlib,
            "import_module",
            return_value=watchdog,
        ):
            ok, response = checker.reload_updated_plugins(
                {
                    "host": "127.0.0.1",
                    "port": 28016,
                    "password": "secret",
                },
                [("Kits.cs", "4.4.9")],
                progress=progress,
            )

        self.assertFalse(ok)
        self.assertIn("Kits.cs: Kits - Failed to compile", response)
        self.assertEqual(rcon_send.call_count, 3)
        self.assertEqual(rcon_send.call_args_list[1].args[1], "oxide.load Kits")
        self.assertEqual(
            rcon_send.call_args_list[2].args[1],
            "oxide.plugins",
        )
        progress.assert_called_once()
        self.assertEqual(
            progress.call_args.args[3],
            "FAILED TO COMPILE",
        )
        command_call = rcon_send.call_args_list[1]
        self.assertEqual(command_call.args[1], "oxide.load Kits")
        self.assertEqual(command_call.kwargs["timeout_s"], 120)
        completion_matcher = command_call.kwargs["response_matcher"]
        self.assertFalse(completion_matcher("oxide.load accepted"))
        self.assertFalse(
            completion_matcher("Kits was compiled successfully")
        )
        self.assertFalse(
            completion_matcher("Unloaded plugin Kits v4.4.9 by k1lly0u")
        )
        self.assertTrue(
            completion_matcher("Loaded plugin Kits v4.4.9 by k1lly0u")
        )
        self.assertTrue(
            completion_matcher(
                "Error while compiling Kits: bad overload"
            )
        )

    def test_full_inventory_reports_49_loaded_and_three_compile_failures(self):
        loaded = [f"Plugin{index:02d}" for index in range(1, 50)]
        broken = ["HeliRide", "NpcHorses", "UberTool"]
        inventory_text = "\n".join(
            [
                (
                    f'{index:02d} "{name}" (1.0.0) by Example '
                    f"- {name}.cs"
                )
                for index, name in enumerate(loaded, start=1)
            ]
            + [
                (
                    f"{index:02d} {name} - Failed to compile: "
                    "compatibility error"
                )
                for index, name in enumerate(broken, start=50)
            ]
        )

        def send(_cfg, command, **kwargs):
            if command == "oxide.plugins":
                return True, inventory_text
            plugin_name = command.rsplit(" ", 1)[-1]
            if plugin_name in broken:
                response = (
                    f"Error while compiling {plugin_name}: "
                    "compatibility error"
                )
            else:
                response = (
                    f"Loaded plugin {plugin_name} v1.0.0 by Example"
                )
            self.assertTrue(kwargs["response_matcher"](response))
            self.assertEqual(kwargs["timeout_s"], 120)
            return True, response

        rcon_send = mock.Mock(side_effect=send)
        watchdog = mock.Mock(
            rcon_send=rcon_send,
            rcon_extract_message=lambda value: value,
        )
        requested = [
            (f"{name}.cs", "1.0.0")
            for name in loaded + broken
        ]
        activation_records = []

        with mock.patch.object(
            checker.importlib,
            "import_module",
            return_value=watchdog,
        ):
            ok, response = checker.reload_updated_plugins(
                {
                    "host": "127.0.0.1",
                    "port": 28016,
                    "password": "secret",
                },
                requested,
                activation_records=activation_records,
            )

        self.assertFalse(ok)
        self.assertEqual(len(activation_records), 52)
        self.assertEqual(
            sum(record["status"] == "OK" for record in activation_records),
            49,
        )
        self.assertEqual(
            sum(
                record["status"] == "FAILED TO COMPILE"
                for record in activation_records
            ),
            3,
        )
        for plugin_name in broken:
            self.assertIn(
                f"{plugin_name}.cs: Error while compiling {plugin_name}",
                response,
            )
        self.assertEqual(rcon_send.call_count, 54)

    def test_reload_shows_current_target_and_preserves_loaded_result(self):
        inventory = (
            '01 "Admin No Loot" (0.1.3) by Dana (0.00s / 0 B) '
            "- AdminNoLoot.cs"
        )
        loaded = "Loaded plugin Admin No Loot v0.1.3 by Dana"
        rcon_send = mock.Mock(
            side_effect=[
                (True, inventory),
                (True, loaded),
                (True, inventory),
            ]
        )
        watchdog = mock.Mock(
            rcon_send=rcon_send,
            rcon_extract_message=lambda value: value,
        )
        activation_records = []

        with mock.patch.object(
            checker.importlib,
            "import_module",
            return_value=watchdog,
        ), mock.patch.object(
            checker,
            "ActivitySpinner",
        ) as spinner:
            ok, response = checker.reload_updated_plugins(
                {
                    "host": "127.0.0.1",
                    "port": 28016,
                    "password": "secret",
                },
                [("AdminNoLoot.cs", "0.1.3")],
                activation_records=activation_records,
            )

        self.assertTrue(ok, response)
        spinner.assert_called_once_with(
            "  [1/1] Verifying Admin No Loot v0.1.3 by Dana "
            "(oxide.reload AdminNoLoot)",
            message_on_own_line=True,
        )
        self.assertEqual(
            activation_records,
            [
                {
                    "plugin": "AdminNoLoot.cs",
                    "command": "oxide.reload AdminNoLoot",
                    "status": "OK",
                    "response": loaded,
                }
            ],
        )
        matcher = rcon_send.call_args_list[1].kwargs["response_matcher"]
        self.assertFalse(
            matcher("Unloaded plugin Admin No Loot v0.1.3 by Dana")
        )
        self.assertTrue(matcher(loaded))

    def test_individual_reload_rejects_missing_expected_inventory_version(self):
        rcon_send = mock.Mock(
            side_effect=[
                (
                    True,
                    '01 "Example Plugin" (1.2.2) by Example '
                    '- ExamplePlugin.cs',
                ),
                (True, "reload complete"),
                (
                    True,
                    '01 "Example Plugin" (1.2.2) by Example '
                    '- ExamplePlugin.cs',
                ),
            ]
        )
        watchdog = mock.Mock(
            rcon_send=rcon_send,
            rcon_extract_message=lambda value: value,
        )
        activation_records = []
        with mock.patch.object(
            checker.importlib,
            "import_module",
            return_value=watchdog,
        ):
            ok, response = checker.reload_updated_plugins(
                {
                    "host": "127.0.0.1",
                    "port": 28016,
                    "password": "secret",
                },
                [("ExamplePlugin.cs", "1.2.3")],
                activation_records=activation_records,
            )

        self.assertFalse(ok)
        self.assertIn(
            "expected version 1.2.3 not found in oxide.plugins",
            response,
        )
        self.assertEqual(
            activation_records[0]["status"],
            "VERIFY FAILED",
        )
        self.assertIn(
            "expected version 1.2.3 not found",
            activation_records[0]["response"],
        )

    def test_activation_inventory_matching_does_not_confuse_similar_names(self):
        rcon_send = mock.Mock(
            side_effect=[
                (
                    True,
                    '01 "Foo Plus" (1.0.0) by Example - Foo-Plus.cs',
                ),
                (True, "Foo was compiled successfully"),
                (
                    True,
                    '01 "Foo" (1.0.0) by Example - Foo.cs',
                ),
            ]
        )
        watchdog = mock.Mock(
            rcon_send=rcon_send,
            rcon_extract_message=lambda value: value,
        )
        with mock.patch.object(
            checker.importlib,
            "import_module",
            return_value=watchdog,
        ):
            ok, response = checker.reload_updated_plugins(
                {
                    "host": "127.0.0.1",
                    "port": 28016,
                    "password": "secret",
                },
                [("Foo.cs", "1.0.0")],
            )

        self.assertTrue(ok, response)
        self.assertEqual(
            rcon_send.call_args_list[1].args[1],
            "oxide.load Foo",
        )

    def test_final_inventory_version_requires_an_exact_token(self):
        rcon_send = mock.Mock(
            side_effect=[
                (
                    True,
                    '01 "Example Plugin" (1.2.2) by Example '
                    '- ExamplePlugin.cs',
                ),
                (True, "reload complete"),
                (
                    True,
                    '01 "Example Plugin" (1.2.30) by Example '
                    '- ExamplePlugin.cs',
                ),
            ]
        )
        watchdog = mock.Mock(
            rcon_send=rcon_send,
            rcon_extract_message=lambda value: value,
        )
        with mock.patch.object(
            checker.importlib,
            "import_module",
            return_value=watchdog,
        ):
            ok, response = checker.reload_updated_plugins(
                {
                    "host": "127.0.0.1",
                    "port": 28016,
                    "password": "secret",
                },
                [("ExamplePlugin.cs", "1.2.3")],
            )

        self.assertFalse(ok)
        self.assertIn(
            "expected version 1.2.3 not found in oxide.plugins",
            response,
        )

    def test_audit_logger_appends_structured_records(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log" / "oxide_plugin_updater.log"
            audit = checker.AuditLogger(path)
            audit.write(
                "update_summary",
                updated=2,
                failed_or_refused=1,
            )

            record = checker.json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(record["event"], "update_summary")
        self.assertEqual(record["updated"], 2)
        self.assertEqual(record["failed_or_refused"], 1)
        self.assertIn("ts", record)


if __name__ == "__main__":
    unittest.main()
