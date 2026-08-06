import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rust_watchdog as wd


class TtyBuffer(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class ViewConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "rust_watchdog.json"

    def write_config(self, document):
        self.config_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def effective_config(self):
        cfg = wd.load_cfg(str(self.config_path))
        cfg = wd.normalize_cfg_paths(cfg, str(self.config_path))
        wd.apply_recovery_toggles(cfg)
        return cfg

    def test_view_shows_defaults_overrides_normalized_paths_and_toggles(self):
        self.write_config(
            {
                "server_dir": "server-root",
                "enable_server_update": False,
                "recovery_steps": ["update", "mu", "restart"],
                "alerts": {
                    "enabled": True,
                    "telegram": {"timeout_s": 13},
                },
            }
        )
        cfg = self.effective_config()
        stream = io.StringIO()

        wd.print_effective_config(
            cfg,
            str(self.config_path),
            stream=stream,
        )
        rendered = stream.getvalue()

        self.assertIn(
            f"server_dir          {self.config_path.parent / 'server-root'}",
            rendered,
        )
        self.assertIn(
            'recovery_steps                        ["mu", "restart"]',
            rendered,
        )
        self.assertIn("timeout_s            13", rendered)
        self.assertIn(
            "token_env            RUST_WD_TELEGRAM_TOKEN",
            rendered,
        )
        self.assertIn("preflight_getme      false", rendered)
        self.assertNotIn("_recovery_steps_original", rendered)
        self.assertNotIn("\033[", rendered)

    def test_both_cli_spellings_exit_before_runtime_side_effects(self):
        lockfile = Path(self.tmp.name) / "must-not-be-created.lock"
        logfile = Path(self.tmp.name) / "must-not-be-created.log"
        self.write_config(
            {
                "lockfile": str(lockfile),
                "logfile": str(logfile),
            }
        )

        for flag in ("--view-config", "--viewconfig"):
            with self.subTest(flag=flag):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(wd.__file__).resolve()),
                        "--config",
                        str(self.config_path),
                        flag,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr,
                )
                self.assertIn(
                    "Rust Watchdog v0.4.11 -- effective configuration",
                    completed.stdout,
                )
                self.assertFalse(lockfile.exists())
                self.assertFalse(logfile.exists())

    def test_interactive_utf8_terminal_gets_symbols_and_color(self):
        self.write_config({})
        stream = TtyBuffer()
        with mock.patch.dict(
            os.environ,
            {"TERM": "xterm-256color"},
            clear=False,
        ):
            os.environ.pop("NO_COLOR", None)
            wd.print_effective_config(
                self.effective_config(),
                str(self.config_path),
                stream=stream,
            )

        rendered = stream.getvalue()
        self.assertIn("⚙ Rust Watchdog v0.4.11", rendered)
        self.assertIn("\033[1;36m", rendered)

    def test_no_color_disables_ansi_without_disabling_utf8(self):
        self.write_config({})
        stream = TtyBuffer()
        with mock.patch.dict(
            os.environ,
            {"TERM": "xterm-256color", "NO_COLOR": "1"},
            clear=False,
        ):
            wd.print_effective_config(
                self.effective_config(),
                str(self.config_path),
                stream=stream,
            )

        rendered = stream.getvalue()
        self.assertIn("⚙ Rust Watchdog v0.4.11", rendered)
        self.assertNotIn("\033[", rendered)


if __name__ == "__main__":
    unittest.main()
