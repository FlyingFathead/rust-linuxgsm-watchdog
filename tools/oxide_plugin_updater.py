#!/usr/bin/env python3
"""
Oxide Plugin Updater

Check local Oxide/uMod Rust plugins (*.cs) against uMod plugin JSON endpoints,
with optional fallback to ChaosCode manifest for plugins that do not match uMod.

Features:
- defaults to $HOME/serverfiles/oxide/plugins
- cache to disk (TTL)
- proper 429 handling (Retry-After / X-Retry-After)
- randomized request-interval throttling
- progress output
- optional ANSI colors
- optional ChaosCode fallback (default: on)
- check-only by default, with an apt-like update summary
- optional validated uMod updates with backups and atomic replacement

Exit codes:
- 0: all OK, or every update candidate was installed successfully
- 1: at least one plugin remains OUTDATED
- 2: at least one UNKNOWN, ERROR, or update failure
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib
import json
import os
import random
import re
import shlex
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

DEFAULT_PLUGINS_DIR = Path.home() / "serverfiles" / "oxide" / "plugins"

# ------------------------------------------------------------
# Import your inventory scanner (local source of truth)
# ------------------------------------------------------------
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from oxide_plugins_inventory import extract_plugin_info, scan_plugins  # type: ignore
except Exception as e:
    print(f"FATAL: cannot import plugin scanner from oxide_plugins_inventory.py: {e}", file=sys.stderr)
    raise SystemExit(2)

USER_AGENT = "rust-linuxgsm-watchdog/oxide_plugin_updater (stdlib urllib)"

UMOD_PLUGIN_JSON = "https://umod.org/plugins/{name}.json"
UMOD_SEARCH_JSON = "https://umod.org/plugins/search.json"
UMOD_PLUGIN_DOWNLOAD = "https://umod.org/plugins/{filename}"

CHAOS_MANIFEST_JSON = "https://chaoscode.io/api/resource_manifest.json"

CACHE_DIR_DEFAULT = HERE / "data" / "cache"
CACHE_FILE_DEFAULT = CACHE_DIR_DEFAULT / "oxide_plugin_updater_cache.json"
CACHE_TTL_SECONDS_DEFAULT = 60 * 60
CONFIG_FILE_DEFAULT = HERE / "oxide_plugin_updater.json"
LOG_FILE_DEFAULT = HERE.parent / "log" / "oxide_plugin_updater.log"
PLUGIN_BACKUP_DIR_DEFAULT = HERE / "data" / "plugin-backups"
STATE_FILE_DEFAULT = HERE / "data" / "state" / "plugin_history.json"

CHAOS_CACHE_TTL_SECONDS_DEFAULT = 45 * 60  # manifest updates ~31m; keep a little headroom

MAX_PLUGIN_BYTES = 10 * 1024 * 1024
SHRINK_WARN_PERCENT = 25.0
SHRINK_REFUSE_PERCENT = 50.0
RCON_ACTIVATION_COMMAND = "oxide.load|oxide.reload <updated-plugin>"
_ACTIVITY_SPINNER_ENABLED = True
_NETWORK_AUDIT = None

CONFIG_DEFAULTS: Dict[str, Any] = {
    "plugins_directory": str(DEFAULT_PLUGINS_DIR),
    "recursive": False,
    "sources": {
        "check_chaoscode": True,
        "fallback_search": False,
    },
    "network": {
        "timeout_seconds": 12,
        "minimum_interval_seconds": 1.5,
        "maximum_interval_seconds": 3.0,
        "maximum_retries": 6,
        "maximum_backoff_seconds": 300,
        "fallback_backoff_seconds": 30,
        "show_activity_spinner": True,
    },
    "cache": {
        "file": str(CACHE_FILE_DEFAULT),
        "umod_ttl_seconds": CACHE_TTL_SECONDS_DEFAULT,
        "chaoscode_ttl_seconds": CHAOS_CACHE_TTL_SECONDS_DEFAULT,
    },
    "validation": {
        "maximum_plugin_bytes": MAX_PLUGIN_BYTES,
        "shrink_warning_percent": SHRINK_WARN_PERCENT,
        "shrink_refusal_percent": SHRINK_REFUSE_PERCENT,
    },
    "updates": {
        "backup_directory": str(PLUGIN_BACKUP_DIR_DEFAULT),
        "reload_plugins_after_updates": True,
    },
    "state": {
        "enabled": True,
        "file": str(STATE_FILE_DEFAULT),
        "history_limit_per_plugin": 50,
        "reload_history_limit": 50,
    },
    "rcon": {
        "identity": "rustserver",
        "host": "127.0.0.1",
        "port": 28016,
        "password": "",
        "password_environment_variable": "RUST_RCON_PASSWORD",
    },
    "logging": {
        "enabled": True,
        "file": str(LOG_FILE_DEFAULT),
    },
    "output": {
        "color": "auto",
    },
}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if (
            isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_updater_config(path: Path) -> Dict[str, Any]:
    """Load and merge the updater JSON with built-in compatibility defaults."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"configuration root must be a JSON object: {path}")
    return _deep_merge(CONFIG_DEFAULTS, raw)


def _load_updater_config_document(path: Path) -> Dict[str, Any]:
    """Load the unmerged JSON document used for persistent config edits."""
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if not raw_text.strip():
        raise ValueError(f"configuration file is empty: {path}")
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e
    if not isinstance(document, dict):
        raise ValueError(f"configuration root must be a JSON object: {path}")
    return document


def _atomic_write_updater_config(
    path: Path,
    document: Dict[str, Any],
) -> Optional[Path]:
    """Atomically write JSON, backing up an existing configuration first."""
    path = path.expanduser().resolve()
    if not path.parent.is_dir():
        raise ValueError(
            f"configuration parent directory does not exist: {path.parent}"
        )

    timestamp = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    backup_path: Optional[Path] = None
    original_mode = 0o644
    if path.exists():
        if not path.is_file():
            raise ValueError(
                f"configuration path is not a regular file: {path}"
            )
        original_mode = path.stat().st_mode & 0o7777
        backup_directory = path.parent / "data" / "config-backups"
        backup_directory.mkdir(parents=True, exist_ok=True)
        backup_path = backup_directory / f"{path.name}.bak.{timestamp}"
        shutil.copy2(path, backup_path)

    temp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, original_mode)
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return backup_path


def set_plugins_directory_config(
    config_path: Path,
    plugins_directory: Path,
) -> Dict[str, Any]:
    """Persist one validated Oxide plugin-directory setting and stop there."""
    config_path = config_path.expanduser().resolve()
    plugins_directory = plugins_directory.expanduser().resolve()
    if not plugins_directory.exists():
        raise ValueError(
            f"plugin directory does not exist: {plugins_directory}"
        )
    if not plugins_directory.is_dir():
        raise ValueError(
            f"plugin directory is not a directory: {plugins_directory}"
        )

    existed = config_path.exists()
    if existed:
        document = _load_updater_config_document(config_path)
    else:
        document = {}

    old_value = document.get("plugins_directory")
    old_resolved: Optional[Path] = None
    if isinstance(old_value, str) and old_value.strip():
        old_resolved = _resolve_config_path(old_value, config_path)

    changed = not existed or old_resolved != plugins_directory
    backup_path: Optional[Path] = None
    if changed:
        document["plugins_directory"] = str(plugins_directory)
        validate_updater_config(_deep_merge(CONFIG_DEFAULTS, document))
        backup_path = _atomic_write_updater_config(config_path, document)

    return {
        "config_path": config_path,
        "old_value": old_value,
        "new_value": str(plugins_directory),
        "changed": changed,
        "created": not existed and changed,
        "backup_path": backup_path,
    }


def print_plugins_directory_config_result(result: Dict[str, Any]) -> None:
    config_path = result["config_path"]
    old_value = result.get("old_value")
    print(f"CONFIG: {config_path}")
    if result.get("created"):
        print("CREATED: new updater configuration")
    if result.get("changed"):
        print(
            "CONFIG CHANGE: $.plugins_directory: "
            f"{old_value if old_value is not None else '<unset>'} "
            f"-> {result['new_value']}"
        )
        if result.get("backup_path") is not None:
            print(f"BACKUP: {result['backup_path']}")
        print(f"SAVED: {config_path}")
    else:
        print(
            "NO CHANGES: plugins_directory already resolves to "
            f"{result['new_value']}."
        )


def validate_updater_config(config: Dict[str, Any]) -> None:
    sections = (
        "sources",
        "network",
        "cache",
        "validation",
        "updates",
        "state",
        "rcon",
        "logging",
        "output",
    )
    for section in sections:
        if not isinstance(config.get(section), dict):
            raise ValueError(f"{section} must be a JSON object")

    boolean_values = {
        "recursive": config.get("recursive"),
        "sources.check_chaoscode":
            config["sources"].get("check_chaoscode"),
        "sources.fallback_search":
            config["sources"].get("fallback_search"),
        "network.show_activity_spinner":
            config["network"].get("show_activity_spinner"),
        "updates.reload_plugins_after_updates":
            config["updates"].get("reload_plugins_after_updates"),
        "state.enabled": config["state"].get("enabled"),
        "logging.enabled": config["logging"].get("enabled"),
    }
    for name, value in boolean_values.items():
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")

    integer_minimums = {
        "network.timeout_seconds":
            (config["network"].get("timeout_seconds"), 1),
        "network.maximum_retries":
            (config["network"].get("maximum_retries"), 0),
        "network.maximum_backoff_seconds":
            (config["network"].get("maximum_backoff_seconds"), 1),
        "network.fallback_backoff_seconds":
            (config["network"].get("fallback_backoff_seconds"), 1),
        "cache.umod_ttl_seconds":
            (config["cache"].get("umod_ttl_seconds"), 0),
        "cache.chaoscode_ttl_seconds":
            (config["cache"].get("chaoscode_ttl_seconds"), 0),
        "validation.maximum_plugin_bytes":
            (config["validation"].get("maximum_plugin_bytes"), 200),
        "state.history_limit_per_plugin":
            (config["state"].get("history_limit_per_plugin"), 0),
        "state.reload_history_limit":
            (config["state"].get("reload_history_limit"), 0),
        "rcon.port": (config["rcon"].get("port"), 1),
    }
    for name, (value, minimum) in integer_minimums.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
    if config["rcon"]["port"] > 65535:
        raise ValueError("rcon.port must not exceed 65535")

    float_minimums = {
        "network.minimum_interval_seconds":
            (config["network"].get("minimum_interval_seconds"), 0.0),
        "network.maximum_interval_seconds":
            (config["network"].get("maximum_interval_seconds"), 0.0),
        "validation.shrink_warning_percent":
            (config["validation"].get("shrink_warning_percent"), 0.0),
        "validation.shrink_refusal_percent":
            (config["validation"].get("shrink_refusal_percent"), 0.0),
    }
    for name, (value, minimum) in float_minimums.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        if float(value) < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
    warn = float(config["validation"]["shrink_warning_percent"])
    refuse = float(config["validation"]["shrink_refusal_percent"])
    if warn > refuse:
        raise ValueError(
            "validation.shrink_warning_percent must not exceed "
            "validation.shrink_refusal_percent"
        )
    if refuse > 100:
        raise ValueError(
            "validation.shrink_refusal_percent must not exceed 100"
        )
    minimum_interval = float(
        config["network"]["minimum_interval_seconds"]
    )
    maximum_interval = float(
        config["network"]["maximum_interval_seconds"]
    )
    if maximum_interval < minimum_interval:
        raise ValueError(
            "network.maximum_interval_seconds must be greater than or equal "
            "to network.minimum_interval_seconds"
        )

    fallback = config["network"]["fallback_backoff_seconds"]
    maximum = config["network"]["maximum_backoff_seconds"]
    if fallback > maximum:
        raise ValueError(
            "network.fallback_backoff_seconds must not exceed "
            "network.maximum_backoff_seconds"
        )
    if config["output"].get("color") not in {"auto", "always", "never"}:
        raise ValueError("output.color must be auto, always, or never")

    path_values = {
        "plugins_directory": config.get("plugins_directory"),
        "cache.file": config["cache"].get("file"),
        "updates.backup_directory":
            config["updates"].get("backup_directory"),
        "state.file": config["state"].get("file"),
        "logging.file": config["logging"].get("file"),
    }
    for name, value in path_values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty path string")


