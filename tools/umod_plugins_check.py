#!/usr/bin/env python3
"""Backward-compatible launcher and import shim for Oxide Plugin Updater."""

from oxide_plugin_updater import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main(legacy_check_only=True))
