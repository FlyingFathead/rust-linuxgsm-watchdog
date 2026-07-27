import tempfile
import unittest
from pathlib import Path

from rust_watchdog_alerts import Alert, AlertManager


class AlertFootnoteRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = AlertManager(
            {
                "alerts": {
                    "enabled": False,
                    "include_host": False,
                    "include_identity": False,
                    "state_path": str(Path(self.tmp.name) / "alerts.json"),
                }
            }
        )
        self.alert = Alert(
            event="startup_ok",
            level="INFO",
            title="Startup OK",
            text="server looks healthy",
            fields={
                "_footnote_lines": [
                    "Server last wiped: 2026-07-31 18:09:00 UTC (RCON)",
                    "(5 days, 23 hours, 51 minutes ago)",
                    "",
                    "Server last restarted: unknown "
                    "(no Rust process start timestamp recorded)",
                ]
            },
            ts=0,
        )

    def test_html_footnotes_are_separate_and_italic(self):
        rendered = self.manager._render_html(self.alert)
        self.assertIn(
            "\n\n<i>Server last wiped: 2026-07-31 18:09:00 UTC "
            "(RCON)</i>\n"
            "<i>(5 days, 23 hours, 51 minutes ago)</i>\n\n"
            "<i>Server last restarted: unknown "
            "(no Rust process start timestamp recorded)</i>",
            rendered,
        )
        self.assertNotIn("<i></i>", rendered)
        self.assertNotIn("_footnote_lines=", rendered)

    def test_existing_discord_renderer_uses_markdown_italics(self):
        rendered = self.manager._render_plain(self.alert)
        self.assertIn(
            "\n\n*Server last wiped: 2026-07-31 18:09:00 UTC "
            "(RCON)*\n"
            "*(5 days, 23 hours, 51 minutes ago)*\n\n"
            "*Server last restarted: unknown "
            "(no Rust process start timestamp recorded)*",
            rendered,
        )

    def test_telegram_markdown_footnotes_use_italic_markup(self):
        rendered = self.manager._render_telegram_markdown(
            self.alert,
            v2=False,
        )
        self.assertIn(
            "\n\n_Server last wiped: 2026-07-31 18:09:00 UTC "
            "(RCON)_\n"
            "_(5 days, 23 hours, 51 minutes ago)_\n\n"
            "_Server last restarted: unknown "
            "(no Rust process start timestamp recorded)_",
            rendered,
        )

    def test_elapsed_footnotes_do_not_change_dedupe_key(self):
        rendered = self.manager._render(self.alert)
        self.assertNotIn("Server last wiped:", rendered)
        self.assertNotIn("Server last restarted:", rendered)


if __name__ == "__main__":
    unittest.main()
