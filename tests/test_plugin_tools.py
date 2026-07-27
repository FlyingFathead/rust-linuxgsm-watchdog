import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import oxide_plugins_inventory as inventory  # noqa: E402
import umod_plugins_check as checker  # noqa: E402


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
            ["umod_plugins_check.py"],
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
            ["umod_plugins_check.py", str(chosen)],
        ), contextlib.redirect_stdout(io.StringIO()):
            rc = checker.main()

        self.assertEqual(rc, 0)
        scan.assert_called_once_with(chosen, recursive=False)


if __name__ == "__main__":
    unittest.main()