def _resolve_config_path(value: Any, config_path: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("configured path must not be empty")
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded
    return (config_path.parent / expanded).resolve()


def _config_section(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def effective_config_for_display(
    config: Dict[str, Any],
    config_path: Path,
) -> Dict[str, Any]:
    """Return a resolved, secret-safe copy suitable for terminal output."""
    rendered = copy.deepcopy(config)
    rendered["plugins_directory"] = str(
        _resolve_config_path(rendered["plugins_directory"], config_path)
    )
    path_fields = (
        ("cache", "file"),
        ("updates", "backup_directory"),
        ("state", "file"),
        ("logging", "file"),
    )
    for section, key in path_fields:
        rendered[section][key] = str(
            _resolve_config_path(rendered[section][key], config_path)
        )
    if rendered["rcon"].get("password"):
        rendered["rcon"]["password"] = "<redacted>"
    return rendered


def print_effective_updater_config(
    config: Dict[str, Any],
    config_path: Path,
    *,
    no_config: bool,
) -> None:
    print(
        "CONFIG: "
        + ("<built-in defaults>" if no_config else str(config_path))
    )
    print(
        json.dumps(
            effective_config_for_display(config, config_path),
            ensure_ascii=False,
            indent=2,
        )
    )


def _explicit_option(argv: List[str], option: str) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in argv)

# ------------------------------------------------------------
# Version compare (best-effort)
# ------------------------------------------------------------
def parse_version(v: str) -> Tuple[Tuple[int, ...], str]:
    v = (v or "").strip()
    if v.startswith(("v", "V")):
        v = v[1:].strip()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)?(.*)$", v)
    if not m:
        return ((), v)
    nums_s = (m.group(1) or "").strip()
    suffix = (m.group(2) or "").strip()
    if nums_s:
        nums = tuple(int(x) for x in nums_s.split(".") if re.match(r"^\d+$", x))
    else:
        nums = ()
    return (nums, suffix)

def version_is_newer(remote: str, local: str) -> Optional[bool]:
    if not remote or not local:
        return None
    r_nums, r_suf = parse_version(remote)
    l_nums, l_suf = parse_version(local)
    if not r_nums or not l_nums:
        return None
    if r_nums != l_nums:
        return r_nums > l_nums
    if r_suf == l_suf:
        return False
    if (not r_suf) and l_suf:
        return True
    return None

