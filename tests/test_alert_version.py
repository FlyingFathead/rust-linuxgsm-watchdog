import unittest

import rust_watchdog
from rust_watchdog_alerts import Alert, AlertManager


class AlertVersionTests(unittest.TestCase):
    def setUp(self):
        self.manager = AlertManager(
            {
                "alerts": {
                    "enabled": False,
                    "include_host": False,
                    "include_identity": False,
                }
            }
        )

    @staticmethod
    def _alert(version):
        return Alert(
            event="watchdog_started",
            level="INFO",
            title="started",
            text="watchdog loop online",
            fields={"version": version},
            ts=0,
        )

    def test_main_version_is_canonical_alert_version(self):
        captured = {}

        class CaptureManager:
            def emit(self, **kwargs):
                captured.update(kwargs)

        previous = rust_watchdog.ALERTS
        rust_watchdog.ALERTS = CaptureManager()
        try:
            rust_watchdog.alert("watchdog_started")
        finally:
            rust_watchdog.ALERTS = previous

        self.assertEqual(captured["version"], rust_watchdog.__version__)
        self.assertEqual(rust_watchdog._runtime_version_label(), "v0.4.4")

    def test_versioned_label_is_used_by_every_renderer(self):
        alert = self._alert(rust_watchdog.__version__)

        self.assertTrue(
            self.manager._render_html(alert).startswith(
                "🟢 <b>rust-linuxgsm-watchdog (v0.4.4)</b> -- "
                "<b>started</b>"
            )
        )
        self.assertTrue(
            self.manager._render_plain(alert).startswith(
                "🟢 rust-linuxgsm-watchdog (v0.4.4) -- started"
            )
        )
        self.assertTrue(
            self.manager._render_telegram_markdown(alert, v2=False).startswith(
                "🟢 *rust-linuxgsm-watchdog (v0.4.4)* -- *started*"
            )
        )
        self.assertIn(
            r"rust\-linuxgsm\-watchdog \(v0\.4\.4\)",
            self.manager._render_telegram_markdown(alert, v2=True),
        )

    def test_missing_version_falls_back_to_na(self):
        alert = self._alert("")
        self.manager.version_default = "0.0.0-stale-config-value"

        self.assertIn(
            "<b>rust-linuxgsm-watchdog (N/A)</b>",
            self.manager._render_html(alert),
        )
        self.assertIn(
            "rust-linuxgsm-watchdog (N/A) -- started",
            self.manager._render_plain(alert),
        )
        self.assertIn(
            "*rust-linuxgsm-watchdog (N/A)*",
            self.manager._render_telegram_markdown(alert, v2=False),
        )


if __name__ == "__main__":
    unittest.main()