# ------------------------------------------------------------
# Cache
# ------------------------------------------------------------
def ensure_cache_path(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

def load_cache(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            loaded = json.loads(
                path.read_text(encoding="utf-8", errors="replace") or "{}"
            )
            return loaded if isinstance(loaded, dict) else {}
    except Exception:
        pass
    return {}

def save_cache(path: Path, obj: Dict[str, Any]) -> None:
    ensure_cache_path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _empty_plugin_state() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": "",
        "plugins": {},
        "reload_history": [],
    }


def load_plugin_state(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_plugin_state()
    except Exception as e:
        raise ValueError(f"cannot load plugin state {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"plugin state root must be a JSON object: {path}")
    if not isinstance(raw.get("plugins"), dict):
        raw["plugins"] = {}
    if not isinstance(raw.get("reload_history"), list):
        raw["reload_history"] = []
    raw["schema_version"] = 1
    return raw


def save_plugin_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["schema_version"] = 1
    state["updated_at"] = dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _append_limited(
    records: List[Dict[str, Any]],
    record: Dict[str, Any],
    limit: int,
) -> None:
    records.append(record)
    if limit > 0 and len(records) > limit:
        del records[:-limit]


def plugin_state_key(plugin: Dict[str, Any], plugins_dir: Path) -> str:
    path = Path(str(plugin.get("file") or plugin.get("filename") or ""))
    try:
        return path.resolve().relative_to(plugins_dir.resolve()).as_posix()
    except ValueError:
        return str(plugin.get("filename") or path.name)


def observe_plugin_state(
    state: Dict[str, Any],
    *,
    key: str,
    plugin: Dict[str, Any],
    row: Dict[str, Any],
    history_limit: int,
    observed_at: Optional[str] = None,
) -> str:
    """
    Record a check result and classify OUTDATED as new, known, or changed.

    UNKNOWN/ERROR never erase an earlier known-outdated record.
    """
    now = observed_at or dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    plugins = state.setdefault("plugins", {})
    entry = plugins.setdefault(key, {})
    history = entry.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        entry["history"] = history

    local = {
        "version": str(plugin.get("version") or ""),
        "sha256": str(plugin.get("sha256") or ""),
        "size_bytes": int(plugin.get("size_bytes") or 0),
        "mtime": str(plugin.get("mtime") or ""),
    }
    remote = {
        "source": str(row.get("source") or ""),
        "version": str(row.get("remote") or ""),
        "url": str(row.get("remote_url") or ""),
    }
    status = str(row.get("status") or "UNKNOWN")

    entry.update(
        {
            "filename": str(plugin.get("filename") or Path(key).name),
            "path": str(plugin.get("file") or ""),
            "name": str(plugin.get("name") or ""),
            "author": str(plugin.get("author") or ""),
            "current": local,
            "remote": remote,
            "last_check_status": status,
            "last_checked_at": now,
        }
    )

    active = entry.get("active_outdated")
    if status == "OUTDATED":
        fingerprint = {
            "local_version": local["version"],
            "local_sha256": local["sha256"],
            "remote_version": remote["version"],
            "source": remote["source"],
        }
        same = isinstance(active, dict) and all(
            active.get(field) == value
            for field, value in fingerprint.items()
        )
        if same:
            active["last_seen_at"] = now
            active["checks_seen"] = int(active.get("checks_seen") or 0) + 1
            active["remote_url"] = remote["url"]
            return "known"

        classification = "changed" if isinstance(active, dict) else "new"
        event = {
            "event": (
                "outdated_changed"
                if classification == "changed"
                else "outdated_detected"
            ),
            "at": now,
            **fingerprint,
            "remote_url": remote["url"],
        }
        if classification == "changed":
            event["previous"] = active
        _append_limited(history, event, history_limit)
        entry["active_outdated"] = {
            **fingerprint,
            "remote_url": remote["url"],
            "first_seen_at": now,
            "last_seen_at": now,
            "checks_seen": 1,
        }
        return classification

    if status == "OK" and isinstance(active, dict):
        _append_limited(
            history,
            {
                "event": "outdated_resolved",
                "at": now,
                "previous": active,
                "current_version": local["version"],
                "current_sha256": local["sha256"],
            },
            history_limit,
        )
        entry["active_outdated"] = None
    return ""


def record_installed_update(
    state: Dict[str, Any],
    *,
    key: str,
    candidate: UpdateCandidate,
    validation: DownloadValidation,
    backup_path: Path,
    history_limit: int,
) -> None:
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    plugins = state.setdefault("plugins", {})
    entry = plugins.setdefault(key, {})
    history = entry.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        entry["history"] = history
    _append_limited(
        history,
        {
            "event": "update_installed",
            "at": now,
            "source": "umod",
            "from_version": candidate.local_version,
            "to_version": candidate.remote_version,
            "old_size": validation.old_size,
            "new_size": validation.new_size,
            "old_sha256": validation.old_sha256,
            "new_sha256": validation.new_sha256,
            "download_url": candidate.download_url,
            "backup": str(backup_path),
        },
        history_limit,
    )
    entry["current"] = {
        "version": candidate.remote_version,
        "sha256": validation.new_sha256,
        "size_bytes": validation.new_size,
        "mtime": now,
    }
    entry["last_check_status"] = "OK"
    entry["active_outdated"] = None


def record_reload_state(
    state: Dict[str, Any],
    *,
    result: str,
    detail: str,
    updated_plugins: int,
    limit: int,
    activations: Optional[List[Dict[str, str]]] = None,
) -> None:
    records = state.setdefault("reload_history", [])
    if not isinstance(records, list):
        records = []
        state["reload_history"] = records
    _append_limited(
        records,
        {
            "at": dt.datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            "command": RCON_ACTIVATION_COMMAND,
            "result": result,
            "detail": detail,
            "updated_plugins": updated_plugins,
            "activations": list(activations or []),
        },
        limit,
    )


def record_revalidated_source(
    state: Dict[str, Any],
    *,
    key: str,
    candidate: UpdateCandidate,
    validation: DownloadValidation,
    download_url: str,
    history_limit: int,
) -> None:
    """Record a forced pristine-source check which required no replacement."""
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    plugins = state.setdefault("plugins", {})
    entry = plugins.setdefault(key, {})
    history = entry.setdefault("history", [])
    if not isinstance(history, list):
        history = []
        entry["history"] = history
    _append_limited(
        history,
        {
            "event": "source_revalidated",
            "at": now,
            "source": "umod",
            "version": candidate.remote_version,
            "size_bytes": validation.new_size,
            "sha256": validation.new_sha256,
            "download_url": download_url,
        },
        history_limit,
    )
    entry["current"] = {
        "version": candidate.local_version,
        "sha256": validation.old_sha256,
        "size_bytes": validation.old_size,
        "mtime": now,
    }
    entry["last_check_status"] = "OK"
    entry["active_outdated"] = None

# ------------------------------------------------------------
# HTTP with 429 handling + rate-limit headers
# ------------------------------------------------------------
@dataclass
class HttpResult:
    data: Any
    headers: Dict[str, str]


@dataclass
class HttpBytesResult:
    data: bytes
    headers: Dict[str, str]
    final_url: str


@dataclass
class UpdateCandidate:
    filename: str
    path: Path
    name: str
    local_version: str
    remote_version: str
    download_url: str
    local_sha256: str = ""


@dataclass
class DownloadValidation:
    errors: List[str]
    warnings: List[str]
    candidate_name: str
    candidate_author: str
    candidate_version: str
    old_size: int
    new_size: int
    old_sha256: str
    new_sha256: str


@dataclass(frozen=True)
class InstallResult:
    success: bool
    source_changed: bool = False

    def __bool__(self) -> bool:
        return self.success


class AuditLogger:
    """Append small JSON records without making logging a runtime dependency."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self._failed = False

    def write(self, event: str, **fields: Any) -> None:
        if self.path is None or self._failed:
            return
        record = {
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        except OSError as e:
            self._failed = True
            print(f"WARNING: could not write audit log {self.path}: {e}", file=sys.stderr)


def _headers_dict(h) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for k, v in h.items():
            out[str(k)] = str(v)
    except Exception:
        pass
    return out

def _retry_after_seconds(headers: Dict[str, str]) -> Optional[int]:
    for k in ("Retry-After", "X-Retry-After"):
        v = headers.get(k)
        if v:
            try:
                return int(float(v))
            except Exception:
                pass
    return None


class ActivitySpinner:
    """TTY-only single-glyph activity spinner; silent for pipes and files."""

    FRAMES = ("\\", "|", "/", "-")

    def __init__(self, message: str):
        self.message = message
        self.enabled = bool(
            _ACTIVITY_SPINNER_ENABLED and sys.stderr.isatty()
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        index = 0
        while not self._stop.wait(0.12):
            frame = self.FRAMES[index % len(self.FRAMES)]
            print(
                f"\r{frame} {self.message}",
                end="",
                file=sys.stderr,
                flush=True,
            )
            index += 1

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(
                target=self._run,
                name="oxide-plugin-updater-spinner",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        print("\r\033[2K", end="", file=sys.stderr, flush=True)


def _console_event(tag: str, message: str) -> None:
    timestamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{tag}] {message}", file=sys.stderr, flush=True)


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total} second{'s' if total != 1 else ''}"
    minutes, remainder = divmod(total, 60)
    if remainder == 0:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes}m {remainder}s"


def wait_with_activity(seconds: float, message: str) -> None:
    delay = max(0.0, float(seconds))
    if delay <= 0:
        return
    _console_event(
        "WAIT",
        f"{message}; backing off for {_format_duration(delay)}.",
    )
    if _NETWORK_AUDIT is not None:
        _NETWORK_AUDIT.write(
            "network_wait",
            reason=message,
            seconds=delay,
        )
    if not (_ACTIVITY_SPINNER_ENABLED and sys.stderr.isatty()):
        time.sleep(delay)
        _console_event(
            "CONTINUE",
            "Cooldown complete; continuing plugin checks.",
        )
        if _NETWORK_AUDIT is not None:
            _NETWORK_AUDIT.write(
                "network_continue",
                reason=message,
            )
        return

    deadline = time.monotonic() + delay
    index = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        frame = ActivitySpinner.FRAMES[index % len(ActivitySpinner.FRAMES)]
        print(
            f"\r{frame} {message}; {remaining:.1f}s remaining",
            end="",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(min(0.12, remaining))
        index += 1
    print("\r\033[2K", end="", file=sys.stderr, flush=True)
    _console_event(
        "CONTINUE",
        "Cooldown complete; continuing plugin checks.",
    )
    if _NETWORK_AUDIT is not None:
        _NETWORK_AUDIT.write(
            "network_continue",
            reason=message,
        )


def rate_limit_delay(
    headers: Dict[str, str],
    *,
    attempt: int,
    fallback_backoff_s: int,
    max_backoff_s: int,
) -> int:
    retry_after = _retry_after_seconds(headers)
    if retry_after is None:
        delay = int(
            min(
                max_backoff_s,
                fallback_backoff_s * (2 ** max(0, attempt - 1)),
            )
        )
    else:
        delay = retry_after
    if delay > max_backoff_s:
        raise RuntimeError(
            f"server requested a {delay}s rate-limit cooldown; "
            f"aborting because configured maximum_backoff_seconds="
            f"{max_backoff_s}"
        )
    return max(0, delay)


def is_rate_limit_failure(error: Exception) -> bool:
    text = str(error or "").casefold()
    return "http 429" in text or "rate-limit" in text or "rate limit" in text


_LAST_NETWORK_REQUEST_STARTED = 0.0


def pace_network_request(
    minimum_interval_s: float,
    maximum_interval_s: Optional[float] = None,
) -> float:
    """Sleep as needed to put a fresh random interval between HTTP requests."""
    global _LAST_NETWORK_REQUEST_STARTED

    minimum = max(0.0, float(minimum_interval_s))
    maximum = minimum if maximum_interval_s is None else max(
        0.0,
        float(maximum_interval_s),
    )
    if maximum < minimum:
        raise ValueError(
            "maximum request interval must be greater than or equal to "
            "minimum request interval"
        )

    target_interval = random.uniform(minimum, maximum)
    now = time.monotonic()
    elapsed = now - _LAST_NETWORK_REQUEST_STARTED
    wait = max(0.0, target_interval - elapsed)
    if _LAST_NETWORK_REQUEST_STARTED > 0.0 and wait > 0.0:
        time.sleep(wait)
    _LAST_NETWORK_REQUEST_STARTED = time.monotonic()
    return wait


def http_get_json(
    url: str,
    *,
    timeout_s: int,
    min_interval_s: float,
    max_retries: int,
    debug_headers: bool,
    fallback_backoff_s: int = 30,
    max_backoff_s: int = 300,
    max_interval_s: Optional[float] = None,
) -> HttpResult:
    pace_network_request(min_interval_s, max_interval_s)

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    attempt = 0
    while True:
        attempt += 1
        try:
            label = Path(urlparse(url).path).name or "uMod metadata"
            with ActivitySpinner(f"Fetching {label}"):
                with urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read()
                    hdrs = _headers_dict(resp.headers)
            if debug_headers:
                rl = {k: hdrs.get(k, "") for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After", "X-Retry-After")}
                print(f"DEBUG headers: {rl}", file=sys.stderr)
            obj = json.loads(raw.decode("utf-8", errors="replace") or "{}")

            # If server says remaining=0 and provides retry-after, we can be polite before next call
            try:
                rem = int(hdrs.get("X-RateLimit-Remaining", "999999"))
            except Exception:
                rem = 999999
            ra = _retry_after_seconds(hdrs)
            if rem == 0 and ra:
                delay = rate_limit_delay(
                    hdrs,
                    attempt=attempt,
                    fallback_backoff_s=fallback_backoff_s,
                    max_backoff_s=max_backoff_s,
                )
                wait_with_activity(
                    delay,
                    "Remote reports its rate-limit quota is exhausted",
                )

            return HttpResult(data=obj, headers=hdrs)

        except HTTPError as e:
            hdrs = _headers_dict(e.headers)
            if e.code == 429:
                delay = rate_limit_delay(
                    hdrs,
                    attempt=attempt,
                    fallback_backoff_s=fallback_backoff_s,
                    max_backoff_s=max_backoff_s,
                )
                if attempt <= max_retries:
                    wait_with_activity(
                        delay,
                        "Got HTTP 429 Too Many Requests",
                    )
                    continue
                raise RuntimeError(
                    f"HTTP 429 rate-limited (cooldown={delay}s) "
                    f"after {max_retries} retries"
                )
            if e.code == 404:
                raise FileNotFoundError("404 not found")
            raise RuntimeError(f"HTTPError {e.code}: {e.reason}")

        except URLError as e:
            if attempt <= max_retries:
                wait_with_activity(
                    1.0,
                    f"Got a transient network error: {e}",
                )
                continue
            raise RuntimeError(f"URLError: {e}")

        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSONDecodeError: {e}")


def http_get_bytes(
    url: str,
    *,
    timeout_s: int,
    min_interval_s: float,
    max_retries: int,
    debug_headers: bool,
    max_bytes: int = MAX_PLUGIN_BYTES,
    fallback_backoff_s: int = 30,
    max_backoff_s: int = 300,
    max_interval_s: Optional[float] = None,
) -> HttpBytesResult:
    """Download a bounded response body with the checker's normal retry policy."""
    pace_network_request(min_interval_s, max_interval_s)

    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain, text/x-csharp, application/octet-stream",
        },
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            label = Path(urlparse(url).path).name or "plugin source"
            with ActivitySpinner(f"Downloading {label}"):
                with urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read(max_bytes + 1)
                    hdrs = _headers_dict(resp.headers)
                    final_url = str(resp.geturl())
            if len(raw) > max_bytes:
                raise RuntimeError(f"download exceeds {max_bytes} bytes")
            if debug_headers:
                rl = {
                    k: hdrs.get(k, "")
                    for k in (
                        "Content-Type",
                        "Content-Length",
                        "X-RateLimit-Limit",
                        "X-RateLimit-Remaining",
                        "Retry-After",
                        "X-Retry-After",
                    )
                }
                print(f"DEBUG download headers: {rl}", file=sys.stderr)
            return HttpBytesResult(data=raw, headers=hdrs, final_url=final_url)

        except HTTPError as e:
            hdrs = _headers_dict(e.headers)
            if e.code == 429:
                delay = rate_limit_delay(
                    hdrs,
                    attempt=attempt,
                    fallback_backoff_s=fallback_backoff_s,
                    max_backoff_s=max_backoff_s,
                )
                if attempt <= max_retries:
                    wait_with_activity(
                        delay,
                        "Got HTTP 429 Too Many Requests",
                    )
                    continue
                raise RuntimeError(
                    f"HTTP 429 rate-limited (cooldown={delay}s) "
                    f"after {max_retries} retries"
                )
            if e.code == 404:
                raise FileNotFoundError("404 not found")
            raise RuntimeError(f"HTTPError {e.code}: {e.reason}")

        except URLError as e:
            if attempt <= max_retries:
                wait_with_activity(
                    1.0,
                    f"Got a transient network error: {e}",
                )
                continue
            raise RuntimeError(f"URLError: {e}")

# ------------------------------------------------------------
# Matching / URLs
# ------------------------------------------------------------
def stem_noext(filename: str) -> str:
    return Path(filename).stem

def umod_direct_json_url(stem: str) -> str:
    return UMOD_PLUGIN_JSON.format(name=stem)

def umod_search_url(query: str) -> str:
    params = [
        ("query", query),
        ("page", "1"),
        ("sort", "title"),
        ("sortdir", "asc"),
        ("filter", ""),
        ("categories[]", "rust"),
    ]
    return f"{UMOD_SEARCH_JSON}?{urlencode(params)}"


def umod_download_url(filename: str, plugin_data: Dict[str, Any]) -> str:
    """Return a constrained official uMod source URL for a local plugin filename."""
    raw = str(plugin_data.get("download_url") or "").strip()
    if raw:
        url = urljoin("https://umod.org/", raw)
    else:
        url = UMOD_PLUGIN_DOWNLOAD.format(filename=quote(filename, safe="._-"))

    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "umod.org",
        "www.umod.org",
    }:
        raise ValueError(f"untrusted uMod download URL: {url}")
    if Path(parsed.path).name.lower() != filename.lower():
        raise ValueError(
            f"download filename mismatch: expected {filename}, got {Path(parsed.path).name or '-'}"
        )
    return url

def best_match_from_search(local_filename: str, search_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = search_data.get("data") if isinstance(search_data, dict) else None
    if not isinstance(items, list):
        return None

    fn = local_filename.lower()
    stem = stem_noext(local_filename).lower()

    best = None
    best_score = -1

    for it in items:
        if not isinstance(it, dict):
            continue
        score = 0
        dl = str(it.get("download_url") or "")
        dl_base = Path(dl).name.lower() if dl else ""
        title = str(it.get("title") or "").lower()
        name = str(it.get("name") or "").lower()

        if dl_base == fn:
            score += 100
        if title.replace(" ", "") == stem.replace(" ", ""):
            score += 30
        if name.replace(" ", "") == stem.replace(" ", ""):
            score += 25

        if score > best_score:
            best_score = score
            best = it

    return best if (best and best_score >= 25) else None

# ------------------------------------------------------------
# ChaosCode manifest loader (cached)
# ------------------------------------------------------------
def load_chaos_manifest(
    cache: Dict[str, Any],
    cache_path: Path,
    *,
    ttl_s: int,
    timeout_s: int,
    debug_headers: bool,
    fallback_backoff_s: int = 30,
    max_backoff_s: int = 300,
    min_interval_s: float = 0.0,
    max_interval_s: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns mapping: stem_lower -> resource dict
    Resource fields (typical):
      ResourceTitle, ResourceVersion, ResourceFile, ResourceURL, AuthorName
    """
    key = "chaos:manifest"
    now = int(time.time())

    ent = cache.get(key)
    if isinstance(ent, dict):
        ts = int(ent.get("ts", 0) or 0)
        if ts and (now - ts) <= int(ttl_s) and "data" in ent and isinstance(ent["data"], list):
            manifest_list = ent["data"]
            return _index_chaos_manifest(manifest_list)

    # Fetch fresh
    res = http_get_json(
        CHAOS_MANIFEST_JSON,
        timeout_s=int(timeout_s),
        min_interval_s=min_interval_s,
        max_retries=3,
        debug_headers=bool(debug_headers),
        fallback_backoff_s=fallback_backoff_s,
        max_backoff_s=max_backoff_s,
        max_interval_s=max_interval_s,
    )
    manifest_list = res.data
    if not isinstance(manifest_list, list):
        raise RuntimeError("Chaos manifest: unexpected JSON shape (expected list)")

    cache[key] = {"ts": now, "data": manifest_list}
    save_cache(cache_path, cache)

    return _index_chaos_manifest(manifest_list)

def _index_chaos_manifest(manifest_list: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for it in manifest_list:
        if not isinstance(it, dict):
            continue
        rf = str(it.get("ResourceFile") or "").strip()
        if not rf:
            continue
        stem = rf.split(".", 1)[0].strip().lower()
        if not stem:
            continue
        # last-one-wins; fine for our use
        out[stem] = it
    return out

# ------------------------------------------------------------
# ANSI colors (toggle)
# ------------------------------------------------------------
ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
    "cyan": "\x1b[36m",
}

def want_color(mode: str) -> bool:
    mode = (mode or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    return sys.stdout.isatty()

def color_status(s: str, *, use: bool) -> str:
    if not use:
        return s
    if s == "OK":
        return f"{ANSI['green']}{s}{ANSI['reset']}"
    if s == "OUTDATED":
        return f"{ANSI['yellow']}{s}{ANSI['reset']}"
    if s.startswith("ERROR") or s.startswith("UNKNOWN"):
        return f"{ANSI['red']}{s}{ANSI['reset']}"
    return s


def color_text(s: str, color: str, *, use: bool, bold: bool = False) -> str:
    if not use:
        return s
    prefix = ANSI["bold"] if bold else ""
    return f"{prefix}{ANSI[color]}{s}{ANSI['reset']}"

# ------------------------------------------------------------
# Output table
# ------------------------------------------------------------
def print_table(rows: List[Dict[str, Any]], *, use_color: bool) -> None:
    cols = ["filename", "source", "local", "remote", "status", "remote_url"]
    labels = {"remote_url": "remote url"}
    widths = {c: len(labels.get(c, c)) for c in cols}

    def s(v: Any) -> str:
        return "-" if v is None else str(v)

    # compute widths without ANSI
    for r in rows:
        for c in cols:
            vv = s(r.get(c))
            if c == "remote_url" and len(vv) > 110:
                vv = vv[:107] + "..."
            widths[c] = min(max(widths[c], len(vv)), 120)

    header = "  ".join(labels.get(c, c).ljust(widths[c]) for c in cols)
    print(terminal_rule())
    print(header)
    print(terminal_rule())

    for r in rows:
        parts = []
        for c in cols:
            vv = s(r.get(c))
            if c == "status":
                vv = color_status(vv, use=use_color)
            if c == "remote_url":
                raw = s(r.get(c))
                if len(raw) > 110:
                    raw = raw[:107] + "..."
                vv = raw  # keep urls plain
            parts.append(vv)

        # pad all but status (ANSI would break padding)
        line = []
        for i, c in enumerate(cols):
            if c == "status":
                line.append(parts[i])
            else:
                line.append(parts[i].ljust(widths[c]))
        print("  ".join(line))


def terminal_rule() -> str:
    """Return a literal hyphen rule spanning the current terminal width."""
    return "-" * max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)


def update_command_hint(
    plugins_dir: Path,
    *,
    recursive: bool,
    config_path: Path = CONFIG_FILE_DEFAULT,
    no_config: bool = False,
) -> str:
    parts = ["python3", "tools/oxide_plugin_updater.py"]
    if no_config:
        parts.append("--no-config")
    elif config_path != CONFIG_FILE_DEFAULT:
        parts.extend(["--config", str(config_path)])
    if plugins_dir != DEFAULT_PLUGINS_DIR:
        parts.append(str(plugins_dir))
    if recursive:
        parts.append("--recursive")
    parts.append("--update")
    return " ".join(shlex.quote(part) for part in parts)


def set_plugins_directory_command_hint(
    config_path: Path,
    *,
    placeholder: str = "/path/to/oxide/plugins",
) -> str:
    parts = ["python3", "tools/oxide_plugin_updater.py"]
    if config_path != CONFIG_FILE_DEFAULT:
        parts.extend(["--config", str(config_path)])
    parts.extend(["--set-plugins-directory", placeholder])
    return " ".join(shlex.quote(part) for part in parts)


def print_missing_plugins_directory_help(
    plugins_dir: Path,
    *,
    config_path: Path,
    no_config: bool,
) -> None:
    print(
        f"ERROR: Oxide plugin directory not found: {plugins_dir}",
        file=sys.stderr,
    )
    print(
        "Set the path permanently in the updater configuration:",
        file=sys.stderr,
    )
    print(
        "  "
        + set_plugins_directory_command_hint(config_path),
        file=sys.stderr,
    )
    print(
        "Or use a one-run directory override without changing the config:",
        file=sys.stderr,
    )
    print(
        "  python3 tools/oxide_plugin_updater.py "
        "/path/to/oxide/plugins",
        file=sys.stderr,
    )
    if no_config:
        print(
            "NOTE: --no-config ignores persisted settings; omit it after "
            "saving the directory.",
            file=sys.stderr,
        )


def print_check_summary(
    candidates: List[UpdateCandidate],
    *,
    manual_outdated: int,
    plugins_dir: Path,
    recursive: bool,
    use_color: bool,
    config_path: Path = CONFIG_FILE_DEFAULT,
    no_config: bool = False,
    history_counts: Optional[Dict[str, int]] = None,
    known_unconfirmed: Optional[List[str]] = None,
) -> None:
    print()
    print(terminal_rule())
    if candidates:
        heading = f"Plugins that can be auto-updated ({len(candidates)}):"
        print(color_text(heading, "yellow", use=use_color, bold=True))
        for candidate in candidates:
            arrow = color_text("->", "cyan", use=use_color)
            remote = color_text(candidate.remote_version, "yellow", use=use_color)
            print(
                f"  {candidate.filename}: "
                f"{candidate.local_version} {arrow} {remote}"
            )
    else:
        print(color_text("No plugins can be auto-updated.", "green", use=use_color, bold=True))
    print(terminal_rule())

    if candidates:
        command = update_command_hint(
            plugins_dir,
            recursive=recursive,
            config_path=config_path,
            no_config=no_config,
        )
        print(
            f"Found {len(candidates)} "
            f"{'plugin' if len(candidates) == 1 else 'plugins'} that can be auto-updated."
        )
        print(
            "Run "
            f"{color_text('Oxide Plugin Updater', 'cyan', use=use_color, bold=True)} "
            f"to update {'it' if len(candidates) == 1 else 'them'}:"
        )
        print(f"  {color_text(command, 'cyan', use=use_color)}")
    if manual_outdated:
        print(
            f"{manual_outdated} additional outdated "
            f"{'plugin requires' if manual_outdated == 1 else 'plugins require'} "
            "a manual update (for example, a ChaosCode/paid plugin)."
        )
    if history_counts and sum(history_counts.values()):
        print(
            "Outdated history: "
            f"{history_counts.get('new', 0)} newly detected, "
            f"{history_counts.get('known', 0)} previously known, "
            f"{history_counts.get('changed', 0)} changed since the prior check."
        )
    if known_unconfirmed:
        print(
            "Previously known outdated but not refreshed during this check "
            f"({len(known_unconfirmed)}):"
        )
        for filename in known_unconfirmed:
            print(f"  {filename}")


def _validate_umod_source_url(url: str, filename: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "umod.org",
        "www.umod.org",
    }:
        return f"download redirected to an untrusted URL: {url}"
    received_filename = Path(parsed.path).name
    if received_filename.lower() != filename.lower():
        return (
            f"download filename mismatch: expected {filename}, "
            f"got {received_filename or '-'}"
        )
    return None


def _header_value(headers: Dict[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def _normalized_plugin_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def path_is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate_plugin_download(
    candidate: UpdateCandidate,
    installed: bytes,
    downloaded: bytes,
    *,
    headers: Dict[str, str],
    final_url: str,
    allow_large_shrink: bool,
    shrink_warn_percent: float = SHRINK_WARN_PERCENT,
    shrink_refuse_percent: float = SHRINK_REFUSE_PERCENT,
    force_reinstall: bool = False,
) -> DownloadValidation:
    """Validate transport, identity, metadata, version, and basic C# structure."""
    errors: List[str] = []
    warnings: List[str] = []
    old_size = len(installed)
    new_size = len(downloaded)
    old_sha256 = hashlib.sha256(installed).hexdigest()
    new_sha256 = hashlib.sha256(downloaded).hexdigest()

    redirect_error = _validate_umod_source_url(final_url, candidate.filename)
    if redirect_error:
        errors.append(redirect_error)

    content_type = _header_value(headers, "Content-Type").lower()
    if any(marker in content_type for marker in ("text/html", "xml", "json")):
        errors.append(f"unexpected Content-Type: {content_type}")
    if not downloaded:
        errors.append("download is empty")
    elif len(downloaded) < 200:
        errors.append(f"download is implausibly small ({new_size} bytes)")
    if b"\x00" in downloaded:
        errors.append("download contains NUL bytes and does not look like text source")

    text = ""
    try:
        text = downloaded.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        errors.append(f"download is not valid UTF-8 C# source: {e}")

    stripped = text.lstrip().lower()
    if stripped.startswith(("<!doctype html", "<html", "<?xml")):
        errors.append("download is an HTML/XML response, not C# source")

    candidate_name = ""
    candidate_author = ""
    candidate_version = ""
    if text:
        name, author, version = extract_plugin_info(text)
        candidate_name = name or ""
        candidate_author = author or ""
        candidate_version = version or ""

        if not candidate_name or not candidate_version:
            errors.append("downloaded source has no recognizable [Info(...)] metadata")
        elif _normalized_plugin_name(candidate_name) != _normalized_plugin_name(candidate.name):
            errors.append(
                f"plugin identity mismatch: installed {candidate.name!r}, "
                f"downloaded {candidate_name!r}"
            )

        if candidate_version != candidate.remote_version:
            errors.append(
                f"version mismatch: uMod reports {candidate.remote_version}, "
                f"download contains {candidate_version or '-'}"
            )
        definitely_newer = (
            version_is_newer(candidate_version, candidate.local_version)
            is True
        )
        if force_reinstall:
            if (
                candidate_version != candidate.local_version
                and not definitely_newer
            ):
                errors.append(
                    f"downloaded version {candidate_version or '-'} is neither "
                    f"the installed version {candidate.local_version} nor "
                    "definitely newer"
                )
        elif not definitely_newer:
            errors.append(
                f"downloaded version {candidate_version or '-'} is not "
                f"definitely newer than installed version "
                f"{candidate.local_version}"
            )

        if not re.search(r"\bnamespace\s+Oxide\.Plugins\b", text):
            errors.append("downloaded source has no Oxide.Plugins namespace")
        expected_class = re.escape(Path(candidate.filename).stem)
        if not re.search(rf"\bclass\s+{expected_class}\b", text):
            errors.append(
                f"downloaded source has no expected class {Path(candidate.filename).stem}"
            )

    if old_size > 0 and new_size < old_size:
        shrink_percent = ((old_size - new_size) / old_size) * 100.0
        if shrink_percent >= shrink_warn_percent:
            warnings.append(
                f"candidate is {shrink_percent:.1f}% smaller "
                f"({old_size} -> {new_size} bytes)"
            )
        if shrink_percent >= shrink_refuse_percent and not allow_large_shrink:
            errors.append(
                f"candidate is {shrink_percent:.1f}% smaller; "
                "review it and use --allow-large-shrink only if intentional"
            )

    if installed == downloaded and not force_reinstall:
        errors.append("download is byte-for-byte identical to the installed file")

    return DownloadValidation(
        errors=errors,
        warnings=warnings,
        candidate_name=candidate_name,
        candidate_author=candidate_author,
        candidate_version=candidate_version,
        old_size=old_size,
        new_size=new_size,
        old_sha256=old_sha256,
        new_sha256=new_sha256,
    )


def install_update(
    candidate: UpdateCandidate,
    *,
    plugins_dir: Path,
    backup_root: Path,
    timeout_s: int,
    min_interval_s: float,
    max_retries: int,
    debug_headers: bool,
    allow_large_shrink: bool,
    use_color: bool,
    audit: Optional[AuditLogger] = None,
    max_plugin_bytes: int = MAX_PLUGIN_BYTES,
    shrink_warn_percent: float = SHRINK_WARN_PERCENT,
    shrink_refuse_percent: float = SHRINK_REFUSE_PERCENT,
    fallback_backoff_s: int = 30,
    max_backoff_s: int = 300,
    package_state: Optional[Dict[str, Any]] = None,
    state_history_limit: int = 50,
    max_interval_s: Optional[float] = None,
    force_reinstall: bool = False,
) -> InstallResult:
    print(
        color_text(
            f"{candidate.filename}: {candidate.local_version} -> {candidate.remote_version}",
            "yellow",
            use=use_color,
            bold=True,
        )
    )

    target = candidate.path
    try:
        if target.is_symlink():
            raise RuntimeError("installed plugin is a symbolic link")
        if not target.is_file():
            raise RuntimeError("installed plugin is not a regular file")
        relative = target.resolve().relative_to(plugins_dir.resolve())
        installed = target.read_bytes()
        installed_sha256 = hashlib.sha256(installed).hexdigest()
        if (
            candidate.local_sha256
            and installed_sha256 != candidate.local_sha256
        ):
            raise RuntimeError(
                "installed plugin changed after the inventory scan "
                f"(expected SHA-256 {candidate.local_sha256}, "
                f"got {installed_sha256})"
            )
        installed_text = installed.decode("utf-8-sig")
        installed_name, _installed_author, installed_version = extract_plugin_info(
            installed_text
        )
        if (
            _normalized_plugin_name(installed_name or "")
            != _normalized_plugin_name(candidate.name)
        ):
            raise RuntimeError(
                "installed plugin identity changed after the inventory scan"
            )
        if (installed_version or "") != candidate.local_version:
            raise RuntimeError(
                "installed plugin version changed after the inventory scan "
                f"({candidate.local_version} -> {installed_version or '-'})"
            )
    except Exception as e:
        print(color_text(f"  REFUSED: {e}", "red", use=use_color))
        if audit:
            audit.write(
                "plugin_update",
                plugin=candidate.filename,
                source="umod",
                installed_path=str(candidate.path),
                local_version=candidate.local_version,
                remote_version=candidate.remote_version,
                result="refused",
                reason=str(e),
            )
        return InstallResult(False)

    try:
        response = http_get_bytes(
            candidate.download_url,
            timeout_s=timeout_s,
            min_interval_s=min_interval_s,
            max_retries=max_retries,
            debug_headers=debug_headers,
            max_bytes=max_plugin_bytes,
            fallback_backoff_s=fallback_backoff_s,
            max_backoff_s=max_backoff_s,
            max_interval_s=max_interval_s,
        )
    except Exception as e:
        print(color_text(f"  DOWNLOAD FAILED: {e}", "red", use=use_color))
        if audit:
            audit.write(
                "plugin_update",
                plugin=candidate.filename,
                source="umod",
                installed_path=str(candidate.path),
                local_version=candidate.local_version,
                remote_version=candidate.remote_version,
                result="download_failed",
                reason=str(e),
            )
        return InstallResult(False)

    validation = validate_plugin_download(
        candidate,
        installed,
        response.data,
        headers=response.headers,
        final_url=response.final_url,
        allow_large_shrink=allow_large_shrink,
        shrink_warn_percent=shrink_warn_percent,
        shrink_refuse_percent=shrink_refuse_percent,
        force_reinstall=force_reinstall,
    )
    size_delta = validation.new_size - validation.old_size
    size_percent = (
        (size_delta / validation.old_size) * 100.0
        if validation.old_size
        else 0.0
    )
    print(
        f"  Size: {validation.old_size} -> {validation.new_size} bytes "
        f"({size_percent:+.1f}%)"
    )
    print(f"  SHA-256 old: {validation.old_sha256}")
    print(f"  SHA-256 new: {validation.new_sha256}")
    for warning in validation.warnings:
        print(color_text(f"  WARNING: {warning}", "yellow", use=use_color))
    if validation.errors:
        for error in validation.errors:
            print(color_text(f"  REFUSED: {error}", "red", use=use_color))
        if audit:
            audit.write(
                "plugin_update",
                plugin=candidate.filename,
                source="umod",
                installed_path=str(candidate.path),
                local_version=candidate.local_version,
                remote_version=candidate.remote_version,
                result="validation_refused",
                errors=validation.errors,
                warnings=validation.warnings,
                old_size=validation.old_size,
                new_size=validation.new_size,
                old_sha256=validation.old_sha256,
                new_sha256=validation.new_sha256,
                download_url=response.final_url,
            )
        return InstallResult(False)
    print(
        color_text(
            "  Source validation: OK (Oxide compile not tested)",
            "green",
            use=use_color,
        )
    )

    if installed == response.data:
        print(
            color_text(
                "  Installed source is already byte-for-byte identical; "
                "leaving the file unchanged.",
                "green",
                use=use_color,
            )
        )
        if package_state is not None:
            record_revalidated_source(
                package_state,
                key=relative.as_posix(),
                candidate=candidate,
                validation=validation,
                download_url=response.final_url,
                history_limit=state_history_limit,
            )
        if audit:
            audit.write(
                "plugin_update",
                plugin=candidate.filename,
                source="umod",
                installed_path=str(candidate.path),
                local_version=candidate.local_version,
                remote_version=candidate.remote_version,
                result="identical_source_revalidated",
                old_size=validation.old_size,
                new_size=validation.new_size,
                old_sha256=validation.old_sha256,
                new_sha256=validation.new_sha256,
                download_url=response.final_url,
            )
        return InstallResult(True, source_changed=False)

    safe_plugin = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(relative).stem)
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate.local_version)
    backup_filename = (
        f"{Path(relative).stem}-{validation.old_sha256[:12]}{Path(relative).suffix}"
    )
    backup_path = backup_root / safe_plugin / safe_version / backup_filename
    tmp_path = target.with_name(
        f".{target.name}.update-{os.getpid()}-{time.time_ns()}.tmp"
    )
    try:
        current_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        if current_sha256 != validation.old_sha256:
            raise RuntimeError("installed plugin changed while its update was downloading")

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if not backup_path.exists():
            shutil.copy2(target, backup_path)
        backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        if backup_sha256 != validation.old_sha256:
            raise RuntimeError(
                f"backup verification failed: expected {validation.old_sha256}, "
                f"got {backup_sha256}"
            )

        mode = target.stat().st_mode & 0o777
        with tmp_path.open("xb") as handle:
            handle.write(response.data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except Exception as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        print(color_text(f"  INSTALL FAILED: {e}", "red", use=use_color))
        if audit:
            audit.write(
                "plugin_update",
                plugin=candidate.filename,
                source="umod",
                installed_path=str(candidate.path),
                local_version=candidate.local_version,
                remote_version=candidate.remote_version,
                result="install_failed",
                reason=str(e),
                backup=str(backup_path),
                old_sha256=validation.old_sha256,
                new_sha256=validation.new_sha256,
            )
        return InstallResult(False)

    print(f"  Backup: {backup_path}")
    print(color_text("  Updated: YES", "green", use=use_color, bold=True))
    if package_state is not None:
        record_installed_update(
            package_state,
            key=relative.as_posix(),
            candidate=candidate,
            validation=validation,
            backup_path=backup_path,
            history_limit=state_history_limit,
        )
    if audit:
        audit.write(
            "plugin_update",
            plugin=candidate.filename,
            source="umod",
            installed_path=str(candidate.path),
            local_version=candidate.local_version,
            remote_version=candidate.remote_version,
            result="updated",
            backup=str(backup_path),
            old_size=validation.old_size,
            new_size=validation.new_size,
            old_sha256=validation.old_sha256,
            new_sha256=validation.new_sha256,
            warnings=validation.warnings,
            download_url=response.final_url,
        )
    return InstallResult(True, source_changed=True)


def _rcon_response_text(watchdog: Any, response: Any) -> str:
    extractor = getattr(watchdog, "rcon_extract_message", None)
    if callable(extractor):
        return str(extractor(str(response or "")) or "").strip()
    return str(response or "").strip()


def _reload_compile_failure(text: str) -> str:
    """Return the useful compiler-error line, or an empty string."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    for line in lines:
        if re.search(r"\bfailed to compile\b", line, re.IGNORECASE):
            return line
    for line in lines:
        if re.search(
            r"\b(?:compiler error|compilation failed)\b",
            line,
            re.IGNORECASE,
        ):
            return line
    return ""


def _inventory_lines_for_plugin(
    inventory_text: str,
    filename: str,
) -> List[str]:
    """Find only exact Oxide inventory lines for one plugin filename."""
    plugin_filename = Path(filename).name
    plugin_name = Path(plugin_filename).stem
    filename_pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-]){re.escape(plugin_filename)}"
        rf"(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    failed_pattern = re.compile(
        rf"^\s*(?:\d+\s+)?{re.escape(plugin_name)}(?:\.cs)?"
        rf"\s+-\s+failed to compile\b",
        re.IGNORECASE,
    )
    return [
        line
        for line in str(inventory_text or "").splitlines()
        if filename_pattern.search(line) or failed_pattern.search(line)
    ]


def _inventory_line_has_version(line: str, expected_version: str) -> bool:
    """Match an exact inventory version token, not a numeric substring."""
    official = re.match(
        r'^\s*\d+\s+(?:"[^"]*"|\S+)\s+\(([^()]+)\)',
        line,
    )
    if official:
        return official.group(1).strip().casefold() == expected_version.casefold()
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_.-]){re.escape(expected_version)}"
            rf"(?![A-Za-z0-9_.-])",
            line,
            re.IGNORECASE,
        )
    )


def reload_updated_plugins(
    rcon_config: Dict[str, Any],
    plugins: Optional[List[Tuple[str, str]]] = None,
    *,
    progress: Optional[Any] = None,
    activation_records: Optional[List[Dict[str, str]]] = None,
) -> Tuple[bool, str]:
    """Activate updated plugins individually and verify the resulting inventory."""
    password = str(rcon_config.get("password") or "")
    password_environment_variable = str(
        rcon_config.get("password_environment_variable") or ""
    ).strip()
    if not password and password_environment_variable:
        password = os.environ.get(password_environment_variable, "")

    try:
        port = int(rcon_config.get("port", 0))
    except (TypeError, ValueError):
        return False, "RCON port must be an integer"

    project_root = str(HERE.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        watchdog = importlib.import_module("rust_watchdog")
    except Exception as e:
        return False, f"cannot load watchdog WebRCON client: {e}"

    cfg = {
        "identity": str(rcon_config.get("identity") or "").strip(),
        "rcon_host": str(rcon_config.get("host") or "").strip(),
        "rcon_port": port,
        "rcon_password": password,
    }
    requested = list(plugins or [])
    if not requested:
        return False, "no plugins were supplied for activation"

    try:
        inventory_ok, inventory_response = watchdog.rcon_send(
            cfg,
            "oxide.plugins",
        )
        inventory_before = _rcon_response_text(watchdog, inventory_response)
    except Exception as e:
        inventory_ok = False
        inventory_before = f"oxide.plugins raised an exception: {e}"
    if not inventory_ok:
        return False, (
            "cannot determine current plugin state before activation: "
            + (inventory_before or "no response")
        )

    failures: List[str] = []
    activation_failed = set()

    def update_activation_record(
        filename: str,
        status: str,
        detail: str,
    ) -> None:
        if activation_records is None:
            return
        for record in reversed(activation_records):
            if record.get("plugin") == filename:
                record["status"] = status
                record["response"] = detail
                return

    for index, (filename, _expected_version) in enumerate(requested, start=1):
        plugin_name = Path(filename).stem
        matching_lines = _inventory_lines_for_plugin(
            inventory_before,
            filename,
        )
        currently_loaded = bool(matching_lines) and not any(
            re.search(r"\bfailed to compile\b", line, re.IGNORECASE)
            for line in matching_lines
        )
        action = "reload" if currently_loaded else "load"
        command = f"oxide.{action} {plugin_name}"
        try:
            ok, response = watchdog.rcon_send(cfg, command)
        except Exception as e:
            ok, response = False, f"RCON {action} raised an exception: {e}"
        response_text = _rcon_response_text(watchdog, response)
        compile_failure = _reload_compile_failure(response_text)
        if not ok:
            status = "RCON FAILED"
            detail = response_text or "no response"
            failures.append(f"{filename}: {detail}")
            activation_failed.add(filename)
        elif compile_failure:
            status = "FAILED TO COMPILE"
            detail = compile_failure
            failures.append(f"{filename}: {compile_failure}")
            activation_failed.add(filename)
        else:
            status = "OK"
            detail = response_text
        if activation_records is not None:
            activation_records.append(
                {
                    "plugin": filename,
                    "command": command,
                    "status": status,
                    "response": detail,
                }
            )
        if progress:
            progress(index, len(requested), filename, status, detail)

    inventory_ok = False
    inventory_text = ""
    try:
        inventory_ok, inventory_response = watchdog.rcon_send(
            cfg,
            "oxide.plugins",
        )
        inventory_text = _rcon_response_text(
            watchdog,
            inventory_response,
        )
    except Exception as e:
        inventory_text = f"oxide.plugins raised an exception: {e}"

    if not inventory_ok:
        detail = (
            "final oxide.plugins verification failed: "
            + (inventory_text or "no response")
        )
        failures.append(detail)
        for filename, _expected_version in requested:
            if filename not in activation_failed:
                update_activation_record(filename, "VERIFY FAILED", detail)
    else:
        for filename, expected_version in requested:
            if filename in activation_failed:
                continue
            matching_lines = _inventory_lines_for_plugin(
                inventory_text,
                filename,
            )
            if not matching_lines:
                detail = (
                    f"{filename}: not listed by oxide.plugins after activation"
                )
                failures.append(detail)
                update_activation_record(filename, "VERIFY FAILED", detail)
            elif any(
                re.search(
                    r"\bfailed to compile\b",
                    line,
                    re.IGNORECASE,
                )
                for line in matching_lines
            ):
                detail = next(
                    line.strip()
                    for line in matching_lines
                    if re.search(
                        r"\bfailed to compile\b",
                        line,
                        re.IGNORECASE,
                    )
                )
                failures.append(f"{filename}: {detail}")
                update_activation_record(
                    filename,
                    "FAILED TO COMPILE",
                    detail,
                )
            elif (
                expected_version
                and expected_version != "-"
                and not any(
                    _inventory_line_has_version(line, expected_version)
                    for line in matching_lines
                )
            ):
                detail = (
                    f"{filename}: expected version {expected_version} "
                    "not found in oxide.plugins"
                )
                failures.append(detail)
                update_activation_record(filename, "VERIFY FAILED", detail)

    if failures:
        return False, "\n".join(failures)
    return True, (
        f"{len(requested)} plugin"
        f"{'' if len(requested) == 1 else 's'} activated and verified"
    )


# ------------------------------------------------------------
# Main logic
# ------------------------------------------------------------
def main(
    argv: Optional[List[str]] = None,
    *,
    legacy_check_only: bool = False,
) -> int:
    global _ACTIVITY_SPINNER_ENABLED, _NETWORK_AUDIT
    argv = list(sys.argv[1:] if argv is None else argv)

    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--config", default=str(CONFIG_FILE_DEFAULT))
    bootstrap.add_argument("--no-config", action="store_true")
    bootstrap.add_argument(
        "--set-plugins-directory",
        "--set-plugins-dir",
        dest="set_plugins_directory",
    )
    bootstrap_args, _unknown = bootstrap.parse_known_args(argv)
    config_path = Path(bootstrap_args.config).expanduser().resolve()

    config = copy.deepcopy(CONFIG_DEFAULTS)
    if not bootstrap_args.no_config:
        try:
            config = load_updater_config(config_path)
        except FileNotFoundError:
            if (
                _explicit_option(argv, "--config")
                and bootstrap_args.set_plugins_directory is None
            ):
                print(
                    f"Configuration file not found: {config_path}",
                    file=sys.stderr,
                )
                return 2
        except ValueError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 2
    try:
        validate_updater_config(config)
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    sources_config = _config_section(config, "sources")
    network_config = _config_section(config, "network")
    cache_config = _config_section(config, "cache")
    validation_config = _config_section(config, "validation")
    updates_config = _config_section(config, "updates")
    state_config = _config_section(config, "state")
    rcon_config = _config_section(config, "rcon")
    logging_config = _config_section(config, "logging")
    output_config = _config_section(config, "output")

    try:
        configured_plugins_dir = _resolve_config_path(
            config.get("plugins_directory"),
            config_path,
        )
        configured_cache_file = _resolve_config_path(
            cache_config.get("file"),
            config_path,
        )
        configured_backup_dir = _resolve_config_path(
            updates_config.get("backup_directory"),
            config_path,
        )
        configured_state_file = _resolve_config_path(
            state_config.get("file"),
            config_path,
        )
        configured_log_file = _resolve_config_path(
            logging_config.get("file"),
            config_path,
        )
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(
        description=(
            "Check installed Rust Oxide/uMod plugins and report available "
            "updates. Installations are handled by oxide_plugin_updater.py."
            if legacy_check_only
            else
            "Oxide Plugin Updater: check, validate, archive, install, and "
            "optionally activate Rust Oxide/uMod plugins."
        )
    )
    ap.add_argument(
        "--config",
        default=str(config_path),
        help=f"updater JSON configuration (default: {CONFIG_FILE_DEFAULT})",
    )
    ap.add_argument(
        "--no-config",
        action="store_true",
        help="ignore the updater JSON and use built-in defaults plus CLI options",
    )
    ap.add_argument(
        "--view-config",
        "--viewconfig",
        dest="view_config",
        action="store_true",
        help=(
            "show the merged, resolved updater configuration and exit "
            "without scanning"
        ),
    )
    ap.add_argument(
        "--set-plugins-directory",
        "--set-plugins-dir",
        dest="set_plugins_directory",
        metavar="PATH",
        help=(
            argparse.SUPPRESS
            if legacy_check_only
            else
            "validate and persist plugins_directory in the updater JSON, "
            "then exit"
        ),
    )
    ap.add_argument(
        "plugins_dir",
        nargs="?",
        default=str(configured_plugins_dir),
        help=f"oxide/plugins directory (configured: {configured_plugins_dir})",
    )
    ap.add_argument(
        "--recursive",
        action="store_true",
        default=bool(config.get("recursive", False)),
        help="scan recursively for *.cs",
    )
    ap.add_argument(
        "--cache",
        default=str(configured_cache_file),
        help=f"cache file (configured: {configured_cache_file})",
    )
    ap.add_argument(
        "--cache-ttl",
        type=int,
        default=int(cache_config.get("umod_ttl_seconds", CACHE_TTL_SECONDS_DEFAULT)),
        help="uMod result validity time in seconds",
    )
    ap.add_argument(
        "--override-cache",
        action="store_true",
        help=(
            "ignore valid uMod and ChaosCode cache entries for this run, "
            "fetch fresh results, and refresh the cache"
        ),
    )

    # Chaos toggle (default: on)
    try:
        boolopt = argparse.BooleanOptionalAction  # py3.9+
        ap.add_argument(
            "--check-chaos",
            default=bool(sources_config.get("check_chaoscode", True)),
            action=boolopt,
            help="also check ChaosCode (fallback for uMod-unknown)",
        )
    except Exception:
        # fallback if somehow running on older python
        ap.add_argument(
            "--check-chaos",
            action="store_true",
            default=bool(sources_config.get("check_chaoscode", True)),
        )
        ap.add_argument("--no-check-chaos", action="store_true", default=False)

    ap.add_argument(
        "--chaos-cache-ttl",
        type=int,
        default=int(
            cache_config.get(
                "chaoscode_ttl_seconds",
                CHAOS_CACHE_TTL_SECONDS_DEFAULT,
            )
        ),
        help="Chaos manifest cache TTL seconds",
    )

    ap.add_argument(
        "--timeout",
        type=int,
        default=int(network_config.get("timeout_seconds", 12)),
        help="HTTP timeout seconds",
    )
    ap.add_argument(
        "--min-interval",
        type=float,
        default=None,
        help=(
            "minimum seconds between HTTP requests; by itself, preserves "
            "legacy fixed-interval behavior"
        ),
    )
    ap.add_argument(
        "--max-interval",
        type=float,
        default=None,
        help="maximum seconds between HTTP requests for randomized pacing",
    )
    ap.add_argument(
        "--max-retries",
        type=int,
        default=int(network_config.get("maximum_retries", 6)),
        help="max retries for transient errors / 429",
    )
    ap.add_argument("--outdated-only", action="store_true", help="only show outdated plugins")
    ap.add_argument(
        "--fallback-search",
        action="store_true",
        default=bool(sources_config.get("fallback_search", False)),
        help="if direct .json 404s, try search.json (slower; may rate-limit)",
    )
    ap.add_argument("--progress", action="store_true", help="show progress lines (default: on if TTY)")
    ap.add_argument("--no-progress", action="store_true", help="disable progress lines")
    ap.add_argument(
        "--color",
        default=str(output_config.get("color", "auto")),
        choices=["auto", "always", "never"],
        help="ANSI colors for status",
    )
    ap.add_argument("--debug-headers", action="store_true", help="print rate-limit headers to stderr")
    ap.add_argument("--json", dest="as_json", action="store_true", help="output JSON")
    ap.add_argument(
        "--update",
        action="store_true",
        help=(
            argparse.SUPPRESS
            if legacy_check_only
            else "validate, back up, and update every eligible outdated uMod plugin"
        ),
    )
    ap.add_argument(
        "--update-plugin",
        metavar="NAME",
        help=argparse.SUPPRESS if legacy_check_only else (
            "check and update exactly one installed plugin; accepts the "
            "filename with or without its .cs extension"
        ),
    )
    ap.add_argument(
        "--verify-plugin",
        metavar="NAME",
        help=argparse.SUPPRESS if legacy_check_only else (
            "compile/load-verify exactly one installed plugin without "
            "checking or downloading updates"
        ),
    )
    ap.add_argument(
        "--verify-all-plugins",
        "--verify-all",
        dest="verify_all_plugins",
        action="store_true",
        help=argparse.SUPPRESS if legacy_check_only else (
            "compile/load-verify every scanned plugin sequentially without "
            "using a wildcard command"
        ),
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help=argparse.SUPPRESS if legacy_check_only else (
            "with --update-plugin, bypass metadata cache, re-download the "
            "current upstream source, and force targeted activation even "
            "when the version or bytes are unchanged"
        ),
    )
    ap.add_argument(
        "--backup-dir",
        default=str(configured_backup_dir),
        help=argparse.SUPPRESS if legacy_check_only else (
            "archive root for replaced plugins "
            f"(configured: {configured_backup_dir})"
        ),
    )
    ap.add_argument(
        "--allow-large-shrink",
        action="store_true",
        help=argparse.SUPPRESS if legacy_check_only else (
            "allow an update beyond the configured shrink refusal threshold "
            "after all other validation passes"
        ),
    )
    try:
        ap.add_argument(
            "--reload-plugins-after-updates",
            default=bool(
                updates_config.get("reload_plugins_after_updates", True)
            ),
            action=argparse.BooleanOptionalAction,
            help=argparse.SUPPRESS if legacy_check_only else (
                "load or reload each processed plugin via RCON, detect compile "
                "failures, and verify oxide.plugins (configured default: enabled)"
            ),
        )
    except AttributeError:
        ap.add_argument(
            "--reload-plugins-after-updates",
            action="store_true",
            default=bool(
                updates_config.get("reload_plugins_after_updates", True)
            ),
        )
        ap.add_argument(
            "--no-reload-plugins-after-updates",
            action="store_true",
            default=False,
        )
    ap.add_argument(
        "--log-file",
        default=str(configured_log_file),
        help=f"append JSON audit records here (configured: {configured_log_file})",
    )
    ap.add_argument(
        "--no-log",
        action="store_true",
        help="do not write the check/update audit log",
    )
    ap.add_argument(
        "--state-file",
        default=str(configured_state_file),
        help=(
            "remember installed hashes and known-outdated history here "
            f"(configured: {configured_state_file})"
        ),
    )
    ap.add_argument(
        "--no-state",
        action="store_true",
        help="do not read or write persistent plugin package state",
    )
    args = ap.parse_args(argv)

    selected_modes = sum(
        (
            bool(args.update),
            bool(args.update_plugin),
            bool(args.verify_plugin),
            bool(args.verify_all_plugins),
        )
    )
    if selected_modes > 1:
        ap.error(
            "--update, --update-plugin, --verify-plugin, and "
            "--verify-all-plugins are mutually exclusive"
        )
    verify_mode = bool(args.verify_plugin or args.verify_all_plugins)
    if args.update_plugin:
        args.update = True
    if args.force and not args.update_plugin:
        ap.error("--force requires --update-plugin")
    if args.force:
        args.override_cache = True

    configured_min_interval = float(
        network_config.get("minimum_interval_seconds", 1.5)
    )
    configured_max_interval = float(
        network_config.get("maximum_interval_seconds", 3.0)
    )
    if args.min_interval is None and args.max_interval is None:
        args.min_interval = configured_min_interval
        args.max_interval = configured_max_interval
    elif args.min_interval is not None and args.max_interval is None:
        # Preserve the pre-range CLI meaning: --min-interval N alone is a
        # fixed N-second interval rather than an accidental N-to-config-max
        # range.
        args.max_interval = args.min_interval
    elif args.min_interval is None:
        args.min_interval = configured_min_interval
    if args.min_interval < 0:
        ap.error("--min-interval must be at least 0")
    if args.max_interval < args.min_interval:
        ap.error("--max-interval must be greater than or equal to --min-interval")

    _ACTIVITY_SPINNER_ENABLED = bool(
        network_config.get("show_activity_spinner", True)
    )

    if (
        hasattr(args, "no_reload_plugins_after_updates")
        and args.no_reload_plugins_after_updates
    ):
        args.reload_plugins_after_updates = False

    if args.set_plugins_directory is not None:
        if legacy_check_only:
            print(
                "umod_plugins_check.py is check-only. Configure paths with "
                "Oxide Plugin Updater:",
                file=sys.stderr,
            )
            print(
                "  "
                + set_plugins_directory_command_hint(
                    config_path,
                    placeholder=str(args.set_plugins_directory),
                ),
                file=sys.stderr,
            )
            return 2
        if args.no_config:
            ap.error(
                "--set-plugins-directory cannot be combined with --no-config"
            )
        if (
            args.update
            or args.update_plugin
            or verify_mode
            or args.as_json
            or args.view_config
        ):
            ap.error(
                "--set-plugins-directory cannot be combined with "
                "update/verify modes, --json, or --view-config"
            )
        try:
            result = set_plugins_directory_config(
                config_path,
                Path(args.set_plugins_directory),
            )
        except (OSError, ValueError) as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            return 2
        print_plugins_directory_config_result(result)
        return 0

    if args.view_config:
        if args.update or verify_mode:
            ap.error(
                "--view-config cannot be combined with update/verify modes"
            )
        print_effective_updater_config(
            config,
            config_path,
            no_config=bool(args.no_config),
        )
        return 0

    if args.as_json and (args.update or verify_mode):
        ap.error("--json cannot be combined with update/verify modes")
    if args.allow_large_shrink and not args.update:
        ap.error("--allow-large-shrink requires --update")
    if legacy_check_only and (args.update or verify_mode):
        command = update_command_hint(
            Path(args.plugins_dir).expanduser(),
            recursive=bool(args.recursive),
            config_path=config_path,
            no_config=bool(args.no_config),
        )
        print(
            "umod_plugins_check.py is check-only. Run Oxide Plugin Updater "
            "for installations:",
            file=sys.stderr,
        )
        print(f"  {command}", file=sys.stderr)
        return 2

    plugins_dir = Path(args.plugins_dir).expanduser()
    cache_path = Path(args.cache).expanduser()
    state_path = Path(args.state_file).expanduser()
    state_enabled = bool(state_config.get("enabled", True)) and not args.no_state
    history_limit = int(state_config.get("history_limit_per_plugin", 50))
    reload_history_limit = int(state_config.get("reload_history_limit", 50))
    fallback_backoff_s = int(
        network_config.get("fallback_backoff_seconds", 30)
    )
    max_backoff_s = int(
        network_config.get("maximum_backoff_seconds", 300)
    )
    if fallback_backoff_s < 1 or max_backoff_s < fallback_backoff_s:
        print(
            "Configuration error: network backoff values must satisfy "
            "1 <= fallback_backoff_seconds <= maximum_backoff_seconds",
            file=sys.stderr,
        )
        return 2
    logging_enabled = bool(logging_config.get("enabled", True))
    audit = AuditLogger(
        None
        if args.no_log or not logging_enabled
        else Path(args.log_file).expanduser()
    )
    _NETWORK_AUDIT = audit
    audit.write(
        "run_started",
        mode=(
            "verify"
            if verify_mode
            else "update"
            if args.update
            else "check"
        ),
        program=(
            "umod_plugins_check"
            if legacy_check_only
            else "oxide_plugin_updater"
        ),
        config_file=None if args.no_config else str(config_path),
        plugins_dir=str(plugins_dir),
        recursive=bool(args.recursive),
        reload_plugins_after_updates=bool(
            args.reload_plugins_after_updates
        ),
    )
    cache = load_cache(cache_path)
    if args.override_cache:
        print(
            "Cache override enabled: fetching fresh upstream results as "
            "needed; refreshed results will still be cached.",
            file=sys.stderr,
        )
    package_state: Optional[Dict[str, Any]] = None
    if state_enabled and not verify_mode:
        try:
            package_state = load_plugin_state(state_path)
        except ValueError as e:
            print(f"WARNING: {e}; starting with empty package state.", file=sys.stderr)
            audit.write("state_load_failed", state_file=str(state_path), reason=str(e))
            package_state = _empty_plugin_state()

    try:
        locals_ = scan_plugins(
            plugins_dir,
            recursive=bool(args.recursive),
        )
    except FileNotFoundError:
        print_missing_plugins_directory_help(
            plugins_dir,
            config_path=config_path,
            no_config=bool(args.no_config),
        )
        audit.write("run_failed", reason="plugin_directory_not_found")
        return 2
    if not locals_:
        print(f"No plugins found in directory: {plugins_dir}")
        audit.write("check_summary", total=0, outdated=0, auto_updateable=0)
        return 0

    requested_single_plugin = args.update_plugin or args.verify_plugin
    if requested_single_plugin:
        requested_name = Path(str(requested_single_plugin)).name
        if requested_name.casefold().endswith(".cs"):
            requested_name = requested_name[:-3]
        matches = [
            plugin
            for plugin in locals_
            if Path(str(plugin.get("filename") or "")).stem.casefold()
            == requested_name.casefold()
        ]
        if not matches:
            print(
                f"Plugin not found: {requested_single_plugin}",
                file=sys.stderr,
            )
            return 2
        if len(matches) != 1:
            paths = ", ".join(
                str(plugin.get("file") or plugin.get("filename") or "-")
                for plugin in matches
            )
            print(
                f"Plugin name is ambiguous: "
                f"{requested_single_plugin} ({paths})",
                file=sys.stderr,
            )
            return 2
        locals_ = matches

    show_progress = args.progress or (sys.stderr.isatty() and not args.no_progress)
    use_color = want_color(args.color)

    if verify_mode:
        plugins_to_verify = [
            (
                str(plugin.get("filename") or ""),
                str(plugin.get("version") or "-"),
            )
            for plugin in locals_
        ]
        activation_records: List[Dict[str, str]] = []
        print(
            color_text(
                f"Verifying {len(plugins_to_verify)} "
                f"{'plugin' if len(plugins_to_verify) == 1 else 'plugins'} "
                "sequentially via targeted Oxide commands:",
                "cyan",
                use=use_color,
                bold=True,
            )
        )

        def show_verify_progress(
            index: int,
            total: int,
            filename: str,
            status: str,
            detail: str,
        ) -> None:
            status_color = "green" if status == "OK" else "red"
            command = (
                activation_records[-1].get("command", "")
                if activation_records
                else ""
            )
            print(
                f"  [{index:>{len(str(total))}}/{total}] "
                f"{filename} -- "
                + color_text(
                    status,
                    status_color,
                    use=use_color,
                    bold=status != "OK",
                )
                + (f" ({command})" if command else "")
            )
            if status != "OK" and detail:
                print(f"      {detail}")

        verify_succeeded, verify_detail = reload_updated_plugins(
            rcon_config,
            plugins_to_verify,
            progress=show_verify_progress,
            activation_records=activation_records,
        )
        total_plugins = len(plugins_to_verify)
        compiled_ok = sum(
            record.get("status") == "OK"
            for record in activation_records
        )
        compile_failures = [
            record
            for record in activation_records
            if record.get("status") == "FAILED TO COMPILE"
        ]
        other_failures = [
            record
            for record in activation_records
            if record.get("status") not in {"OK", "FAILED TO COMPILE"}
        ]
        unrecorded = max(0, total_plugins - len(activation_records))

        print()
        print(terminal_rule())
        print(
            color_text(
                "Plugin verification summary:",
                "cyan",
                use=use_color,
                bold=True,
            )
        )
        print(
            color_text(
                f"{compiled_ok} out of {total_plugins} "
                f"{'plugin' if total_plugins == 1 else 'plugins'} "
                "compiled/loaded successfully.",
                "green" if compiled_ok == total_plugins else "yellow",
                use=use_color,
                bold=True,
            )
        )
        print(
            color_text(
                f"{len(compile_failures)} "
                f"{'plugin' if len(compile_failures) == 1 else 'plugins'} "
                "failed to compile.",
                "red" if compile_failures else "green",
                use=use_color,
                bold=bool(compile_failures),
            )
        )
        could_not_verify = len(other_failures) + unrecorded
        if could_not_verify:
            print(
                color_text(
                    f"{could_not_verify} "
                    f"{'plugin could' if could_not_verify == 1 else 'plugins could'} "
                    "not be verified.",
                    "red",
                    use=use_color,
                    bold=True,
                )
            )

        failed_records = compile_failures + other_failures
        if failed_records:
            print("Failures:")
            for record in failed_records:
                plugin = record.get("plugin") or "-"
                status = record.get("status") or "FAILED"
                command = record.get("command") or "-"
                response = record.get("response") or "no response"
                print(f"  {plugin} -- {status}")
                print(f"    Command: {command}")
                print(f"    Error: {response}")
        if unrecorded and verify_detail:
            print("Verification error:")
            print(f"  {verify_detail}")
        print(terminal_rule())
        audit.write(
            "plugins_verify",
            result="ok" if verify_succeeded else "failed",
            response=verify_detail,
            verified_plugins=len(plugins_to_verify),
            compiled_loaded_ok=compiled_ok,
            failed_to_compile=len(compile_failures),
            could_not_verify=could_not_verify,
            activations=activation_records,
        )
        return 0 if verify_succeeded else 2

    check_chaos = bool(getattr(args, "check_chaos", True))
    # older-python fallback parsing if both flags exist
    if hasattr(args, "no_check_chaos") and getattr(args, "no_check_chaos"):
        check_chaos = False

    chaos_index: Dict[str, Dict[str, Any]] = {}
    chaos_load_err: Optional[str] = None
    chaos_manifest_cached = False
    chaos_manifest_checked = False

    def ensure_chaos_manifest() -> None:
        nonlocal chaos_index, chaos_load_err
        nonlocal chaos_manifest_cached, chaos_manifest_checked
        if not check_chaos or chaos_manifest_checked:
            return
        chaos_manifest_checked = True
        chaos_ent = cache.get("chaos:manifest")
        if isinstance(chaos_ent, dict) and not args.override_cache:
            chaos_ts = int(chaos_ent.get("ts", 0) or 0)
            chaos_manifest_cached = bool(
                chaos_ts
                and (int(time.time()) - chaos_ts)
                <= int(args.chaos_cache_ttl)
                and isinstance(chaos_ent.get("data"), list)
            )
        try:
            chaos_index = load_chaos_manifest(
                cache,
                cache_path,
                ttl_s=-1 if args.override_cache else int(args.chaos_cache_ttl),
                timeout_s=int(args.timeout),
                debug_headers=bool(args.debug_headers),
                fallback_backoff_s=fallback_backoff_s,
                max_backoff_s=max_backoff_s,
                min_interval_s=float(args.min_interval),
                max_interval_s=float(args.max_interval),
            )
        except Exception as e:
            chaos_load_err = str(e)
            chaos_index = {}

    # A targeted uMod hit does not need the global ChaosCode manifest. Defer
    # that request until the selected plugin actually needs the fallback.
    if check_chaos and not args.update_plugin:
        ensure_chaos_manifest()

    hdr = f"Found {len(locals_)} plugins in {plugins_dir} -- checking uMod"
    if check_chaos:
        hdr += " (+ ChaosCode fallback)"
        if chaos_load_err:
            hdr += f" [Chaos manifest ERROR: {chaos_load_err}]"
    print(hdr + "...", file=sys.stderr)

    out_rows: List[Dict[str, Any]] = []
    update_candidates: List[UpdateCandidate] = []
    outdated_count = 0
    unknown_count = 0
    any_outdated = False
    any_unknown = False
    outdated_history_counts = {"new": 0, "known": 0, "changed": 0}
    known_unconfirmed: List[str] = []
    network_circuit_reason = ""

    for i, p in enumerate(locals_, start=1):
        filename = str(p.get("filename") or "")
        stem = stem_noext(filename)
        stem_l = stem.lower()
        local_ver = str(p.get("version") or "") or ""

        # ---------------- uMod lookup ----------------
        source = "umod"
        status = ""
        remote_ver = "-"
        remote_url = "-"

        key = f"umod:json:{stem}"
        now = int(time.time())
        cached = False
        data = None

        ent = cache.get(key)
        if isinstance(ent, dict) and not args.override_cache:
            ts = int(ent.get("ts", 0) or 0)
            if ts and (now - ts) <= int(args.cache_ttl) and "data" in ent:
                data = ent["data"]
                cached = True

        if data is None:
            url = umod_direct_json_url(stem)
            if network_circuit_reason:
                status = (
                    "ERROR: network refresh skipped after persistent "
                    f"rate limit ({network_circuit_reason})"
                )
                any_unknown = True
            else:
                try:
                    res = http_get_json(
                        url,
                        timeout_s=int(args.timeout),
                        min_interval_s=float(args.min_interval),
                        max_retries=int(args.max_retries),
                        debug_headers=bool(args.debug_headers),
                        fallback_backoff_s=fallback_backoff_s,
                        max_backoff_s=max_backoff_s,
                        max_interval_s=float(args.max_interval),
                    )
                    data = res.data
                    cache[key] = {"ts": now, "data": data}
                    save_cache(cache_path, cache)
                except FileNotFoundError:
                    data = None
                except Exception as e:
                    status = f"ERROR: {e}"
                    any_unknown = True
                    if is_rate_limit_failure(e):
                        network_circuit_reason = str(e)
                        _console_event(
                            "NETWORK",
                            "Persistent rate limit detected; skipping further "
                            "uncached remote requests during this run and "
                            "continuing with cached/history data.",
                        )
                        audit.write(
                            "network_circuit_open",
                            reason=network_circuit_reason,
                        )

        # Fallback search.json only if asked
        if (
            data is None
            and args.fallback_search
            and not status
            and not network_circuit_reason
        ):
            try:
                sres = http_get_json(
                    umod_search_url(stem),
                    timeout_s=int(args.timeout),
                    min_interval_s=float(args.min_interval),
                    max_retries=int(args.max_retries),
                    debug_headers=bool(args.debug_headers),
                    fallback_backoff_s=fallback_backoff_s,
                    max_backoff_s=max_backoff_s,
                    max_interval_s=float(args.max_interval),
                )
                m = best_match_from_search(filename, sres.data if isinstance(sres.data, dict) else {})
                if m:
                    remote_ver = str(m.get("latest_release_version") or "-")
                    remote_url = str(m.get("url") or "-")
                    data = m
                else:
                    status = "UNKNOWN (no match)"
                    any_unknown = True
            except Exception as e:
                status = f"ERROR: {e}"
                any_unknown = True

        if data is not None and not status:
            if not isinstance(data, dict):
                status = "ERROR: unexpected uMod metadata shape"
                any_unknown = True
                data = None

        if data is not None and not status:
            remote_ver = str(data.get("latest_release_version") or "-")
            remote_url = str(data.get("url") or "-")

            if local_ver != "-" and remote_ver != "-" and local_ver and remote_ver:
                if local_ver == remote_ver:
                    status = "OK"
                else:
                    newer = version_is_newer(remote_ver, local_ver)
                    status = "OUTDATED" if (newer is True or newer is None) else "OK"
            else:
                status = "UNKNOWN (missing version)"
                any_unknown = True

        # If uMod failed/unknown and chaos enabled -> Chaos fallback
        if (
            (not status or status.startswith("UNKNOWN"))
            and check_chaos
            and not status.startswith("ERROR")
        ):
            ensure_chaos_manifest()
        if (not status or status.startswith("UNKNOWN")) and check_chaos and chaos_index and (not status.startswith("ERROR")):
            rr = chaos_index.get(stem_l)
            if isinstance(rr, dict):
                source = "chaos"
                cached = chaos_manifest_cached
                remote_ver = str(rr.get("ResourceVersion") or "-")
                remote_url = str(rr.get("ResourceURL") or "-")

                if local_ver and remote_ver and local_ver != "-" and remote_ver != "-":
                    if local_ver == remote_ver:
                        status = "OK"
                    else:
                        newer = version_is_newer(remote_ver, local_ver)
                        status = "OUTDATED" if (newer is True or newer is None) else "OK"
                else:
                    status = "UNKNOWN (missing version)"
                    any_unknown = True

        if not status:
            status = "UNKNOWN (no match)"
            any_unknown = True
            source = "unknown"

        if status == "OUTDATED":
            any_outdated = True
            outdated_count += 1
        if status.startswith(("UNKNOWN", "ERROR")):
            unknown_count += 1

        row = {
            "filename": filename,
            "source": source,
            "local": local_ver or "-",
            "remote": remote_ver or "-",
            "status": status,
            "remote_url": remote_url or "-",
            "outdated_history": "-",
            "previously_known_outdated": False,
        }
        if package_state is not None:
            classification = observe_plugin_state(
                package_state,
                key=plugin_state_key(p, plugins_dir),
                plugin=p,
                row=row,
                history_limit=history_limit,
            )
            row["outdated_history"] = classification or "-"
            if classification:
                outdated_history_counts[classification] += 1
            state_entry = package_state["plugins"].get(
                plugin_state_key(p, plugins_dir),
                {},
            )
            if (
                status.startswith(("UNKNOWN", "ERROR"))
                and isinstance(state_entry, dict)
                and isinstance(state_entry.get("active_outdated"), dict)
            ):
                row["previously_known_outdated"] = True
                known_unconfirmed.append(filename)

        if (
            (status == "OUTDATED" or (
                bool(args.force)
                and source == "umod"
                and isinstance(data, dict)
                and remote_ver not in ("", "-")
            ))
            and source == "umod"
            and isinstance(data, dict)
            and (
                bool(args.force)
                or version_is_newer(remote_ver, local_ver) is True
            )
        ):
            try:
                update_candidates.append(
                    UpdateCandidate(
                        filename=filename,
                        path=Path(str(p.get("file") or (plugins_dir / filename))),
                        name=str(p.get("name") or stem),
                        local_version=local_ver,
                        remote_version=remote_ver,
                        download_url=umod_download_url(filename, data),
                        local_sha256=str(p.get("sha256") or ""),
                    )
                )
            except ValueError:
                # Keep the row as OUTDATED/manual rather than trusting a bad URL.
                pass

        if args.outdated_only and status != "OUTDATED":
            if show_progress:
                tag = "cached" if cached else "net"
                st = color_status(status, use=use_color)
                print(f"[{i}/{len(locals_)}] {filename} [{source}] -- {st} ({tag})", file=sys.stderr)
            continue

        out_rows.append(row)

        if show_progress:
            tag = "cached" if cached else "net"
            st = color_status(status, use=use_color)
            print(f"[{i}/{len(locals_)}] {filename} [{source}] -- {st} ({tag})", file=sys.stderr)

    if package_state is not None:
        try:
            save_plugin_state(state_path, package_state)
        except OSError as e:
            print(
                f"WARNING: could not save package state {state_path}: {e}",
                file=sys.stderr,
            )
            audit.write(
                "state_save_failed",
                state_file=str(state_path),
                reason=str(e),
            )

    if args.as_json:
        print(json.dumps(out_rows, ensure_ascii=False, indent=2))
    else:
        print_table(out_rows, use_color=use_color)

    manual_outdated = max(0, outdated_count - len(update_candidates))
    any_unknown = unknown_count > 0
    audit.write(
        "check_summary",
        total=len(locals_),
        outdated=outdated_count,
        auto_updateable=len(update_candidates),
        manual_only=manual_outdated,
        unknown_error=unknown_count,
        outdated_new=outdated_history_counts["new"],
        outdated_known=outdated_history_counts["known"],
        outdated_changed=outdated_history_counts["changed"],
        previously_known_outdated_unconfirmed=len(known_unconfirmed),
    )

    update_failures = 0
    update_successes = 0
    source_updates = 0
    source_revalidations = 0
    updated_filenames = set()
    updated_plugins_for_reload: List[Tuple[str, str]] = []
    failed_filenames = set()
    reload_attempted = False
    reload_succeeded = False
    reload_detail = ""
    if not args.as_json and args.update:
        print()
        print(terminal_rule())
        print(
            color_text(
                f"Processing {len(update_candidates)} eligible uMod "
                f"{'plugin' if len(update_candidates) == 1 else 'plugins'}:",
                "yellow",
                use=use_color,
                bold=True,
            )
        )
        print(terminal_rule())

        backup_root = Path(args.backup_dir).expanduser()
        if path_is_within(backup_root, plugins_dir):
            message = (
                f"backup directory must not be inside the live plugin tree: "
                f"{backup_root}"
            )
            print(color_text(f"REFUSED: {message}", "red", use=use_color))
            audit.write("update_refused", reason=message)
            return 2
        for index, candidate in enumerate(update_candidates, start=1):
            if index > 1:
                print()
            installed_ok = install_update(
                candidate,
                plugins_dir=plugins_dir,
                backup_root=backup_root,
                timeout_s=int(args.timeout),
                min_interval_s=float(args.min_interval),
                max_retries=int(args.max_retries),
                debug_headers=bool(args.debug_headers),
                allow_large_shrink=bool(args.allow_large_shrink),
                use_color=use_color,
                audit=audit,
                max_plugin_bytes=int(
                    validation_config.get(
                        "maximum_plugin_bytes",
                        MAX_PLUGIN_BYTES,
                    )
                ),
                shrink_warn_percent=float(
                    validation_config.get(
                        "shrink_warning_percent",
                        SHRINK_WARN_PERCENT,
                    )
                ),
                shrink_refuse_percent=float(
                    validation_config.get(
                        "shrink_refusal_percent",
                        SHRINK_REFUSE_PERCENT,
                    )
                ),
                fallback_backoff_s=fallback_backoff_s,
                max_backoff_s=max_backoff_s,
                max_interval_s=float(args.max_interval),
                package_state=package_state,
                state_history_limit=history_limit,
                force_reinstall=bool(args.force),
            )
            if installed_ok:
                update_successes += 1
                if getattr(installed_ok, "source_changed", True):
                    source_updates += 1
                else:
                    source_revalidations += 1
                updated_filenames.add(candidate.filename)
                updated_plugins_for_reload.append(
                    (candidate.filename, candidate.remote_version)
                )
                if package_state is not None:
                    try:
                        save_plugin_state(state_path, package_state)
                    except OSError as e:
                        print(
                            f"WARNING: could not save package state "
                            f"{state_path}: {e}",
                            file=sys.stderr,
                        )
                        audit.write(
                            "state_save_failed",
                            state_file=str(state_path),
                            reason=str(e),
                        )
            else:
                update_failures += 1
                failed_filenames.add(candidate.filename)

        if update_successes and args.reload_plugins_after_updates:
            reload_attempted = True
            activation_records: List[Dict[str, str]] = []
            print()
            print(
                color_text(
                    "Activating processed plugins individually via RCON "
                    "and checking compile results:",
                    "cyan",
                    use=use_color,
                    bold=True,
                )
            )

            def show_reload_progress(
                index: int,
                total: int,
                filename: str,
                status: str,
                detail: str,
            ) -> None:
                status_color = "green" if status == "OK" else "red"
                print(
                    f"  [{index:>{len(str(total))}}/{total}] "
                    f"{filename} -- "
                    + color_text(
                        status,
                        status_color,
                        use=use_color,
                        bold=status != "OK",
                    )
                )
                if status != "OK" and detail:
                    print(f"      {detail}")

            reload_succeeded, reload_detail = reload_updated_plugins(
                rcon_config,
                updated_plugins_for_reload,
                progress=show_reload_progress,
                activation_records=activation_records,
            )
            if reload_succeeded:
                print(
                    color_text(
                        "  Plugin activation: OK",
                        "green",
                        use=use_color,
                        bold=True,
                    )
                )
            else:
                print(
                    color_text(
                        f"  Plugin activation: FAILED ({reload_detail})",
                        "red",
                        use=use_color,
                        bold=True,
                    )
                )
            audit.write(
                "plugins_reload",
                operation="activation",
                command=RCON_ACTIVATION_COMMAND,
                activations=activation_records,
                result="ok" if reload_succeeded else "failed",
                response=reload_detail,
                updated_plugins=update_successes,
            )
            if package_state is not None:
                record_reload_state(
                    package_state,
                    result="ok" if reload_succeeded else "failed",
                    detail=reload_detail,
                    updated_plugins=update_successes,
                    limit=reload_history_limit,
                    activations=activation_records,
                )
        elif update_successes:
            reload_detail = "disabled by configuration or CLI"
            audit.write(
                "plugins_reload",
                operation="activation",
                command=RCON_ACTIVATION_COMMAND,
                activations=[],
                result="deferred",
                reason=reload_detail,
                updated_plugins=update_successes,
            )
            if package_state is not None:
                record_reload_state(
                    package_state,
                    result="deferred",
                    detail=reload_detail,
                    updated_plugins=update_successes,
                    limit=reload_history_limit,
                    activations=[],
                )

        if package_state is not None and update_successes:
            try:
                save_plugin_state(state_path, package_state)
            except OSError as e:
                print(
                    f"WARNING: could not save package state {state_path}: {e}",
                    file=sys.stderr,
                )
                audit.write(
                    "state_save_failed",
                    state_file=str(state_path),
                    reason=str(e),
                )

        remaining_rows = [
            row
            for row in out_rows
            if row.get("status") == "OUTDATED"
            and row.get("filename") not in updated_filenames
        ]

        print()
        print(terminal_rule())
        print(color_text("Update summary:", "cyan", use=use_color, bold=True))
        print(
            f"{len(locals_)} "
            f"{'plugin' if len(locals_) == 1 else 'plugins'} found in "
            f"directory: {plugins_dir}"
        )
        print(
            color_text(
                f"{source_updates} plugin "
                f"{'source' if source_updates == 1 else 'sources'} updated.",
                "green",
                use=use_color,
                bold=True,
            )
        )
        if source_revalidations:
            print(
                color_text(
                    f"{source_revalidations} plugin "
                    f"{'source was' if source_revalidations == 1 else 'sources were'} "
                    "already identical and revalidated.",
                    "green",
                    use=use_color,
                    bold=True,
                )
            )
        print(
            color_text(
                f"{len(remaining_rows)} "
                f"{'plugin remains' if len(remaining_rows) == 1 else 'plugins remain'} "
                "outdated.",
                "yellow" if remaining_rows else "green",
                use=use_color,
                bold=True,
            )
        )
        print(terminal_rule())
        if remaining_rows:
            print(
                color_text(
                    f"Plugins that still need to be updated "
                    f"({len(remaining_rows)}):",
                    "yellow",
                    use=use_color,
                    bold=True,
                )
            )
            for row in remaining_rows:
                filename = str(row.get("filename") or "-")
                local_version = str(row.get("local") or "-")
                remote_version = str(row.get("remote") or "-")
                remote_url = str(row.get("remote_url") or "-")
                source = str(row.get("source") or "unknown")
                disposition = (
                    "failed/refused"
                    if filename in failed_filenames
                    else "manual"
                )
                arrow = color_text("->", "cyan", use=use_color)
                print(
                    f"  {filename}: {local_version} {arrow} "
                    f"{remote_version} [{source}; {disposition}]"
                )
                print(f"    [url: {remote_url} ]")
        else:
            print(
                color_text(
                    "No confirmed outdated plugins remain.",
                    "green",
                    use=use_color,
                    bold=True,
                )
            )
        print(terminal_rule())
        print(f"  Source updates:{source_updates:>3}")
        if source_revalidations:
            print(f"  Revalidated:   {source_revalidations:>3}")
        print(f"  Failed/refused:{update_failures:>3}")
        print(f"  Manual-only:   {manual_outdated}")
        print(f"  Unknown/error: {unknown_count}")
        if known_unconfirmed:
            print(f"  Known outdated, unconfirmed: {len(known_unconfirmed)}")
        if source_updates:
            print(f"  Backups:       {backup_root}")
        if reload_attempted:
            print(
                "  Plugin activation: "
                + ("OK" if reload_succeeded else "FAILED")
            )
        elif update_successes:
            print("  Plugin activation: deferred")
        else:
            print("  Plugin activation: not needed")
        print(terminal_rule())
        audit.write(
            "update_summary",
            updated=source_updates,
            processed_successfully=update_successes,
            source_updates=source_updates,
            source_revalidations=source_revalidations,
            failed_or_refused=update_failures,
            manual_only=manual_outdated,
            unknown_error=unknown_count,
            backup_root=str(backup_root),
            remaining_outdated=len(remaining_rows),
            remaining_outdated_plugins=[
                row.get("filename") for row in remaining_rows
            ],
            reload_command=RCON_ACTIVATION_COMMAND,
            reload_result=(
                "ok"
                if reload_succeeded
                else "failed"
                if reload_attempted
                else "deferred"
                if update_successes
                else "not_needed"
            ),
        )
    elif not args.as_json:
        print_check_summary(
            update_candidates,
            manual_outdated=manual_outdated,
            plugins_dir=plugins_dir,
            recursive=bool(args.recursive),
            use_color=use_color,
            config_path=config_path,
            no_config=bool(args.no_config),
            history_counts=outdated_history_counts,
            known_unconfirmed=known_unconfirmed,
        )

    if any_unknown:
        return 2
    if update_failures:
        return 2
    if reload_attempted and not reload_succeeded:
        return 2
    if args.update:
        return 1 if manual_outdated else 0
    if any_outdated:
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
