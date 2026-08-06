#!/usr/bin/env python3

# =============================================================
# https://github.com/FlyingFathead/rust-linuxgsm-watchdog
# -------------------------------------------------------------
# A restart & update watchdog for Rust game servers on LinuxGSM
# -------------------------------------------------------------
# FlyingFathead / 2026 / https://github.com/FlyingFathead/
# =============================================================

import argparse
from dataclasses import dataclass
import errno
import getpass
import html
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
# Python 3.9+: stdlib timezone database access (requires tzdata on the host)
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore
except Exception:
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

__version__ = "0.4.10"


def _runtime_version():
    """Return the canonical version, or an empty string if it is unavailable."""
    try:
        return str(globals().get("__version__") or "").strip()
    except Exception:
        return ""


def _runtime_version_label():
    """Return the display form used in alert headers."""
    version = _runtime_version()
    if not version:
        return "N/A"
    return version if version.lower().startswith("v") else f"v{version}"


SMOOTHRESTARTER_URL = "https://umod.org/plugins/smooth-restarter"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FOR_HINTS = None
STATUS_COORDINATOR = None
DEFAULTS = {
    "server_dir": "/home/rustserver",
    "identity": "rustserver",

    "interval_seconds": 30,
    "cooldown_seconds": 120,

    "lockfile": os.path.join(PROJECT_DIR, "data", "lock", "rust_watchdog.lock"),

    # NOTE: this must be a FILE path, not a directory
    # "logfile":  os.path.join(PROJECT_DIR, "log",  "rust_watchdog.log"),
    "logfile":  os.path.join(PROJECT_DIR, "data", "log", "rust_watchdog.log"),

    # Pause feature enabled by default (only pauses if the file exists)
    "pause_file": os.path.join(PROJECT_DIR, "data", ".watchdog_pause"),

    # DRY RUN MODE: when true, never runs recovery steps
    "dry_run": False,

    # watch for updates
    "enable_update_watch": True,
    "update_check_interval_seconds": 600,
    "update_check_timeout": 60,

    # ---------------------------------------------------------
    # Duplicate RustDedicated guard (same +server.identity)
    # ---------------------------------------------------------
    "dupe_identity_policy": "pause",   # "warn" | "pause" | "fatal" | "kill_extra"
    "dupe_identity_check_listen_port": True,
    "server_port": 28015,             # used for listen check when possible

    # ---------------------------------------------------------
    # Forced wipe highlighter (Rust monthly forced wipe baseline)
    # First Thursday of month, 19:00 Europe/London
    # ---------------------------------------------------------
    "enable_forced_wipe_highlight": True,
    "forced_wipe_tz": "Europe/London",
    "forced_wipe_hour": 19,
    "forced_wipe_minute": 0,

    # How long before wipe we consider it "soon" (lead time)
    "forced_wipe_lead_hours": 24,

    # How long after the scheduled time we still consider it "wipe window"
    "forced_wipe_window_minutes": 180,

    # Pre-wipe update hold (avoid restart/update thrash just before wipe)
    # If update-watch finds an update during the hold window, we only log it.
    "forced_wipe_update_hold": True,
    "forced_wipe_update_hold_before_minutes": 360,  # 6h

    # Optional: if server is DOWN during pre-wipe hold, skip update/mu and just restart
    "forced_wipe_recovery_restart_only_prewipe": True,

    # ---------------------------------------------------------
    # Optional automatic forced-wipe action
    # ---------------------------------------------------------
    # Destructive behavior is deliberately opt-in for upgrades.
    # Values: "off" | "map-wipe" | "full-wipe"
    "forced_wipe_action": "off",
    "forced_wipe_trigger": "new-build-after-schedule",
    "forced_wipe_early_release_tolerance_minutes": 15,
    "forced_wipe_action_window_minutes": 360,
    # Optional calendar backstop: if this cycle was observed before the action
    # window ended but no wipe was recorded, arm the configured wipe action at
    # the end of that window even when no monthly build was identified.
    "forced_wipe_fallback_at_window_end": False,
    "forced_wipe_backup_before": True,
    "forced_wipe_backup_required": True,
    "forced_wipe_verify_update_current": True,
    "forced_wipe_state_file": os.path.join(PROJECT_DIR, "data", "state", "forced_wipe.json"),

    # With automatic deletion off, keep warning after the monthly schedule
    # passes until an administrator records that the manual wipe is complete.
    "forced_wipe_reminder_enabled": True,
    "forced_wipe_reminder_repeat_minutes": 30,
    "forced_wipe_reminder_message_template":
        "⚠️ FORCED WIPE DUE: cycle {cycle} entered its scheduled wipe window "
        "at {wipe_tz} ({tz_name}); no completed wipe is recorded and "
        "forced_wipe_action={action}.",

    # Cadence schedule (time-to-wipe -> log interval)
    # Each entry can include:
    #   - dt_gt_seconds: match if dt_seconds > this
    #   - dt_lte_seconds: match if dt_seconds <= this
    #   - interval_seconds: required
    # First match wins; last entry can be a fallback with only interval_seconds.
    "forced_wipe_log_schedule": [
        {"dt_gt_seconds": 604800, "interval_seconds": 86400},  # > 7d   -> daily
        {"dt_gt_seconds": 172800, "interval_seconds": 21600},  # > 48h  -> 6h
        {"dt_gt_seconds": 86400,  "interval_seconds": 7200},   # > 24h  -> 2h
        {"dt_gt_seconds": 21600,  "interval_seconds": 3600},   # > 6h   -> 1h
        {"dt_gt_seconds": 3600,   "interval_seconds": 1800},   # > 1h   -> 30m
        {"dt_gt_seconds": 600,    "interval_seconds": 300},    # > 10m  -> 5m
        {"dt_gt_seconds": 0,      "interval_seconds": 60},     # > 0    -> 1m
        {"dt_gt_seconds": -10800, "interval_seconds": 600},    # > -3h  -> 10m
        {"interval_seconds": 86400},                            # fallback
    ],

    # Message strings
    "forced_wipe_tag_scheduled": "scheduled",
    "forced_wipe_tag_soon": "WIPE SOON",
    "forced_wipe_tag_window": "WIPE WINDOW",
    "forced_wipe_message_template":
        "FORCED_WIPE: next = {wipe_tz} ({tz_name}) | local={wipe_local} | utc={wipe_utc} | in {eta} | {tag}",

    # Recovery toggles (convenience flags; defaults keep current behavior)
    "enable_server_update": True,   # controls "update"
    "enable_mods_update": True,     # controls "mu" (mod updates)

    # Health checks (any PASS => RUNNING)
    "check_process_identity": True,  # pgrep RustDedicated + identity
    "check_tcp_rcon": True,          # TCP connect to rcon port
    "rcon_host": "127.0.0.1",
    "rcon_port": 28016,
    "tcp_timeout": 2.0,

    # Read Rust's authoritative current save creation time from
    # `serverinfo.SaveCreatedTime` over authenticated WebRCON. This is the
    # primary source for reconstructing the last map-wipe timestamp.
    "wipe_timestamp_rcon_enabled": True,
    # If RCON is unavailable, use the newest stable .map file mtime under the
    # LinuxGSM server identity directory. Active .sav mtimes are never used.
    "wipe_timestamp_filesystem_fallback_enabled": True,
    "wipe_timestamp_interval_seconds": 600,

    "check_lgsm_details": False,      # parse ./rustserver details output (usually only for debugging)
    "details_timeout": 20,

    # Only recover if we see DOWN this many times in a row
    "down_confirmations": 2,

    # What to do when confirmed DOWN
    "recovery_steps": ["update", "mu", "restart"],

    "timeouts": {
        "update": 1800,
        "mu": 900,
        "restart": 600,
        "start": 600,
        "stop": 120,
        "backup": 1800,
        "full-wipe": 600,
        "map-wipe": 600,
    },

    # ---------------------------------------------------------
    # Optional: SmoothRestarter bridge
    # ---------------------------------------------------------
    # check for SmoothRestarter.cs integrity
    # Optional: verify SmoothRestarter is actually loaded (via RCON plugin list / status)
    "smoothrestarter_check_loaded": True,          # <-- main toggle
    "smoothrestarter_check_loaded_strict": False,   # if true: treat "not loaded" as NOT OK

    "smoothrestarter_probe_strict": False,
    "smoothrestarter_probe_min_score": 2,
    "smoothrestarter_command": "sr",

    "enable_smoothrestarter_bridge": False,
    "smoothrestarter_restart_delay_seconds": 300,
    "smoothrestarter_console_cmd": "srestart restart {delay}",

    # ---------------------------------------------------------
    # SmoothRestarter bridge TEST ceremony (safe dry-run)
    # ---------------------------------------------------------
    # For --test-smoothrestarter-send:
    #   -- announce "dry run"
    #   -- start a short SR countdown
    #   -- wait a few seconds
    #   -- cancel it
    #   -- announce "test over"
    "smoothrestarter_test_delay_seconds": 120,
    "smoothrestarter_test_cancel_after_seconds": 8,
    "smoothrestarter_test_send_status": True,
    "smoothrestarter_test_chat_prefix": "[Rust Watchdog]",

    # Rate-limit restart requests (avoid spamming SR during loops)
    "restart_request_cooldown_seconds": 3600,

    # If SR is already counting down (or we fail to request it),
    # what should watchdog do?
    #   "fallback"  -> use no-SR countdown + stop/update/mu/restart NOW
    #   "log_only"  -> do NOT fallback; just log and let SR / humans handle it
    "smoothrestarter_fail_policy": "fallback",

    # When SR path is used, optionally do our own timed RCON notices based on the delay we requested.
    # This does not cancel/retry SR and does not change SR behavior.
    "update_watch_sr_notify": True,
    "update_watch_sr_notify_at_seconds": [300, 120, 60, 30, 10],
    "update_watch_sr_notify_template": "Restart in about {seconds}s (update detected).",
    "update_watch_sr_final_message": "Restarting now -- come back in a few minutes!",

    # ---------------------------------------------------------
    # Update-watch announcements + fallback countdown (no SR)
    # [SR = Smooth Restarter]
    #
    # Rule:
    # - If SR is enabled OR not, we still announce "reboot incoming".
    # - If SR is NOT enabled/working -> we do a crude countdown ourselves:
    #     "Time until server update and restart: xx seconds."
    #   then:
    #     "Server is restarting, come back in a few minutes!"
    # ---------------------------------------------------------
    "update_watch_announce_message":
        "Update detected -- restart incoming.",

    "update_watch_countdown_template":
        "Time until server update and restart: {seconds} seconds.",

    "update_watch_final_message":
        "Server is restarting, come back in a few minutes!",

    # No-SR countdown behavior (only used when SR bridge is disabled OR fails)
    "update_watch_no_sr_countdown_seconds": 30,
    "update_watch_no_sr_tick_seconds": 10,

    # Optional overrides (leave empty to use the default LinuxGSM layout)
    # If relative, they're resolved relative to server_dir.
    "smoothrestarter_config_path": "",
    "smoothrestarter_plugin_path": "",

    # ---------------------------------------------------------
    # Telegram test helper / systemd fallback inspection
    # ---------------------------------------------------------
    "watchdog_systemd_unit_name": "rust-watchdog.service",
    "watchdog_systemd_service_path": "/etc/systemd/system/rust-watchdog.service",
    "test_telegram_status_try_systemd_env_fallback": True,
    "test_telegram_status_check_systemd": True,

}

STATUS_RE = re.compile(r"^\s*Status:\s*(\S+)\s*$", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
UPDATE_YES_RE = re.compile(r"\b(update available|update required|available:\s*yes)\b", re.IGNORECASE)
UPDATE_NO_RE  = re.compile(r"\b(no update available|available:\s*no|already up to date|up to date)\b", re.IGNORECASE)
LOCAL_BUILD_RE = re.compile(r"\bLocal\s+build:\s*([0-9]+)\b", re.IGNORECASE)
REMOTE_BUILD_RE = re.compile(r"\bRemote\s+build:\s*([0-9]+)\b", re.IGNORECASE)
RCON_PW_RE = re.compile(r'(\+rcon\.password\s+)(\".*?\"|\S+)', re.IGNORECASE)
UNKNOWN_CMD_RE = re.compile(r"\bunknown\s+(command|console\s+command)\b", re.IGNORECASE)
SR_NAME_RE = re.compile(r"\bsmooth\s*restarter\b", re.IGNORECASE)


@dataclass(frozen=True)
class UpdateCheckResult:
    verdict: object
    local_build: str = ""
    remote_build: str = ""
    command: str = ""


@dataclass(frozen=True)
class ForcedWipeDecision:
    enabled: bool = False
    cycle: str = ""
    scheduled_utc: str = ""
    armed_now: bool = False
    pending: bool = False
    action_due: bool = False
    hold: bool = False
    reason: str = ""
    candidate_remote_build: str = ""
    armed_trigger: str = ""

# ---------------------------------------------------------
# HEALTH DIAGNOSIS (mapping: "what went to shit" -> hint)
# ---------------------------------------------------------
HEALTH_HINTS = {
    "OK": "All enabled health checks passed.",
    "NO_RUSTDEDI_PROCESS": "RustDedicated isn't running. Check LinuxGSM: ./rustserver details, then ./rustserver start.",
    "IDENTITY_MISMATCH": "RustDedicated is running, but +server.identity doesn't match cfg['identity'].",
    "RCON_ENDPOINT_MISSING": "No RCON host/port known (autodetect+config both missing). Set rcon_host/rcon_port or run with +rcon.ip/+rcon.port.",
    "RCON_CONN_REFUSED": "RCON port is closed/refusing. WebRCON not listening, wrong port, or server isn't actually up.",
    "RCON_TIMEOUT": "TCP connect timed out. Firewall, routing, or server stuck/hung.",
    "RCON_UNREACHABLE": "Host/network unreachable (bad IP, routing, or interface bind).",
    "LGSM_STOPPED": "LinuxGSM 'details' reports STOPPED. Try: ./rustserver start or inspect logs.",
    "LGSM_DETAILS_TIMEOUT": "LinuxGSM 'details' timed out. Script hung; check SteamCMD locks / disk / perms.",
    "LGSM_DETAILS_ERROR": "LinuxGSM 'details' errored. Check script path/perms and server_dir correctness.",
}

# Priority order for selecting the "primary cause"
CAUSE_PRIORITY = [
    "NO_RUSTDEDI_PROCESS",
    "IDENTITY_MISMATCH",
    "RCON_ENDPOINT_MISSING",
    "RCON_CONN_REFUSED",
    "RCON_TIMEOUT",
    "RCON_UNREACHABLE",
    "LGSM_STOPPED",
    "LGSM_DETAILS_TIMEOUT",
    "LGSM_DETAILS_ERROR",
]

@dataclass
class HealthCheckResult:
    name: str
    ok: bool
    code: str
    detail: str
    weight_up: int = 0
    weight_down: int = 0

def _pick_primary_cause(results):
    failing = [r.code for r in results if (r and not r.ok and r.code)]
    for code in CAUSE_PRIORITY:
        if code in failing:
            return code
    return "OK"

def _tcp_fail_code(e: Exception) -> str:
    # Most useful buckets first
    if isinstance(e, ConnectionRefusedError):
        return "RCON_CONN_REFUSED"
    if isinstance(e, socket.timeout) or isinstance(e, TimeoutError):
        return "RCON_TIMEOUT"

    if isinstance(e, OSError):
        err = getattr(e, "errno", None)
        if err in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL):
            return "RCON_UNREACHABLE"
        if err == errno.ECONNREFUSED:
            return "RCON_CONN_REFUSED"
        if err in (errno.ETIMEDOUT,):
            return "RCON_TIMEOUT"

    # fallback
    return "RCON_TIMEOUT"

# ---------------------------------------------------------
# ALERTS (optional module)
# ---------------------------------------------------------
ALERTS = None

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _read_lockfile_pid(lock_path: str):
    try:
        s = Path(lock_path).read_text(encoding="utf-8", errors="ignore").strip()
        pid = int(s)
        return pid if _pid_alive(pid) else None
    except Exception:
        return None


def _proc_started_at(pid: int) -> str:
    if not pid:
        return "unknown"
    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"

def _bool_tf(v) -> str:
    return "true" if bool(v) else "false"


def _parse_int_list_local(s: str):
    out = []
    s = (s or "").strip()
    if not s:
        return out
    for chunk in s.replace(",", " ").split():
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except Exception:
            pass
    return out

def get_server_process_info(cfg):
    """
    Return a safe summary for the RustDedicated process tied to this identity.

    Result keys:
      pid: int | None
      running: bool
      ambiguous: bool
      match_count: int
      selected_by: str
      exe_name: str
      started_at: str
      started_at_utc: str
      uptime_seconds: int | None
      uptime_human: str
    """
    identity = str(cfg.get("identity") or "").strip()
    try:
        server_port = int(cfg.get("server_port", 28015))
    except Exception:
        server_port = 28015
    use_listen_check = parse_bool(cfg.get("dupe_identity_check_listen_port"), True)

    info = {
        "pid": None,
        "running": False,
        "ambiguous": False,
        "match_count": 0,
        "selected_by": "",
        "exe_name": "unknown",
        "started_at": "not running",
        "started_at_utc": "",
        "uptime_seconds": None,
        "uptime_human": "not running",
    }

    if not identity:
        return info

    hits = find_rustdedicated_identity_matches(identity)
    info["match_count"] = len(hits)

    if not hits:
        return info

    chosen_pid = None

    if len(hits) == 1:
        chosen_pid = hits[0][0]
        info["selected_by"] = "identity"
    else:
        if use_listen_check:
            listeners = [pid for pid, _line in hits if pid_listens_udp_port(pid, server_port)]
            if len(listeners) == 1:
                chosen_pid = listeners[0]
                info["selected_by"] = f"identity+udp:{server_port}"
            elif len(listeners) > 1:
                info["ambiguous"] = True
                info["selected_by"] = f"multiple listeners on udp:{server_port}"
                return info

        if chosen_pid is None:
            info["ambiguous"] = True
            info["selected_by"] = "multiple identity matches"
            return info

    info["pid"] = chosen_pid
    info["running"] = True
    info["exe_name"] = _proc_exe_name(chosen_pid)
    info["started_at"] = _proc_started_at(chosen_pid)

    up_s = _proc_elapsed_seconds(chosen_pid)
    info["uptime_seconds"] = up_s
    info["uptime_human"] = _human_seconds(up_s) if up_s is not None else "unknown"
    info["started_at_utc"] = _proc_started_at_utc(chosen_pid)

    return info

def _systemd_unit_status(unit_name: str):
    info = {
        "unit_name": unit_name or "",
        "checked": False,
        "known": False,
        "active": False,
        "enabled": "unknown",
        "load_state": "unknown",
        "active_state": "unknown",
        "sub_state": "unknown",
        "error": "",
    }

    if not unit_name:
        info["error"] = "unit name empty"
        return info

    if not shutil.which("systemctl"):
        info["error"] = "systemctl not found"
        return info

    info["checked"] = True

    try:
        p = subprocess.run(
            [
                "systemctl",
                "show",
                unit_name,
                "--property=LoadState,ActiveState,SubState",
                "--no-pager",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        out = (p.stdout or "").strip()

        for line in out.splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k == "LoadState":
                info["load_state"] = v or "unknown"
            elif k == "ActiveState":
                info["active_state"] = v or "unknown"
            elif k == "SubState":
                info["sub_state"] = v or "unknown"

        info["known"] = info["load_state"] not in ("", "unknown", "not-found")
        info["active"] = (info["active_state"] == "active")

        if p.returncode != 0 and not out and not info["known"]:
            info["error"] = f"systemctl show rc={p.returncode}"

    except Exception as e:
        info["error"] = str(e)

    try:
        p2 = subprocess.run(
            ["systemctl", "is-enabled", unit_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
        enabled_out = (p2.stdout or "").strip()
        if enabled_out:
            info["enabled"] = enabled_out.splitlines()[-1].strip()
    except Exception:
        pass

    return info

def _proc_elapsed_seconds(pid: int):
    if not pid:
        return None
    try:
        out = subprocess.check_output(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if not out:
            return None
        return int(out)
    except Exception:
        return None


def _proc_started_at_utc(pid: int) -> str:
    """
    Resolve a Linux process start time without locale-dependent `ps` parsing.

    /proc/<pid>/stat field 22 is the process start time in clock ticks since
    boot; /proc/stat btime is the boot time as a Unix timestamp.
    """
    if not pid:
        return ""
    try:
        stat_text = Path(f"/proc/{int(pid)}/stat").read_text(
            encoding="utf-8",
            errors="replace",
        )
        right_paren = stat_text.rfind(")")
        if right_paren < 0:
            return ""
        fields_after_comm = stat_text[right_paren + 2:].split()
        # fields_after_comm[0] is field 3 (state), therefore field 22 is 19.
        start_ticks = int(fields_after_comm[19])
        ticks_per_second = int(os.sysconf("SC_CLK_TCK"))

        boot_epoch = None
        with open("/proc/stat", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("btime "):
                    boot_epoch = int(line.split()[1])
                    break
        if boot_epoch is None or ticks_per_second <= 0:
            return ""

        started_epoch = boot_epoch + (start_ticks / ticks_per_second)
        return _utc_iso(datetime.fromtimestamp(started_epoch, timezone.utc))
    except Exception:
        pass

    # Fallback for restricted/containerized environments where ps can see the
    # target process but this process cannot read the matching /proc entry.
    try:
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            text=True,
            stderr=subprocess.DEVNULL,
            env=env,
        ).strip()
        if out:
            local_naive = datetime.strptime(
                " ".join(out.split()),
                "%a %b %d %H:%M:%S %Y",
            )
            return _utc_iso(local_naive.astimezone())
    except Exception:
        pass

    # Last resort. Integer elapsed seconds are slightly less exact than the two
    # sources above, but still establish the correct restart minute.
    elapsed = _proc_elapsed_seconds(pid)
    if elapsed is not None:
        return _utc_iso(
            datetime.now(timezone.utc) - timedelta(seconds=max(0, elapsed))
        )
    return ""

def _human_seconds(total: int) -> str:
    if total is None:
        return "unknown"
    total = int(total)
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)

    if d > 0:
        return f"{d}d {h}h {m}m {s}s"
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def _proc_exe_name(pid: int) -> str:
    if not pid:
        return "unknown"
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe")) or "unknown"
    except Exception:
        return "unknown"

def _parse_systemd_environment_files(service_path: str):
    """
    Return (entries, err)
    entries: [{"path": "/etc/default/rust-watchdog", "optional": True}, ...]
    """
    try:
        text = Path(service_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return [], str(e)

    entries = []
    seen = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, rhs = line.split("=", 1)
        if key.strip().lower() != "environmentfile":
            continue

        try:
            tokens = shlex.split(rhs, posix=True)
        except Exception:
            tokens = [rhs.strip()]

        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue

            optional = False
            if tok.startswith("-"):
                optional = True
                tok = tok[1:].strip()

            tok = os.path.expandvars(os.path.expanduser(tok))
            if not tok:
                continue

            if tok in seen:
                continue
            seen.add(tok)
            entries.append({"path": tok, "optional": optional})

    return entries, ""

def _read_envfile_vars(path: str):
    """
    Best-effort parse for simple EnvironmentFile syntax:
      KEY=value
      KEY="value"
      export KEY=value
    """
    result = {
        "path": path,
        "exists": False,
        "readable": False,
        "access_denied": False,
        "error_kind": "",
        "vars": {},
        "error": "",
    }

    try:
        p = Path(path)
        result["exists"] = p.exists()
        if not p.exists():
            result["error_kind"] = "not_found"
            result["error"] = "file not found"
            return result

        if not p.is_file():
            result["error_kind"] = "not_file"
            result["error"] = "not a file"
            return result

        text = p.read_text(encoding="utf-8", errors="ignore")
        result["readable"] = True
        result["error_kind"] = ""
    except Exception as e:
        err_no = getattr(e, "errno", None)

        if isinstance(e, PermissionError) or err_no in (errno.EACCES, errno.EPERM):
            result["access_denied"] = True
            result["error_kind"] = "permission_denied"
            result["error"] = "permission denied"
            return result

        result["error_kind"] = "read_error"
        result["error"] = str(e)
        return result

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):].strip()

        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue

        key = m.group(1)
        value = m.group(2).strip()

        if not value:
            result["vars"][key] = ""
            continue

        try:
            toks = shlex.split(value, posix=True)
            value = " ".join(toks) if toks else ""
        except Exception:
            if len(value) >= 2 and (
                (value[0] == value[-1] == '"') or
                (value[0] == value[-1] == "'")
            ):
                value = value[1:-1]

        result["vars"][key] = value

    return result

def _resolve_telegram_test_target(cfg, fp=None):
    """
    Resolve Telegram token/chat_ids for --test-telegram-status.

    Order:
      1) current process environment
      2) configured systemd unit -> EnvironmentFile fallback
    """
    alerts_cfg = (cfg.get("alerts") or {})
    tg_cfg = (alerts_cfg.get("telegram") or {})

    token_env = str(tg_cfg.get("token_env", "RUST_WD_TELEGRAM_TOKEN")).strip() or "RUST_WD_TELEGRAM_TOKEN"
    chat_ids_env = str(tg_cfg.get("chat_ids_env", "RUST_WD_TELEGRAM_CHAT_IDS")).strip() or "RUST_WD_TELEGRAM_CHAT_IDS"

    unit_name = str(cfg.get("watchdog_systemd_unit_name") or "rust-watchdog.service").strip()
    service_path = str(cfg.get("watchdog_systemd_service_path") or "/etc/systemd/system/rust-watchdog.service").strip()

    result = {
        "ok": False,
        "token_env": token_env,
        "chat_ids_env": chat_ids_env,
        "token": "",
        "chat_ids": [],
        "source": "",
        "notes": [],
        "errors": [],
        "unit_name": unit_name,
        "service_path": service_path,
        "systemd": {},
        "env_entries": [],
        "env_results": [],
        "permission_denied_env_files": [],
    }

    # 1) current shell/process env
    env_token = (os.getenv(token_env, "") or "").strip()
    env_chat_ids_raw = (os.getenv(chat_ids_env, "") or "").strip()
    env_chat_ids = _parse_int_list_local(env_chat_ids_raw)

    if env_token and env_chat_ids:
        result["ok"] = True
        result["token"] = env_token
        result["chat_ids"] = env_chat_ids
        result["source"] = "current process environment"
        result["notes"].append(
            f"found Telegram credentials in current environment: {token_env}, {chat_ids_env}"
        )
        return result

    if env_token and not env_chat_ids:
        result["notes"].append(
            f"found {token_env} in current environment, but {chat_ids_env} is missing/invalid there"
        )
    elif (not env_token) and env_chat_ids:
        result["notes"].append(
            f"found {chat_ids_env} in current environment, but {token_env} is missing there"
        )
    else:
        result["notes"].append(
            f"Telegram credentials not present in current environment: {token_env}, {chat_ids_env}"
        )

    if not parse_bool(cfg.get("test_telegram_status_try_systemd_env_fallback"), True):
        result["errors"].append("systemd env-file fallback is disabled by config")
        return result

    if parse_bool(cfg.get("test_telegram_status_check_systemd"), True):
        result["systemd"] = _systemd_unit_status(unit_name)

    if not service_path:
        result["errors"].append("systemd service path is empty")
        return result

    if not os.path.exists(service_path):
        result["errors"].append(f"systemd unit file not found: {service_path}")
        return result

    result["notes"].append(f"found systemd unit file: {service_path}")

    env_entries, env_err = _parse_systemd_environment_files(service_path)
    if env_err:
        result["errors"].append(f"could not read systemd unit file: {env_err}")
        return result

    if not env_entries:
        result["errors"].append(
            f"no EnvironmentFile entries found in systemd unit: {service_path}"
        )
        return result

    result["env_entries"] = env_entries
    result["notes"].append(
        "found EnvironmentFile entries: " +
        ", ".join(x["path"] for x in env_entries)
    )

    merged = {}
    readable_paths = []

    for ent in env_entries:
        info = _read_envfile_vars(ent["path"])
        info["optional"] = bool(ent.get("optional"))
        result["env_results"].append(info)

        if info.get("access_denied"):
            result["permission_denied_env_files"].append(info["path"])

        if info["readable"]:
            readable_paths.append(info["path"])
            merged.update(info["vars"])

    token = env_token or str(merged.get(token_env, "") or "").strip()
    chat_ids_raw = env_chat_ids_raw or str(merged.get(chat_ids_env, "") or "").strip()
    chat_ids = env_chat_ids if env_chat_ids else _parse_int_list_local(chat_ids_raw)

    if token and chat_ids:
        result["ok"] = True
        result["token"] = token
        result["chat_ids"] = chat_ids
        if readable_paths:
            result["source"] = f"systemd EnvironmentFile ({', '.join(readable_paths)})"
        else:
            result["source"] = "systemd EnvironmentFile"
        return result

    if result["permission_denied_env_files"]:
        result["errors"].append(
            "systemd EnvironmentFile exists but access was denied to this manual test process: "
            + ", ".join(result["permission_denied_env_files"])
        )

    if not token:
        result["errors"].append(
            f"could not resolve Telegram token variable '{token_env}' "
            f"from current environment or readable systemd EnvironmentFile entries"
        )
    if not chat_ids:
        result["errors"].append(
            f"could not resolve Telegram chat IDs variable '{chat_ids_env}' "
            f"from current environment or readable systemd EnvironmentFile entries"
        )

    return result

def test_telegram_status(cfg, args, fp=None):
    """
    Send a direct Telegram status message and exit.

    This path does NOT depend on AlertManager being enabled.
    It resolves Telegram credentials from:
      1) current process environment
      2) systemd unit EnvironmentFile fallback
    """
    resolved = _resolve_telegram_test_target(cfg, fp=fp)

    token_env = resolved["token_env"]
    chat_ids_env = resolved["chat_ids_env"]

    for note in resolved.get("notes", []):
        log(f"TEST_TELEGRAM_STATUS: {note}", fp)

    systemd_info = resolved.get("systemd") or {}
    if systemd_info.get("checked"):
        log(
            "TEST_TELEGRAM_STATUS: "
            f"systemd unit={systemd_info.get('unit_name')!s} "
            f"known={_bool_tf(systemd_info.get('known'))} "
            f"active={_bool_tf(systemd_info.get('active'))} "
            f"enabled={systemd_info.get('enabled', 'unknown')} "
            f"load_state={systemd_info.get('load_state', 'unknown')} "
            f"sub_state={systemd_info.get('sub_state', 'unknown')}",
            fp
        )
        if systemd_info.get("error"):
            log(f"TEST_TELEGRAM_STATUS: systemd note: {systemd_info['error']}", fp)

    for info in resolved.get("env_results", []):
        path = info.get("path", "")

        if info.get("readable"):
            found_vars = []
            if token_env in info.get("vars", {}):
                found_vars.append(token_env)
            if chat_ids_env in info.get("vars", {}):
                found_vars.append(chat_ids_env)

            if found_vars:
                log(
                    f"TEST_TELEGRAM_STATUS: readable env file: {path} "
                    f"(found {', '.join(found_vars)})",
                    fp
                )
            else:
                log(
                    f"TEST_TELEGRAM_STATUS: readable env file: {path} "
                    f"(Telegram vars not present there)",
                    fp
                )
        elif info.get("access_denied"):
            opt = "optional " if info.get("optional") else ""
            log(
                f"TEST_TELEGRAM_STATUS: {opt}env file found but access denied for this user: {path}",
                fp
            )
        else:
            opt = "optional " if info.get("optional") else ""
            err = info.get("error", "unreadable")
            log(
                f"TEST_TELEGRAM_STATUS: {opt}env file not readable: {path} -- {err}",
                fp
            )

    if not resolved.get("ok"):
        for err in resolved.get("errors", []):
            log(f"TEST_TELEGRAM_STATUS: ERROR: {err}", fp)

        denied_paths = resolved.get("permission_denied_env_files") or []

        if denied_paths:
            log(
                "TEST_TELEGRAM_STATUS: conclusion: this manual test process could not resolve Telegram credentials because access to the systemd EnvironmentFile was denied.",
                fp
            )
            log(
                "TEST_TELEGRAM_STATUS: note: that does NOT mean the running systemd service itself is missing the credentials.",
                fp
            )
            log(
                "TEST_TELEGRAM_STATUS: systemd may already have loaded them successfully at service start.",
                fp
            )
            log(
                f"TEST_TELEGRAM_STATUS: fix for manual testing: either export {token_env} and {chat_ids_env} in this shell before running the test,",
                fp
            )
            log(
                "TEST_TELEGRAM_STATUS: or run the test in a context that is allowed to read the service EnvironmentFile.",
                fp
            )
        else:
            log(
                "TEST_TELEGRAM_STATUS: conclusion: Telegram credentials could not be resolved for this test run.",
                fp
            )
            log(
                f"TEST_TELEGRAM_STATUS: fix: either export {token_env} and {chat_ids_env} in the shell before running this command,",
                fp
            )
            log(
                "TEST_TELEGRAM_STATUS: or put them in the EnvironmentFile used by the watchdog systemd service.",
                fp
            )

        return 2

    try:
        if PROJECT_DIR not in sys.path:
            sys.path.insert(0, PROJECT_DIR)
        from rust_watchdog_alerts import TelegramBackend
    except Exception as e:
        log(f"TEST_TELEGRAM_STATUS: ERROR: could not import Telegram backend: {e}", fp)
        return 2

    live_pid = _read_lockfile_pid(str(cfg.get("lockfile") or ""))
    live_since = _proc_started_at(live_pid) if live_pid else "not running"

    server_dir = str(cfg.get("server_dir") or "")
    rustserver_path = os.path.join(server_dir, "rustserver")
    server_proc = get_server_process_info(cfg)

    def _extract_primary_cause(evidence):
        for line in (evidence or []):
            if isinstance(line, str) and line.startswith("PRIMARY_CAUSE:"):
                return line[len("PRIMARY_CAUSE:"):].strip()
        return ""

    try:
        state, evidence = health_report(cfg, server_dir, rustserver_path, fp=None)
        primary = _extract_primary_cause(evidence)
    except Exception as e:
        state = "UNKNOWN"
        primary = f"health_report failed -- {e}"

    now_s = ts()
    dry_run_s = str(bool(cfg.get("dry_run", False))).lower()

    version = _runtime_version()
    version_label = _runtime_version_label()
    rendered_lines = [
        f"🧪 <b>rust-linuxgsm-watchdog ({html.escape(version_label)}) -- Telegram status test</b>",
        f"<code>time={html.escape(now_s)}</code>",
        f"<code>version={html.escape(version or 'N/A')}</code>",
        f"<code>server_status={html.escape(state)}</code>",
    ]

    if primary and state != "RUNNING":
        rendered_lines.append(f"<code>server_primary={html.escape(primary)}</code>")

    rendered_lines.extend([
        f"<code>watchdog_running={_bool_tf(bool(live_pid))}</code>",
        f"<code>watchdog_pid={live_pid if live_pid else 'not running'}</code>",
        f"<code>watchdog_running_since={html.escape(live_since)}</code>",
        f"<code>dry_run={html.escape(dry_run_s)}</code>",
    ])

    if systemd_info.get("checked"):
        rendered_lines.extend([
            f"<code>systemd_known={_bool_tf(systemd_info.get('known'))}</code>",
            f"<code>systemd_active={_bool_tf(systemd_info.get('active'))}</code>",
        ])

    if resolved.get("source"):
        rendered_lines.append(f"<code>telegram_source={html.escape(str(resolved['source']))}</code>")

    if server_proc.get("running"):
        rendered_lines.extend([
            f"<code>server_exe={html.escape(server_proc.get('exe_name', 'unknown'))}</code>",
            f"<code>server_pid={server_proc.get('pid')}</code>",
            f"<code>server_running_since={html.escape(server_proc.get('started_at', 'unknown'))}</code>",
            f"<code>server_uptime={html.escape(server_proc.get('uptime_human', 'unknown'))}</code>",
        ])
    elif server_proc.get("ambiguous"):
        rendered_lines.extend([
            f"<code>server_pid=ambiguous</code>",
            f"<code>server_matches={server_proc.get('match_count', 0)}</code>",
            f"<code>server_select={html.escape(server_proc.get('selected_by', 'unknown'))}</code>",
        ])
    else:
        rendered_lines.extend([
            f"<code>server_pid=not running</code>",
            f"<code>server_exe=RustDedicated</code>",
        ])

    rendered = "\n".join(rendered_lines)

    tg_cfg = ((cfg.get("alerts") or {}).get("telegram") or {})
    backend = TelegramBackend(
        token=resolved["token"],
        chat_ids=resolved["chat_ids"],
        parse_mode=str(tg_cfg.get("parse_mode", "HTML")),
        disable_web_preview=bool(tg_cfg.get("disable_web_preview", True)),
        timeout_s=int(tg_cfg.get("timeout_s", 8)),
    )

    ok = backend.send(None, rendered)
    if ok:
        log("TEST_TELEGRAM_STATUS: sent OK", fp)
        return 0

    log(
        f"TEST_TELEGRAM_STATUS: send failed: {getattr(backend, 'last_error', 'unknown error')}",
        fp
    )
    return 2

def init_alerts(cfg, fp=None):
    global ALERTS

    enabled = False
    try:
        enabled = (
            parse_bool(cfg.get("alerts_enabled"), False) or
            parse_bool((cfg.get("alerts") or {}).get("enabled"), False)
        )
    except Exception:
        enabled = False

    if not enabled:
        log("ALERTS: disabled", fp)
        return None

    try:
        # Ensure script dir is on sys.path (systemd safe, symlink safe)
        if PROJECT_DIR not in sys.path:
            sys.path.insert(0, PROJECT_DIR)

        from rust_watchdog_alerts import AlertManager  # noqa
    except Exception as e:
        log(f"ALERTS: disabled (import failed): {e}", fp)
        ALERTS = None
        return None

    try:
        ALERTS = AlertManager(
            cfg,
            log_fn=lambda level, msg: log(f"ALERTS: {level}: {msg}", fp)
        )

        if getattr(ALERTS, "enabled", False):
            log("ALERTS: enabled", fp)
        else:
            log("ALERTS: disabled (no usable backends)", fp)

        return ALERTS
    except Exception as e:
        log(f"ALERTS: disabled (init failed): {e}", fp)
        ALERTS = None
        return None

def alert(event: str, message: str = "", level: str = "info", fp=None, **ctx):
    """
    Fire-and-forget alert. Never raises.
    """
    if not ALERTS:
        return
    try:
        fields = dict(ctx or {})
        # Runtime metadata wins over any stale value in a config or call site.
        # The renderer supplies "(N/A)" if this value is empty/unavailable.
        fields["version"] = _runtime_version()
        try:
            footnotes = _build_alert_status_footnotes(
                CFG_FOR_HINTS or {},
                coordinator=STATUS_COORDINATOR,
            )
            if footnotes:
                fields["_footnote_lines"] = footnotes
        except Exception as e:
            log(f"ALERTS: status footnote failed: {e}", fp)

        lvl = str(level or "info").upper()
        ALERTS.emit(
            event=str(event or "event"),
            level=lvl,
            title=str(event or "event"),
            text=str(message or ""),
            **fields
        )
    except Exception as e:
        # don't spam; just one line
        log(f"ALERTS: emit failed: {e}", fp)

# ---------------------------------------------------------
# Optional dependency: websocket-client (for Rust WebRCON)
# ---------------------------------------------------------
_WS_CACHE = {"checked": False, "ok": False, "err": ""}

# rcon endpoint checker
def get_rcon_endpoint(cfg, fp=None, *, need_password=True):
    # 1) autodetect from RustDedicated cmdline
    ip, port, pw = detect_rcon_from_identity(cfg)
    if ip and port and (pw or not need_password):
        return (ip, int(port), pw, "autodetect")

    # 2) fallback to config (support both rcon_ip and old rcon_host)
    ip = (cfg.get("rcon_ip") or cfg.get("rcon_host") or "").strip()
    pw = (cfg.get("rcon_password") or "").strip()
    try:
        port = int(cfg.get("rcon_port", 0))
    except Exception:
        port = 0

    if ip == "0.0.0.0":
        ip = "127.0.0.1"

    if ip and (1 <= port <= 65535) and (pw or not need_password):
        return (ip, port, pw, "config")

    return (None, None, None, "missing")

def websocket_dep_status():
    """
    Returns (ok: bool, err: str).
    Cached so we don't import-spam or repeat warnings every loop.
    """
    if _WS_CACHE["checked"]:
        return (_WS_CACHE["ok"], _WS_CACHE["err"])

    _WS_CACHE["checked"] = True
    try:
        from websocket import create_connection  # noqa: F401
        _WS_CACHE["ok"] = True
        _WS_CACHE["err"] = ""
    except Exception as e:
        _WS_CACHE["ok"] = False
        _WS_CACHE["err"] = str(e)

    return (_WS_CACHE["ok"], _WS_CACHE["err"])

def redact_secrets(s: str) -> str:
    if not s:
        return s
    return RCON_PW_RE.sub(r'\1"<redacted>"', s)

# Set to True when systemd/user requests a stop (SIGTERM/SIGINT)
stop_requested = False

# --------------------------------------------------------
# LOGGING + TIMESTAMPS
# --------------------------------------------------------
def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(line, fp=None):
    msg = f"[{ts()}] {line}"
    print(msg, flush=True)
    if fp:
        fp.write(msg + "\n")
        fp.flush()

# --------------------------------------------------------
# TIMEZONE HELPERS & COUNTERS
# --------------------------------------------------------
def _zoneinfo_available() -> bool:
    return ZoneInfo is not None

def _get_tz(name: str, fp=None):
    """
    Best-effort timezone loader.
    If tzdata is missing on the host, fall back to UTC and log a warning once.
    """
    name = (name or "").strip()
    if not name or not _zoneinfo_available():
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log(f"FORCED_WIPE: WARNING: ZoneInfo '{name}' not found on this host (tzdata missing?). Falling back to UTC.", fp)
        return timezone.utc
    except Exception as e:
        log(f"FORCED_WIPE: WARNING: ZoneInfo error for '{name}': {e}. Falling back to UTC.", fp)
        return timezone.utc

def _human_td(td: timedelta) -> str:
    """
    Human-ish duration, e.g. "2d 3h 14m".
    """
    total = int(td.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d > 0:
        return f"{sign}{d}d {h}h {m}m"
    if h > 0:
        return f"{sign}{h}h {m}m"
    return f"{sign}{m}m"

def _first_thursday_dt(year: int, month: int, *, hour: int, minute: int, tz) -> datetime:
    """
    Return aware datetime for "first Thursday of (year, month) at HH:MM" in tz.
    weekday: Monday=0 ... Sunday=6. Thursday=3.
    """
    d0 = datetime(year, month, 1, hour, minute, tzinfo=tz)
    target = 3  # Thursday
    delta = (target - d0.weekday()) % 7
    return d0 + timedelta(days=delta)

def _forced_wipe_schedule(now_utc: datetime, cfg, fp=None, *, post_window_minutes: int = 0):
    """
    Compute the relevant monthly forced-wipe schedule:
      first Thursday of month @ forced_wipe_hour:forced_wipe_minute in forced_wipe_tz.

    The current month's schedule remains relevant through post_window_minutes.
    This is important after release time: callers can still see and act on the
    current wipe instead of jumping immediately to next month.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    tz_name = str(cfg.get("forced_wipe_tz") or "Europe/London")
    tz = _get_tz(tz_name, fp=fp)

    hour = int(cfg.get("forced_wipe_hour", 19))
    minute = int(cfg.get("forced_wipe_minute", 0))

    now_tz = now_utc.astimezone(tz)
    cand = _first_thursday_dt(now_tz.year, now_tz.month, hour=hour, minute=minute, tz=tz)
    cand_utc = cand.astimezone(timezone.utc)
    keep_until = cand_utc + timedelta(minutes=max(0, int(post_window_minutes)))

    if now_utc > keep_until:
        # next month
        y = now_tz.year
        m = now_tz.month + 1
        if m == 13:
            y += 1
            m = 1
        cand = _first_thursday_dt(y, m, hour=hour, minute=minute, tz=tz)
        cand_utc = cand.astimezone(timezone.utc)

    return {
        "wipe_tz_dt": cand,
        "wipe_utc_dt": cand_utc,
        "tz": tz,
        "tz_name": tz_name,
        "cycle": cand.strftime("%Y-%m"),
    }


def _forced_wipe_calendar_cycle(now_utc: datetime, cfg, fp=None):
    """
    Return this calendar month's forced-wipe schedule in the configured zone.

    Unlike next_forced_wipe(), this deliberately keeps the current month after
    the highlight/action window ends. Persistent state and manual completion
    acknowledgement belong to the monthly cycle, not only to a short release
    window.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    tz_name = str(cfg.get("forced_wipe_tz") or "Europe/London")
    tz = _get_tz(tz_name, fp=fp)
    now_tz = now_utc.astimezone(tz)
    hour = int(cfg.get("forced_wipe_hour", 19))
    minute = int(cfg.get("forced_wipe_minute", 0))
    wipe_tz = _first_thursday_dt(
        now_tz.year,
        now_tz.month,
        hour=hour,
        minute=minute,
        tz=tz,
    )
    return {
        "wipe_tz_dt": wipe_tz,
        "wipe_utc_dt": wipe_tz.astimezone(timezone.utc),
        "tz": tz,
        "tz_name": tz_name,
        "cycle": wipe_tz.strftime("%Y-%m"),
    }


def next_forced_wipe(now_utc: datetime, cfg, fp=None):
    """
    Return the current wipe while its configured highlight window is active,
    otherwise the next scheduled wipe.
    """
    window_m = int(cfg.get("forced_wipe_window_minutes", 180))
    return _forced_wipe_schedule(
        now_utc,
        cfg,
        fp=fp,
        post_window_minutes=max(0, window_m),
    )

def _pick_forced_wipe_interval(cfg, dt_seconds: float) -> int:
    """
    Pick log interval based on cfg["forced_wipe_log_schedule"].
    dt_seconds = (wipe_time_utc - now_utc).total_seconds()
    """
    schedule = cfg.get("forced_wipe_log_schedule")
    if isinstance(schedule, list) and schedule:
        for ent in schedule:
            if not isinstance(ent, dict):
                continue
            interval = ent.get("interval_seconds")
            if interval is None:
                continue

            gt = ent.get("dt_gt_seconds", None)
            lte = ent.get("dt_lte_seconds", None)

            try:
                if gt is not None and not (dt_seconds > float(gt)):
                    continue
                if lte is not None and not (dt_seconds <= float(lte)):
                    continue
                return max(1, int(interval))
            except Exception:
                continue

    # Backwards-compatible fallback if schedule is missing:
    # use old "idle vs active" knobs if present
    idle_i = int(cfg.get("forced_wipe_log_interval_seconds", 3600))
    active_i = int(cfg.get("forced_wipe_log_interval_seconds_active", 300))
    # caller can still decide active vs idle; return idle by default here
    return max(1, idle_i)

def forced_wipe_highlight_log(cfg, fp=None, *, now_utc: datetime = None):
    """
    Emit one status line about next forced wipe.
    Returns (next_log_after_seconds, is_active_window).
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    info = next_forced_wipe(now_utc, cfg, fp=fp)

    wipe_utc = info["wipe_utc_dt"]
    wipe_tz = info["wipe_tz_dt"]

    lead_h = int(cfg.get("forced_wipe_lead_hours", 24))
    window_m = int(cfg.get("forced_wipe_window_minutes", 180))
    lead = timedelta(hours=max(0, lead_h))
    window = timedelta(minutes=max(0, window_m))

    dt = wipe_utc - now_utc
    active = (
    (timedelta(0) < dt <= lead) or
    (dt <= timedelta(0) and (now_utc - wipe_utc) <= window)
    )

    # show also system local time (whatever the host is using)
    wipe_local = wipe_utc.astimezone()

    # Configurable tags
    tag_scheduled = str(cfg.get("forced_wipe_tag_scheduled", "scheduled"))
    tag_soon = str(cfg.get("forced_wipe_tag_soon", "WIPE SOON"))
    tag_window = str(cfg.get("forced_wipe_tag_window", "WIPE WINDOW"))

    # Choose tag (wipe window overrides "soon")
    if dt <= timedelta(0) and (now_utc - wipe_utc) <= window:
        tag = tag_window
    elif active:
        tag = tag_soon
    else:
        tag = tag_scheduled

    # Configurable message template
    template = str(cfg.get(
        "forced_wipe_message_template",
        "FORCED_WIPE: next = {wipe_tz} ({tz_name}) | local={wipe_local} | utc={wipe_utc} | in {eta} | {tag}"
    ))

    wipe_tz_s = wipe_tz.strftime("%Y-%m-%d %H:%M")
    wipe_local_s = wipe_local.strftime("%Y-%m-%d %H:%M %z")
    wipe_utc_s = wipe_utc.strftime("%Y-%m-%d %H:%MZ")

    try:
        msg = template.format(
            wipe_tz=wipe_tz_s,
            tz_name=str(info.get("tz_name", "")),
            wipe_local=wipe_local_s,
            wipe_utc=wipe_utc_s,
            eta=_human_td(dt),
            tag=tag,
        )
    except Exception:
        # Hard fallback if template is broken / missing placeholders
        msg = (
            f"FORCED_WIPE: next = {wipe_tz_s} ({info.get('tz_name','')})"
            f" | local={wipe_local_s}"
            f" | utc={wipe_utc_s}"
            f" | in {_human_td(dt)} | {tag}"
        )

    log(msg, fp)

    interval = _pick_forced_wipe_interval(cfg, dt.total_seconds())

    # If we're using the old knobs (no schedule), keep the old "active vs idle" behavior:
    if not isinstance(cfg.get("forced_wipe_log_schedule"), list):
        idle_i = int(cfg.get("forced_wipe_log_interval_seconds", 3600))
        active_i = int(cfg.get("forced_wipe_log_interval_seconds_active", 300))
        interval = active_i if active else idle_i

    return (max(1, int(interval)), active)

def in_forced_wipe_update_hold(cfg, now_utc: datetime, fp=None):
    """
    True during the pre-wipe hold window (default: last N minutes before wipe).
    Intended to stop update-watch / SmoothRestarter spam right before wipe.
    Returns (hold: bool, reason: str).
    """
    if not parse_bool(cfg.get("forced_wipe_update_hold"), False):
        return (False, "")

    hold_m = int(cfg.get("forced_wipe_update_hold_before_minutes", 360))
    if hold_m <= 0:
        return (False, "")

    # A zero-length post window keeps the current schedule at the exact release
    # instant but moves to next month immediately afterwards.
    info = _forced_wipe_schedule(now_utc, cfg, fp=fp, post_window_minutes=0)
    wipe_utc = info["wipe_utc_dt"]
    dt = wipe_utc - now_utc

    if timedelta(0) < dt <= timedelta(minutes=hold_m):
        when = info["wipe_tz_dt"].strftime("%Y-%m-%d %H:%M")
        return (True, f"within {hold_m}m of wipe ({when} {info.get('tz_name','')})")

    return (False, "")


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc_iso(value: str):
    try:
        s = str(value or "").strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _elapsed_ago(value: str, now_utc: datetime) -> str:
    then = _parse_utc_iso(value)
    if then is None:
        return "unknown"
    seconds = max(0, int((now_utc - then).total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    def unit(n: int, name: str) -> str:
        return f"{n} {name}" + ("" if n == 1 else "s")

    if days:
        return (
            f"{unit(days, 'day')}, {unit(hours, 'hour')}, "
            f"{unit(minutes, 'minute')} ago"
        )
    if hours:
        return f"{unit(hours, 'hour')}, {unit(minutes, 'minute')} ago"
    if minutes:
        return f"{unit(minutes, 'minute')} ago"
    return "less than 1 minute ago"


def _status_timestamp(value: str) -> str:
    parsed = _parse_utc_iso(value)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


class ForcedWipeCoordinator:
    """
    Persistent once-per-cycle forced-wipe state.

    A wipe can become pending when a new remote Steam build is observed
    around/after the scheduled release and a prior remote-build fence exists.
    An explicitly enabled calendar fallback can also arm at the end of the
    action window, but only if this watchdog observed the cycle before that
    cutoff. Once wipe_done is saved, retries may start the server but never wipe
    again.
    """

    VALID_ACTIONS = ("off", "map-wipe", "full-wipe")
    VALID_TRIGGERS = ("new-build-after-schedule",)

    def __init__(self, cfg: dict, fp=None, *, persist: bool = True):
        self.cfg = cfg
        self.fp = fp
        self.action = str(cfg.get("forced_wipe_action", "off")).strip().lower()
        self.trigger = str(
            cfg.get("forced_wipe_trigger", "new-build-after-schedule")
        ).strip().lower()
        self.state_path = str(cfg.get("forced_wipe_state_file") or "").strip()
        self.persist = bool(persist and self.state_path)
        self.last_save_ok = True
        self.state = self._empty_state()
        self._load()

    @property
    def enabled(self) -> bool:
        return self.action in ("map-wipe", "full-wipe")

    @staticmethod
    def _empty_state() -> dict:
        return {
            "schema_version": 4,
            "cycle": "",
            "scheduled_utc": "",
            "cycle_first_observed_at": "",
            "cycle_last_observed_at": "",
            "prewipe_remote_build": "",
            "candidate_remote_build": "",
            "candidate_seen_at": "",
            "latest_local_build": "",
            "latest_remote_build": "",
            "latest_update_verdict": "",
            "latest_build_seen_at": "",
            "armed_action": "",
            "armed_trigger": "",
            "pending": False,
            "started_at": "",
            "wipe_started_at": "",
            "wipe_done": False,
            "wipe_done_at": "",
            "start_done": False,
            "completed": False,
            "completed_at": "",
            "completion_source": "",
            "last_wipe_at": "",
            "last_wipe_source": "",
            "last_wipe_kind": "",
            "last_restart_at": "",
            "last_restart_source": "",
            "reminder_last_sent_at": "",
            "failed_step": "",
            "last_error": "",
            "updated_at": "",
        }

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                state = self._empty_state()
                state.update(obj)
                state["schema_version"] = 4
                for key in (
                    "pending",
                    "wipe_done",
                    "start_done",
                    "completed",
                ):
                    if not isinstance(state.get(key), bool):
                        state[key] = False
                for key in (
                    "cycle",
                    "scheduled_utc",
                    "cycle_first_observed_at",
                    "cycle_last_observed_at",
                    "prewipe_remote_build",
                    "candidate_remote_build",
                    "candidate_seen_at",
                    "latest_local_build",
                    "latest_remote_build",
                    "latest_update_verdict",
                    "latest_build_seen_at",
                    "armed_action",
                    "armed_trigger",
                    "started_at",
                    "wipe_started_at",
                    "wipe_done_at",
                    "completed_at",
                    "completion_source",
                    "last_wipe_at",
                    "last_wipe_source",
                    "last_wipe_kind",
                    "last_restart_at",
                    "last_restart_source",
                    "reminder_last_sent_at",
                    "failed_step",
                    "last_error",
                    "updated_at",
                ):
                    if not isinstance(state.get(key), str):
                        state[key] = ""

                if (
                    state.get("pending")
                    and not state.get("armed_trigger")
                    and state.get("candidate_remote_build")
                ):
                    state["armed_trigger"] = "build-change"
                pending_valid = bool(
                    re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", state["cycle"])
                    and _parse_utc_iso(state["scheduled_utc"])
                    and state["armed_action"] in ("map-wipe", "full-wipe")
                    and (
                        state["candidate_remote_build"]
                        or state["armed_trigger"] == "window-end-fallback"
                    )
                )
                if state["pending"] and not pending_valid:
                    log(
                        "FORCED_WIPE: invalid pending state ignored; "
                        "required cycle/schedule/trigger/action fields are missing",
                        self.fp,
                    )
                    ledger = {
                        key: state.get(key, "")
                        for key in (
                            "last_wipe_at",
                            "last_wipe_source",
                            "last_wipe_kind",
                            "last_restart_at",
                            "last_restart_source",
                        )
                    }
                    state = self._empty_state()
                    state.update(ledger)
                if (
                    state.get("completed")
                    and not state.get("last_wipe_at")
                ):
                    state["last_wipe_at"] = str(
                        state.get("wipe_done_at")
                        or state.get("completed_at")
                        or ""
                    )
                    state["last_wipe_source"] = str(
                        state.get("completion_source") or "legacy"
                    )
                    state["last_wipe_kind"] = str(
                        state.get("armed_action") or "unknown"
                    )
                self.state = state
        except FileNotFoundError:
            return
        except Exception as e:
            log(f"FORCED_WIPE: state load failed; using empty state: {e}", self.fp)

    def _save(self, now_utc: datetime = None) -> bool:
        if not self.persist:
            self.last_save_ok = True
            return True
        try:
            now_utc = now_utc or datetime.now(timezone.utc)
            self.state["updated_at"] = _utc_iso(now_utc)
            parent = os.path.dirname(os.path.abspath(self.state_path)) or "."
            os.makedirs(parent, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, self.state_path)
            try:
                dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                # The file itself was fsynced and atomically replaced. Some
                # filesystems do not support directory fsync.
                pass
            self.last_save_ok = True
            return True
        except Exception as e:
            self.last_save_ok = False
            log(f"FORCED_WIPE: state save failed: {e}", self.fp)
            return False

    def _stored_unfinished_schedule(self):
        if not self.state.get("pending") or self.state.get("completed"):
            return None
        scheduled = _parse_utc_iso(self.state.get("scheduled_utc", ""))
        cycle = str(self.state.get("cycle") or "")
        if not scheduled or not cycle:
            return None
        tz_name = str(self.cfg.get("forced_wipe_tz") or "Europe/London")
        tz = _get_tz(tz_name, fp=self.fp)
        return {
            "wipe_utc_dt": scheduled,
            "wipe_tz_dt": scheduled.astimezone(tz),
            "tz": tz,
            "tz_name": tz_name,
            "cycle": cycle,
        }

    def _schedule(self, now_utc: datetime):
        unfinished = self._stored_unfinished_schedule()
        if unfinished:
            return unfinished
        return _forced_wipe_calendar_cycle(now_utc, self.cfg, fp=self.fp)

    def _ensure_cycle(self, now_utc: datetime):
        info = self._schedule(now_utc)
        cycle = str(info["cycle"])
        if str(self.state.get("cycle") or "") != cycle:
            last_wipe = {
                key: str(self.state.get(key) or "")
                for key in (
                    "last_wipe_at",
                    "last_wipe_source",
                    "last_wipe_kind",
                    "last_restart_at",
                    "last_restart_source",
                )
            }
            self.state = self._empty_state()
            self.state.update(last_wipe)
            self.state["cycle"] = cycle
            self.state["scheduled_utc"] = _utc_iso(info["wipe_utc_dt"])
            self._save(now_utc)
        elif not self.state.get("scheduled_utc"):
            self.state["scheduled_utc"] = _utc_iso(info["wipe_utc_dt"])
            self._save(now_utc)
        return info

    def _record_cycle_observation(self, now_utc: datetime) -> None:
        observed_at = _utc_iso(now_utc)
        changed = False
        if not self.state.get("cycle_first_observed_at"):
            self.state["cycle_first_observed_at"] = observed_at
            changed = True
        if self.state.get("cycle_last_observed_at") != observed_at:
            self.state["cycle_last_observed_at"] = observed_at
            changed = True
        if changed:
            self._save(now_utc)

    def _wipe_recorded_for_cycle(self, info: dict) -> bool:
        if self.state.get("wipe_done") or self.state.get("completed"):
            return True
        last_wipe = _parse_utc_iso(str(self.state.get("last_wipe_at") or ""))
        if last_wipe is None:
            return False
        wipe_tz = info["wipe_tz_dt"]
        return last_wipe.astimezone(info["tz"]).date() == wipe_tz.date()

    def _maybe_arm_window_end_fallback(
        self,
        info: dict,
        now_utc: datetime,
    ) -> tuple:
        if (
            not self.enabled
            or not parse_bool(
                self.cfg.get("forced_wipe_fallback_at_window_end"),
                False,
            )
            or self.state.get("pending")
            or self._wipe_recorded_for_cycle(info)
        ):
            return (False, "")

        action_window_m = max(
            1,
            int(self.cfg.get("forced_wipe_action_window_minutes", 360)),
        )
        action_end = info["wipe_utc_dt"] + timedelta(
            minutes=action_window_m
        )
        if now_utc < action_end:
            return (False, "")

        first_observed = _parse_utc_iso(
            str(
                self.state.get("cycle_first_observed_at")
                or self.state.get("latest_build_seen_at")
                or ""
            )
        )
        if first_observed is None or first_observed > action_end:
            return (
                False,
                "window-end fallback refused: this cycle was not observed "
                "before the cutoff",
            )

        self.state["pending"] = True
        self.state["armed_action"] = self.action
        self.state["armed_trigger"] = "window-end-fallback"
        self.state["candidate_seen_at"] = _utc_iso(now_utc)
        self.state["candidate_remote_build"] = ""
        self.state["failed_step"] = ""
        self.state["last_error"] = ""
        if not self._save(now_utc):
            self.state["pending"] = False
            self.state["armed_action"] = ""
            self.state["armed_trigger"] = ""
            self.state["candidate_seen_at"] = ""
            return (False, "window-end fallback could not be persisted")
        return (
            True,
            f"no wipe recorded by the {action_window_m}m action-window cutoff",
        )

    def observe_update(self, result: UpdateCheckResult, now_utc: datetime) -> ForcedWipeDecision:
        reminder_enabled = parse_bool(
            self.cfg.get("forced_wipe_reminder_enabled"), True
        )
        if (
            not self.enabled
            and not reminder_enabled
        ) or self.trigger not in self.VALID_TRIGGERS:
            return ForcedWipeDecision(enabled=False)

        info = self._ensure_cycle(now_utc)
        scheduled = info["wipe_utc_dt"]
        cycle = str(info["cycle"])
        self._record_cycle_observation(now_utc)
        remote = str(result.remote_build or "").strip()
        local = str(result.local_build or "").strip()

        verdict_label = (
            "available"
            if result.verdict is True
            else "current"
            if result.verdict is False
            else "unknown"
        )
        build_state_changed = False
        for key, value in (
            ("latest_local_build", local),
            ("latest_remote_build", remote),
            ("latest_update_verdict", verdict_label),
        ):
            if value and self.state.get(key) != value:
                self.state[key] = value
                build_state_changed = True
        if local or remote:
            seen_at = _utc_iso(now_utc)
            if self.state.get("latest_build_seen_at") != seen_at:
                self.state["latest_build_seen_at"] = seen_at
                build_state_changed = True
        if build_state_changed:
            self._save(now_utc)

        base = {
            "enabled": self.enabled,
            "cycle": cycle,
            "scheduled_utc": _utc_iso(scheduled),
            "pending": bool(self.state.get("pending")),
            "candidate_remote_build": str(self.state.get("candidate_remote_build") or ""),
            "armed_trigger": str(self.state.get("armed_trigger") or ""),
        }

        if not self.enabled:
            return ForcedWipeDecision(**base)

        if not remote:
            fallback_armed, fallback_reason = (
                self._maybe_arm_window_end_fallback(info, now_utc)
            )
            pending = bool(self.state.get("pending"))
            return ForcedWipeDecision(
                enabled=self.enabled,
                cycle=cycle,
                scheduled_utc=_utc_iso(scheduled),
                armed_now=fallback_armed,
                pending=pending,
                action_due=bool(
                    pending
                    and not self.state.get("completed")
                    and now_utc >= scheduled
                ),
                hold=bool(pending and now_utc < scheduled),
                reason=(
                    "armed state retained despite missing build IDs"
                    if pending
                    else fallback_reason
                ),
                candidate_remote_build=str(
                    self.state.get("candidate_remote_build") or ""
                ),
                armed_trigger=str(self.state.get("armed_trigger") or ""),
            )

        if self.state.get("completed"):
            return ForcedWipeDecision(**base)

        tolerance_m = max(
            0, int(self.cfg.get("forced_wipe_early_release_tolerance_minutes", 15))
        )
        action_window_m = max(
            1, int(self.cfg.get("forced_wipe_action_window_minutes", 360))
        )
        earliest_candidate = scheduled - timedelta(minutes=tolerance_m)
        action_end = scheduled + timedelta(minutes=action_window_m)
        armed_now = False
        reason = ""

        if now_utc < earliest_candidate:
            if self.state.get("prewipe_remote_build") != remote:
                self.state["prewipe_remote_build"] = remote
                self._save(now_utc)
        elif now_utc <= action_end and not self.state.get("wipe_done"):
            baseline = str(self.state.get("prewipe_remote_build") or "")
            if not baseline:
                # Starting inside the release window is ambiguous. Establish a
                # fence but do not turn a possibly old update into a wipe.
                self.state["prewipe_remote_build"] = remote
                self._save(now_utc)
                reason = "no pre-release build fence; refusing to arm"
            elif result.verdict is True and remote != baseline:
                if not self.state.get("pending"):
                    armed_now = True
                    self.state["pending"] = True
                    self.state["candidate_seen_at"] = _utc_iso(now_utc)
                    self.state["armed_action"] = self.action
                    self.state["armed_trigger"] = "build-change"
                self.state["candidate_remote_build"] = remote
                self.state["failed_step"] = ""
                self.state["last_error"] = ""
                self._save(now_utc)
                reason = f"remote build changed {baseline} -> {remote}"

        if not self.state.get("pending"):
            fallback_armed, fallback_reason = (
                self._maybe_arm_window_end_fallback(info, now_utc)
            )
            if fallback_armed:
                armed_now = True
            if fallback_reason:
                reason = fallback_reason

        pending = bool(self.state.get("pending"))
        action_due = pending and not self.state.get("completed") and now_utc >= scheduled
        prewipe_hold, hold_reason = in_forced_wipe_update_hold(
            self.cfg, now_utc, fp=self.fp
        )
        hold = bool((pending and now_utc < scheduled) or (result.verdict is True and prewipe_hold))
        if hold and not reason:
            reason = hold_reason or "candidate observed before scheduled release time"

        return ForcedWipeDecision(
            enabled=self.enabled,
            cycle=cycle,
            scheduled_utc=_utc_iso(scheduled),
            armed_now=armed_now,
            pending=pending,
            action_due=action_due,
            hold=hold,
            reason=reason,
            candidate_remote_build=str(self.state.get("candidate_remote_build") or ""),
            armed_trigger=str(self.state.get("armed_trigger") or ""),
        )

    def needs_recovery(self, now_utc: datetime) -> bool:
        if not self.enabled:
            return False
        info = self._ensure_cycle(now_utc)
        return bool(
            self.state.get("pending")
            and not self.state.get("completed")
            and now_utc >= info["wipe_utc_dt"]
        )

    def mark_started(self, now_utc: datetime) -> bool:
        changed = not bool(self.state.get("started_at"))
        if changed:
            self.state["started_at"] = _utc_iso(now_utc)
            self._save(now_utc)
        return changed

    def mark_wipe_done(self, now_utc: datetime) -> bool:
        self.state["wipe_done"] = True
        self.state["wipe_done_at"] = _utc_iso(now_utc)
        self.state["last_wipe_at"] = _utc_iso(now_utc)
        self.state["last_wipe_source"] = "automatic"
        self.state["last_wipe_kind"] = str(
            self.state.get("armed_action") or self.action or "unknown"
        )
        self.state["failed_step"] = ""
        self.state["last_error"] = ""
        return self._save(now_utc)

    def mark_wipe_started(self, now_utc: datetime) -> bool:
        self.state["wipe_started_at"] = _utc_iso(now_utc)
        return self._save(now_utc)

    def mark_start_done(self, now_utc: datetime) -> None:
        self.state["start_done"] = True
        self._save(now_utc)

    def observe_server_wipe(
        self,
        wiped_at: str,
        now_utc: datetime,
        *,
        source: str = "rcon-save-created",
    ) -> bool:
        """
        Reconcile a wipe timestamp observed from the running Rust server.

        SaveCreatedTime proves when the current map/save was created, but it
        cannot distinguish map-wipe from full-wipe. Explicit manual/automatic
        metadata is therefore retained when the timestamp is already known.
        """
        parsed = _parse_utc_iso(str(wiped_at or ""))
        if parsed is None or parsed > now_utc + timedelta(minutes=1):
            return False

        info = self._ensure_cycle(now_utc)
        normalized = _utc_iso(parsed)
        existing = _parse_utc_iso(
            str(self.state.get("last_wipe_at") or "")
        )
        changed = False

        explicit_kind = str(
            self.state.get("last_wipe_kind") or ""
        ).strip().lower()
        explicit_source = str(
            self.state.get("last_wipe_source") or ""
        ).strip()
        same_explicit_wipe = bool(
            existing is not None
            and parsed > existing
            and (parsed - existing) <= timedelta(minutes=15)
            and explicit_kind in ("map-wipe", "full-wipe")
            and explicit_source
        )

        if existing is None or parsed > existing:
            self.state["last_wipe_at"] = normalized
            if not same_explicit_wipe:
                self.state["last_wipe_source"] = str(
                    source or "rcon-save-created"
                )
                self.state["last_wipe_kind"] = "unknown"
            changed = True
        elif parsed == existing:
            if not self.state.get("last_wipe_source"):
                self.state["last_wipe_source"] = str(
                    source or "rcon-save-created"
                )
                changed = True
            if not self.state.get("last_wipe_kind"):
                self.state["last_wipe_kind"] = "unknown"
                changed = True
        else:
            # Never replace a newer explicit/persisted observation with an
            # older save timestamp (for example after restoring a backup).
            return False

        tolerance_m = max(
            0,
            int(
                self.cfg.get(
                    "forced_wipe_early_release_tolerance_minutes",
                    15,
                )
            ),
        )
        completes_cycle = bool(
            parsed.astimezone(info["tz"]).date()
            == info["wipe_tz_dt"].date()
            or parsed
            >= info["wipe_utc_dt"] - timedelta(minutes=tolerance_m)
        )
        if completes_cycle:
            completion_source = str(source or "rcon-save-created")
            cycle_updates = {
                "pending": False,
                "wipe_done": True,
                "wipe_done_at": normalized,
                "start_done": True,
                "completed": True,
                "completed_at": _utc_iso(now_utc),
                "completion_source": completion_source,
                "failed_step": "",
                "last_error": "",
            }
            for key, value in cycle_updates.items():
                if self.state.get(key) != value:
                    self.state[key] = value
                    changed = True

        if not changed:
            return False
        return self._save(now_utc)

    def observe_server_restart(
        self,
        restarted_at: str,
        now_utc: datetime,
        *,
        source: str = "process-observed",
    ) -> bool:
        parsed = _parse_utc_iso(restarted_at)
        if parsed is None:
            return False
        if parsed > now_utc + timedelta(minutes=1):
            return False
        normalized = _utc_iso(parsed)
        if (
            self.state.get("last_restart_at") == normalized
            and self.state.get("last_restart_source") == source
        ):
            return False
        self.state["last_restart_at"] = normalized
        self.state["last_restart_source"] = str(source or "process-observed")
        return self._save(now_utc)

    def mark_failed(self, step: str, error: str, now_utc: datetime) -> None:
        self.state["failed_step"] = str(step or "")
        self.state["last_error"] = str(error or "")[:1000]
        self._save(now_utc)

    def finish_if_running(self, now_utc: datetime) -> bool:
        self._ensure_cycle(now_utc)
        if (
            self.state.get("pending")
            and self.state.get("wipe_done")
            and not self.state.get("completed")
        ):
            self.state["pending"] = False
            self.state["completed"] = True
            self.state["completed_at"] = _utc_iso(now_utc)
            self.state["completion_source"] = "automatic"
            self.state["failed_step"] = ""
            self.state["last_error"] = ""
            self._save(now_utc)
            return True
        return False

    def reminder_status(self, now_utc: datetime) -> dict:
        info = self._ensure_cycle(now_utc)
        enabled = parse_bool(
            self.cfg.get("forced_wipe_reminder_enabled"), True
        )
        try:
            repeat_m = max(
                1,
                int(self.cfg.get("forced_wipe_reminder_repeat_minutes", 30)),
            )
        except Exception:
            repeat_m = 30

        scheduled = info["wipe_utc_dt"]
        last_wipe = _parse_utc_iso(
            str(self.state.get("last_wipe_at") or "")
        )
        wiped_for_cycle = bool(last_wipe and last_wipe >= scheduled)
        last_sent = _parse_utc_iso(
            str(self.state.get("reminder_last_sent_at") or "")
        )
        due = bool(
            enabled
            and not self.enabled
            and now_utc >= scheduled
            and not self.state.get("completed")
            and not wiped_for_cycle
        )
        send_due = bool(
            due
            and (
                last_sent is None
                or (now_utc - last_sent) >= timedelta(minutes=repeat_m)
            )
        )
        return {
            "enabled": enabled,
            "due": due,
            "send_due": send_due,
            "repeat_minutes": repeat_m,
            "cycle": str(info["cycle"]),
            "scheduled_utc": _utc_iso(scheduled),
            "wipe_tz": info["wipe_tz_dt"].strftime("%Y-%m-%d %H:%M"),
            "tz_name": str(info.get("tz_name") or ""),
            "action": self.action,
            "last_sent_at": str(
                self.state.get("reminder_last_sent_at") or ""
            ),
            "latest_local_build": str(
                self.state.get("latest_local_build") or ""
            ),
            "latest_remote_build": str(
                self.state.get("latest_remote_build") or ""
            ),
            "latest_update_verdict": str(
                self.state.get("latest_update_verdict") or ""
            ),
            "last_wipe_at": str(self.state.get("last_wipe_at") or ""),
            "last_wipe_age": _elapsed_ago(
                str(self.state.get("last_wipe_at") or ""),
                now_utc,
            ),
            "last_wipe_summary": (
                f"{_status_timestamp(str(self.state.get('last_wipe_at') or ''))} "
                f"({_elapsed_ago(str(self.state.get('last_wipe_at') or ''), now_utc)})"
                if self.state.get("last_wipe_at")
                else "unknown (no wipe timestamp recorded)"
            ),
        }

    def render_reminder(self, status: dict) -> str:
        template = str(
            self.cfg.get(
                "forced_wipe_reminder_message_template",
                "⚠️ FORCED WIPE DUE: cycle {cycle} entered its scheduled "
                "wipe window at {wipe_tz} ({tz_name}); no completed wipe "
                "is recorded and forced_wipe_action={action}.",
            )
        )
        try:
            return template.format(**status)
        except Exception:
            return (
                f"⚠️ FORCED WIPE DUE: cycle {status.get('cycle', '?')} "
                f"entered its scheduled wipe window at "
                f"{status.get('wipe_tz', '?')} "
                f"({status.get('tz_name', '?')}); no completed wipe is "
                f"recorded and forced_wipe_action={self.action}."
            )

    def mark_reminder_sent(self, now_utc: datetime) -> bool:
        self.state["reminder_last_sent_at"] = _utc_iso(now_utc)
        return self._save(now_utc)

    def mark_manual_complete(
        self,
        now_utc: datetime,
        *,
        wiped_at: datetime = None,
        wipe_kind: str = "unknown",
    ) -> dict:
        info = self._ensure_cycle(now_utc)
        wiped_at = wiped_at or now_utc
        if wiped_at.tzinfo is None:
            wiped_at = wiped_at.replace(tzinfo=timezone.utc)
        wiped_at = wiped_at.astimezone(timezone.utc)
        if wiped_at > now_utc + timedelta(minutes=1):
            raise ValueError("wipe timestamp cannot be in the future")
        wipe_kind = str(wipe_kind or "unknown").strip().lower()
        if wipe_kind not in ("unknown", "map-wipe", "full-wipe"):
            raise ValueError(
                "wipe_kind must be unknown, map-wipe, or full-wipe"
            )

        self.state["last_wipe_at"] = _utc_iso(wiped_at)
        self.state["last_wipe_source"] = "manual"
        self.state["last_wipe_kind"] = wipe_kind

        tolerance_m = max(
            0,
            int(
                self.cfg.get(
                    "forced_wipe_early_release_tolerance_minutes",
                    15,
                )
            ),
        )
        completes_cycle = bool(
            wiped_at.astimezone(info["tz"]).date()
            == info["wipe_tz_dt"].date()
            or wiped_at
            >= info["wipe_utc_dt"] - timedelta(minutes=tolerance_m)
        )
        if completes_cycle:
            self.state["pending"] = False
            self.state["completed"] = True
            self.state["completed_at"] = _utc_iso(now_utc)
            self.state["completion_source"] = "manual"
        self.state["failed_step"] = ""
        self.state["last_error"] = ""
        if not self._save(now_utc):
            raise RuntimeError("could not persist forced-wipe completion")
        return {
            "cycle": str(info["cycle"]),
            "scheduled_utc": _utc_iso(info["wipe_utc_dt"]),
            "last_wipe_at": self.state["last_wipe_at"],
            "last_wipe_age": _elapsed_ago(
                self.state["last_wipe_at"],
                now_utc,
            ),
            "last_wipe_kind": self.state["last_wipe_kind"],
            "completed_cycle": completes_cycle,
        }

    def status(self, now_utc: datetime) -> dict:
        info = self._ensure_cycle(now_utc)
        out = dict(self.state)
        out.update(
            {
                "enabled": self.enabled,
                "action": self.action,
                "trigger": self.trigger,
                "fallback_at_window_end": parse_bool(
                    self.cfg.get("forced_wipe_fallback_at_window_end"),
                    False,
                ),
                "action_window_ends_utc": _utc_iso(
                    info["wipe_utc_dt"]
                    + timedelta(
                        minutes=max(
                            1,
                            int(
                                self.cfg.get(
                                    "forced_wipe_action_window_minutes",
                                    360,
                                )
                            ),
                        )
                    )
                ),
                "cycle": str(info["cycle"]),
                "scheduled_utc": _utc_iso(info["wipe_utc_dt"]),
                "state_file": self.state_path,
            }
        )
        reminder = self.reminder_status(now_utc)
        out.update(
            {
                "reminder_enabled": reminder["enabled"],
                "reminder_due": reminder["due"],
                "reminder_repeat_minutes": reminder["repeat_minutes"],
            }
        )
        return out


def _refresh_server_restart_ledger(
    cfg: dict,
    coordinator: ForcedWipeCoordinator = None,
    *,
    now_utc: datetime = None,
) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    process_info = get_server_process_info(cfg)
    restarted_at = str(process_info.get("started_at_utc") or "")
    if restarted_at and coordinator is not None:
        coordinator.observe_server_restart(
            restarted_at,
            now_utc,
            source="rust-process-start",
        )
    return restarted_at


def _refresh_server_wipe_ledger_from_rcon(
    cfg: dict,
    coordinator: ForcedWipeCoordinator,
    *,
    now_utc: datetime = None,
    fp=None,
    log_failure: bool = False,
) -> tuple:
    """
    Query `serverinfo` and reconcile its nested SaveCreatedTime value.

    Returns (ok, normalized_timestamp, changed_or_error).
    """
    if not parse_bool(cfg.get("wipe_timestamp_rcon_enabled"), True):
        return (False, "", "disabled")
    if coordinator is None:
        return (False, "", "coordinator unavailable")

    now_utc = now_utc or datetime.now(timezone.utc)
    ok, response = rcon_send(cfg, "serverinfo", fp=fp)
    if not ok:
        error = str(response or "RCON serverinfo failed")
        if log_failure:
            log(
                "WIPE_TIMESTAMP: RCON serverinfo unavailable: "
                f"{error}",
                fp,
            )
        return (False, "", error)

    wiped_at = extract_serverinfo_save_created_time(response)
    if not wiped_at:
        error = "serverinfo response has no valid SaveCreatedTime"
        if log_failure:
            log(f"WIPE_TIMESTAMP: {error}", fp)
        return (False, "", error)

    parsed = _parse_utc_iso(wiped_at)
    if parsed is None or parsed > now_utc + timedelta(minutes=1):
        error = f"invalid or future SaveCreatedTime: {wiped_at}"
        if log_failure:
            log(f"WIPE_TIMESTAMP: {error}", fp)
        return (False, "", error)

    changed = coordinator.observe_server_wipe(
        wiped_at,
        now_utc,
        source="rcon-save-created",
    )
    if changed:
        log(
            "WIPE_TIMESTAMP: reconciled RCON "
            f"serverinfo.SaveCreatedTime={wiped_at}",
            fp,
        )
    return (True, wiped_at, changed)


def _linuxgsm_server_identity_dir(cfg: dict) -> str:
    server_dir = str(cfg.get("server_dir") or "").strip()
    identity = str(cfg.get("identity") or "").strip()
    if not server_dir or not identity:
        return ""
    return os.path.join(
        os.path.abspath(os.path.expanduser(server_dir)),
        "serverfiles",
        "server",
        identity,
    )


def _filesystem_map_wipe_timestamp(cfg: dict) -> tuple:
    """
    Return the newest LinuxGSM identity-directory .map mtime and its path.

    LinuxGSM deletes every .map and .sav* file for both map and full wipes.
    A recreated .map file normally remains unchanged during play, unlike the
    active .sav file. The .map mtime is therefore the conservative filesystem
    fallback when authenticated RCON cannot provide SaveCreatedTime.
    """
    identity_dir = _linuxgsm_server_identity_dir(cfg)
    if not identity_dir:
        return ("", "server identity directory unavailable")
    if not os.path.isdir(identity_dir):
        return ("", f"server identity directory not found: {identity_dir}")

    newest = None
    try:
        for root, _dirs, names in os.walk(identity_dir):
            for name in names:
                if not name.lower().endswith(".map"):
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = float(os.stat(path).st_mtime)
                except OSError:
                    continue
                if newest is None or mtime > newest[0]:
                    newest = (mtime, path)
    except OSError as e:
        return ("", f"could not scan {identity_dir}: {e}")

    if newest is None:
        return ("", f"no .map file found under {identity_dir}")
    try:
        wiped_at = datetime.fromtimestamp(
            newest[0],
            tz=timezone.utc,
        )
    except (OSError, OverflowError, ValueError) as e:
        return ("", f"invalid .map mtime for {newest[1]}: {e}")
    return (_utc_iso(wiped_at), newest[1])


def _refresh_server_wipe_ledger_from_filesystem(
    cfg: dict,
    coordinator: ForcedWipeCoordinator,
    *,
    now_utc: datetime = None,
    fp=None,
    log_failure: bool = False,
) -> tuple:
    """
    Reconcile the newest stable LinuxGSM .map mtime as a fallback source.

    Returns (ok, normalized_timestamp, changed_or_error).
    """
    if not parse_bool(
        cfg.get("wipe_timestamp_filesystem_fallback_enabled"),
        True,
    ):
        return (False, "", "disabled")
    if coordinator is None:
        return (False, "", "coordinator unavailable")

    now_utc = now_utc or datetime.now(timezone.utc)
    wiped_at, detail = _filesystem_map_wipe_timestamp(cfg)
    if not wiped_at:
        if log_failure:
            log(f"WIPE_TIMESTAMP: filesystem fallback unavailable: {detail}", fp)
        return (False, "", detail)

    parsed = _parse_utc_iso(wiped_at)
    if parsed is None or parsed > now_utc + timedelta(minutes=1):
        error = f"invalid or future .map mtime: {wiped_at} ({detail})"
        if log_failure:
            log(f"WIPE_TIMESTAMP: {error}", fp)
        return (False, "", error)

    changed = coordinator.observe_server_wipe(
        wiped_at,
        now_utc,
        source="filesystem-map-mtime",
    )
    if changed:
        log(
            "WIPE_TIMESTAMP: reconciled LinuxGSM "
            f".map mtime={wiped_at} ({detail})",
            fp,
        )
    return (True, wiped_at, changed)


def _refresh_server_wipe_ledger(
    cfg: dict,
    coordinator: ForcedWipeCoordinator,
    *,
    now_utc: datetime = None,
    fp=None,
    log_failure: bool = False,
) -> tuple:
    """Use authenticated RCON first, then the LinuxGSM filesystem fallback."""
    if parse_bool(cfg.get("wipe_timestamp_rcon_enabled"), True):
        rcon_result = _refresh_server_wipe_ledger_from_rcon(
            cfg,
            coordinator,
            now_utc=now_utc,
            fp=fp,
            log_failure=False,
        )
        if rcon_result[0]:
            return rcon_result
        if log_failure:
            log(
                "WIPE_TIMESTAMP: RCON serverinfo unavailable "
                f"({rcon_result[2]}); trying filesystem fallback",
                fp,
            )

    filesystem_result = _refresh_server_wipe_ledger_from_filesystem(
        cfg,
        coordinator,
        now_utc=now_utc,
        fp=fp,
        log_failure=log_failure,
    )
    return filesystem_result


def _load_status_ledger(cfg: dict) -> dict:
    path = str(cfg.get("forced_wipe_state_file") or "").strip()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _wipe_source_label(source: str) -> str:
    normalized = str(source or "").strip()
    labels = {
        "rcon-save-created": "RCON",
        "filesystem-map-mtime": "map file mtime",
        "manual": "manual record",
        "automatic": "watchdog automatic wipe",
    }
    return labels.get(normalized.lower(), normalized)


def _build_alert_status_footnotes(
    cfg: dict,
    *,
    coordinator: ForcedWipeCoordinator = None,
    now_utc: datetime = None,
) -> list:
    alerts_cfg = cfg.get("alerts") if isinstance(cfg.get("alerts"), dict) else {}
    options = (
        alerts_cfg.get("status_footnote")
        if isinstance(alerts_cfg.get("status_footnote"), dict)
        else {}
    )
    if not parse_bool(options.get("enabled"), True):
        return []

    include_wipe = parse_bool(options.get("include_last_wipe"), True)
    include_restart = parse_bool(options.get("include_last_restart"), True)
    if not include_wipe and not include_restart:
        return []

    now_utc = now_utc or datetime.now(timezone.utc)
    state = coordinator.state if coordinator is not None else _load_status_ledger(cfg)

    current_restart = ""
    if include_restart:
        current_restart = _refresh_server_restart_ledger(
            cfg,
            coordinator,
            now_utc=now_utc,
        )
        if coordinator is not None:
            state = coordinator.state

    lines = []
    if include_wipe:
        wiped_at = str(state.get("last_wipe_at") or "")
        rendered_at = _status_timestamp(wiped_at)
        if rendered_at:
            source_label = _wipe_source_label(
                str(state.get("last_wipe_source") or "")
            )
            source_suffix = f" ({source_label})" if source_label else ""
            lines.append("Server last wiped:")
            lines.append(f"{rendered_at}{source_suffix}")
            lines.append(f"({_elapsed_ago(wiped_at, now_utc)})")
        else:
            unknown = str(
                options.get(
                    "unknown_wipe_text",
                    "unknown (no wipe timestamp recorded)",
                )
            ).strip()
            lines.append("Server last wiped:")
            lines.append(unknown)

    if include_restart:
        if lines:
            lines.append("")
        restarted_at = current_restart or str(state.get("last_restart_at") or "")
        rendered_at = _status_timestamp(restarted_at)
        if rendered_at:
            lines.append("Server last restarted:")
            lines.append(rendered_at)
            lines.append(f"({_elapsed_ago(restarted_at, now_utc)})")
        else:
            unknown = str(
                options.get(
                    "unknown_restart_text",
                    "unknown (no Rust process start timestamp recorded)",
                )
            ).strip()
            lines.append("Server last restarted:")
            lines.append(unknown)

    return lines


def _cfg_base_dir(config_path: str) -> str:
    # Resolve relative paths against the CONFIG FILE location, not CWD.
    # This makes behavior identical under systemd vs manual runs.
    try:
        cp = os.path.abspath(os.path.expanduser(os.path.expandvars(config_path or "")))
        return os.path.dirname(cp) if cp else PROJECT_DIR
    except Exception:
        return PROJECT_DIR

def norm_path(p, *, base_dir: str):
    """
    Normalize paths:
      - expand ~ and $VARS
      - if relative, resolve against base_dir (config file dir)
      - return absolute, normalized path
      - keep ""/None as ""
    """
    if p is None:
        return ""
    if not isinstance(p, str):
        p = str(p)
    p = p.strip()
    if not p:
        return ""

    p = os.path.expandvars(os.path.expanduser(p))
    if not os.path.isabs(p):
        p = os.path.join(base_dir, p)

    return os.path.normpath(os.path.abspath(p))

def normalize_cfg_paths(cfg: dict, config_path: str) -> dict:
    base_dir = _cfg_base_dir(config_path)

    for k in ("server_dir", "lockfile", "logfile", "pause_file", "forced_wipe_state_file"):
        if k in cfg:
            cfg[k] = norm_path(cfg.get(k), base_dir=base_dir)

    # SR overrides: relative to server_dir (LinuxGSM root)
    for k in ("smoothrestarter_config_path", "smoothrestarter_plugin_path"):
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            cfg[k] = norm_path(v, base_dir=cfg["server_dir"])

    return cfg

# --------------------------------------------------------
# CONFIG LOADER
# --------------------------------------------------------
def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_HOME_PATH_RE = re.compile(r"^/home/([^/\x00]+)(?=/|$)")
_LINUX_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\$?$")
_FORCED_WIPE_ACTIONS = ("off", "map-wipe", "full-wipe")


def _config_json_path(parts) -> str:
    out = "$"
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(part)):
            out += f".{part}"
        else:
            out += f"[{json.dumps(str(part), ensure_ascii=False)}]"
    return out


def _validate_linux_home_user(value: str) -> str:
    username = str(value or "").strip()
    if (
        not username
        or len(username) > 32
        or not _LINUX_USER_RE.fullmatch(username)
        or username in (".", "..")
    ):
        raise ValueError(
            "home user must be a Linux account name (1-32 characters; "
            "letters, digits, underscore, dot, and hyphen are accepted)"
        )
    return username


def _rewrite_home_paths(node, username: str, parts=()):
    """
    Rewrite JSON string values whose complete path starts with /home/<user>.

    This deliberately does not rewrite embedded text, relative paths, config
    keys, or username-like non-path values such as +server.identity.
    """
    matches = []
    if isinstance(node, dict):
        for key in list(node):
            value = node[key]
            if isinstance(value, str):
                match = _HOME_PATH_RE.match(value)
                if match:
                    rewritten = f"/home/{username}{value[match.end():]}"
                    if rewritten != value:
                        node[key] = rewritten
                    matches.append(
                        {
                            "path": _config_json_path(parts + (key,)),
                            "old": value,
                            "new": rewritten,
                            "old_user": match.group(1),
                            "changed": rewritten != value,
                        }
                    )
            elif isinstance(value, (dict, list)):
                matches.extend(
                    _rewrite_home_paths(value, username, parts + (key,))
                )
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                match = _HOME_PATH_RE.match(value)
                if match:
                    rewritten = f"/home/{username}{value[match.end():]}"
                    if rewritten != value:
                        node[index] = rewritten
                    matches.append(
                        {
                            "path": _config_json_path(parts + (index,)),
                            "old": value,
                            "new": rewritten,
                            "old_user": match.group(1),
                            "changed": rewritten != value,
                        }
                    )
            elif isinstance(value, (dict, list)):
                matches.extend(
                    _rewrite_home_paths(value, username, parts + (index,))
                )
    return matches


def _cli_bool(value: str) -> bool:
    parsed = parse_bool(value, None)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            "expected one of: on/off, true/false, yes/no, or 1/0"
        )
    return bool(parsed)


def _resolve_forced_wipe_action(
    current,
    *,
    set_action=None,
    full_wipe_wipeday=None,
    map_wipe_wipeday=None,
):
    base = str(set_action if set_action is not None else current or "off").strip().lower()
    if base not in _FORCED_WIPE_ACTIONS:
        raise ValueError(
            "forced_wipe_action must be one of "
            f"{', '.join(_FORCED_WIPE_ACTIONS)}; got {base!r}"
        )

    full_enabled = base == "full-wipe"
    map_enabled = base == "map-wipe"
    if full_wipe_wipeday is not None:
        full_enabled = bool(full_wipe_wipeday)
    if map_wipe_wipeday is not None:
        map_enabled = bool(map_wipe_wipeday)

    both_enabled = full_enabled and map_enabled
    if full_enabled:
        resolved = "full-wipe"
    elif map_enabled:
        resolved = "map-wipe"
    else:
        resolved = "off"
    return resolved, both_enabled


def _load_config_document(path: str) -> dict:
    cfg_path = Path(
        os.path.abspath(os.path.expanduser(os.path.expandvars(path or "")))
    )
    if not cfg_path.exists():
        raise ValueError(f"config file does not exist: {cfg_path}")
    if not cfg_path.is_file():
        raise ValueError(f"config path is not a regular file: {cfg_path}")
    try:
        raw = cfg_path.read_text(encoding="utf-8-sig")
    except Exception as e:
        raise ValueError(f"cannot read config file {cfg_path}: {e}") from e
    if not raw.strip():
        raise ValueError(f"config file is empty or whitespace-only: {cfg_path}")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as e:
        context = _format_json_error_context(raw, e.lineno, e.colno)
        raise ValueError(
            f"invalid JSON in {cfg_path} at line {e.lineno}, "
            f"column {e.colno}: {e.msg}\n{context}"
        ) from e
    if not isinstance(document, dict):
        raise ValueError(
            "config top-level JSON must be an object, "
            f"got {type(document).__name__}: {cfg_path}"
        )
    return document


def _atomic_write_config_with_backup(path: str, document: dict) -> str:
    cfg_path = Path(
        os.path.abspath(os.path.expanduser(os.path.expandvars(path or "")))
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = cfg_path.with_name(f"{cfg_path.name}.bak.{timestamp}")
    temp_path = cfg_path.with_name(f".{cfg_path.name}.tmp.{os.getpid()}")
    original_mode = cfg_path.stat().st_mode & 0o7777

    shutil.copy2(str(cfg_path), str(backup_path))
    try:
        with open(temp_path, "x", encoding="utf-8") as f:
            json.dump(document, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, original_mode)
        os.replace(temp_path, cfg_path)
        try:
            dir_fd = os.open(str(cfg_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return str(backup_path)


def edit_config_file(
    path: str,
    *,
    change_home_user=None,
    set_forced_wipe_action=None,
    full_wipe_wipeday=None,
    map_wipe_wipeday=None,
) -> dict:
    document = _load_config_document(path)
    home_matches = []
    warnings = []
    changes = []

    if change_home_user is not None:
        username = _validate_linux_home_user(change_home_user)
        home_matches = _rewrite_home_paths(document, username)

    wipe_edit_requested = any(
        value is not None
        for value in (
            set_forced_wipe_action,
            full_wipe_wipeday,
            map_wipe_wipeday,
        )
    )
    if wipe_edit_requested:
        old_action = str(document.get("forced_wipe_action", "off")).strip().lower()
        new_action, both_enabled = _resolve_forced_wipe_action(
            old_action,
            set_action=set_forced_wipe_action,
            full_wipe_wipeday=full_wipe_wipeday,
            map_wipe_wipeday=map_wipe_wipeday,
        )
        if both_enabled:
            warnings.append(
                "both map-wipe and full-wipe were enabled for the forced-wipe "
                "day; full-wipe takes precedence. Effective "
                'forced_wipe_action="full-wipe".'
            )
        if new_action != old_action or "forced_wipe_action" not in document:
            document["forced_wipe_action"] = new_action
            changes.append(
                {
                    "path": "$.forced_wipe_action",
                    "old": old_action,
                    "new": new_action,
                }
            )

    changed = bool(
        any(match.get("changed") for match in home_matches)
        or changes
    )
    backup_path = ""
    if changed:
        backup_path = _atomic_write_config_with_backup(path, document)

    return {
        "config_path": str(
            Path(
                os.path.abspath(
                    os.path.expanduser(os.path.expandvars(path or ""))
                )
            )
        ),
        "home_matches": home_matches,
        "changes": changes,
        "warnings": warnings,
        "changed": changed,
        "backup_path": backup_path,
    }


def print_config_edit_result(result: dict, *, changed_home_user=None) -> None:
    print(f"CONFIG: {result['config_path']}")
    if changed_home_user is not None:
        matches = result.get("home_matches") or []
        print(f"HOME PATH MATCHES: {len(matches)}")
        for match in matches:
            print(f"  {match['path']}: {match['old']} -> {match['new']}")
        if matches:
            print(
                "WARN: rust-watchdog.service was not modified; review its "
                "User=, Group=, WorkingDirectory=, and ExecStart= values."
            )
    for change in result.get("changes") or []:
        print(f"CONFIG CHANGE: {change['path']}: {change['old']} -> {change['new']}")
    for warning in result.get("warnings") or []:
        print(f"WARN: {warning}")
    if result.get("changed"):
        print(f"BACKUP: {result['backup_path']}")
        print(f"SAVED: {result['config_path']}")
    else:
        print("NO CHANGES: the requested values already match the config.")

def _format_json_error_context(text: str, lineno: int, colno: int, radius: int = 2) -> str:
    lines = text.splitlines()
    if not lines:
        return "(no text content)"

    out = []
    start = max(1, lineno - radius)
    end = min(len(lines), lineno + radius)

    for n in range(start, end + 1):
        line = lines[n - 1]
        prefix = ">>" if n == lineno else "  "
        out.append(f"{prefix} {n:4d} | {line}")
        if n == lineno:
            caret_pad = " " * (colno + 7)  # align under content after '>> 1234 | '
            out.append(f"{caret_pad}^")
    return "\n".join(out)


def load_cfg(path, fp=None):
    cfg = dict(DEFAULTS)

    if not path:
        return cfg

    cfg_path = os.path.abspath(os.path.expanduser(os.path.expandvars(path)))

    if not os.path.exists(cfg_path):
        # Missing config is OK: defaults only
        return cfg

    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:
            raw = f.read()
    except Exception as e:
        fatal(f"config: cannot read '{cfg_path}': {e}", fp=fp)

    if raw is None or raw == "":
        fatal(
            f"config: file exists but is empty: {cfg_path}\n"
            f"Fix: restore valid JSON or delete the file if you want defaults only.",
            fp=fp
        )

    if not raw.strip():
        fatal(
            f"config: file contains only whitespace: {cfg_path}\n"
            f"Fix: restore valid JSON or delete the file if you want defaults only.",
            fp=fp
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        first_bytes = raw.encode("utf-8", errors="replace")[:64].hex(" ")
        context = _format_json_error_context(raw, e.lineno, e.colno)
        fatal(
            "config: invalid JSON in file:\n"
            f"  path:   {cfg_path}\n"
            f"  line:   {e.lineno}\n"
            f"  column: {e.colno}\n"
            f"  error:  {e.msg}\n"
            f"  pos:    {e.pos}\n"
            "\n"
            "Context:\n"
            f"{context}\n"
            "\n"
            f"First bytes (hex): {first_bytes}",
            fp=fp
        )
    except Exception as e:
        fatal(f"config: failed to parse '{cfg_path}': {e}", fp=fp)

    if not isinstance(data, dict):
        fatal(
            f"config: top-level JSON must be an object/dict, got {type(data).__name__}: {cfg_path}",
            fp=fp
        )

    cfg = _deep_merge(cfg, data)
    return cfg

def parse_bool(v, default=True):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return default

def apply_recovery_toggles(cfg):
    """
    Convenience: allow enabling/disabling server/mod updates without forcing users
    to edit recovery_steps manually.

    Behavior:
      - if enable_server_update == False -> remove step "update"
      - if enable_mods_update == False   -> remove step "mu"
      - everything else remains as-is
    """
    enable_update = parse_bool(cfg.get("enable_server_update"), True)
    enable_mu = parse_bool(cfg.get("enable_mods_update"), True)

    orig = cfg.get("recovery_steps", [])
    if not isinstance(orig, list):
        fatal("config: recovery_steps must be a list", fp=None)

    new = []
    for step in orig:
        if not isinstance(step, str) or not step.strip():
            fatal(f"config: recovery_steps contains invalid step: {repr(step)}", fp=None)

        s = step.strip().lower()
        if s == "update" and not enable_update:
            continue
        if s == "mu" and not enable_mu:
            continue
        new.append(step)

    if not new:
        fatal("config: recovery_steps became empty after applying enable_* toggles", fp=None)

    cfg["_recovery_steps_original"] = orig
    cfg["recovery_steps"] = new


_CONFIG_VIEW_SECTION_ORDER = (
    "Core",
    "Health and RCON",
    "Updates and recovery",
    "Forced wipe",
    "SmoothRestarter",
    "Alerts",
    "Service helpers",
    "Other",
)


def _config_view_section(key: str) -> str:
    key = str(key)
    if key == "alerts" or key.startswith("alerts_"):
        return "Alerts"
    if (
        key == "enable_forced_wipe_highlight"
        or key.startswith("forced_wipe_")
    ):
        return "Forced wipe"
    if (
        key == "enable_smoothrestarter_bridge"
        or key.startswith("smoothrestarter_")
        or key == "restart_request_cooldown_seconds"
    ):
        return "SmoothRestarter"
    if (
        key.startswith("watchdog_systemd_")
        or key.startswith("test_telegram_status_")
    ):
        return "Service helpers"
    if (
        key.startswith("dupe_identity_")
        or key.startswith("check_")
        or key.startswith("rcon_")
        or key.startswith("wipe_timestamp_")
        or key in {
            "server_port",
            "tcp_timeout",
            "details_timeout",
        }
    ):
        return "Health and RCON"
    if (
        key.startswith("update_")
        or key.startswith("enable_update_")
        or key in {
            "enable_server_update",
            "enable_mods_update",
            "recovery_steps",
            "timeouts",
        }
    ):
        return "Updates and recovery"
    if key in {
        "server_dir",
        "identity",
        "interval_seconds",
        "cooldown_seconds",
        "down_confirmations",
        "lockfile",
        "logfile",
        "pause_file",
        "dry_run",
    }:
        return "Core"
    return "Other"


def _effective_config_for_view(cfg: dict) -> dict:
    from rust_watchdog_alerts import effective_alert_config

    effective = _deep_merge({}, cfg)
    effective.pop("_recovery_steps_original", None)
    effective["alerts"] = effective_alert_config(effective)
    return effective


def _config_scalar_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        if not value:
            return '""'
        if any(ch in value for ch in "\r\n\t"):
            return json.dumps(value, ensure_ascii=False)
        return value
    return str(value)


def _config_tree_lines(value, *, indent: int, colorize) -> list:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [prefix + "{}"]

        scalar_keys = [
            str(key)
            for key, item in value.items()
            if not isinstance(item, (dict, list))
        ]
        width = max((len(key) for key in scalar_keys), default=0)
        width = min(max(width, 1), 42)
        lines = []
        for key, item in value.items():
            key_text = str(key)
            if isinstance(item, dict):
                if item:
                    lines.append(
                        prefix + colorize(f"{key_text}:", "key")
                    )
                    lines.extend(
                        _config_tree_lines(
                            item,
                            indent=indent + 2,
                            colorize=colorize,
                        )
                    )
                else:
                    lines.append(
                        prefix
                        + colorize(key_text.ljust(width), "key")
                        + "  {}"
                    )
            elif isinstance(item, list):
                if not item:
                    lines.append(
                        prefix
                        + colorize(key_text.ljust(width), "key")
                        + "  []"
                    )
                elif all(
                    not isinstance(entry, (dict, list))
                    for entry in item
                ):
                    rendered = json.dumps(item, ensure_ascii=False)
                    lines.append(
                        prefix
                        + colorize(key_text.ljust(width), "key")
                        + f"  {rendered}"
                    )
                else:
                    lines.append(
                        prefix + colorize(f"{key_text}:", "key")
                    )
                    for index, entry in enumerate(item):
                        lines.append(
                            " " * (indent + 2)
                            + colorize(f"[{index}]", "index")
                        )
                        lines.extend(
                            _config_tree_lines(
                                entry,
                                indent=indent + 4,
                                colorize=colorize,
                            )
                        )
            else:
                lines.append(
                    prefix
                    + colorize(key_text.ljust(width), "key")
                    + f"  {_config_scalar_text(item)}"
                )
        return lines

    return [prefix + _config_scalar_text(value)]


def _stream_is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _stream_supports_unicode(stream) -> bool:
    encoding = str(getattr(stream, "encoding", "") or "").strip()
    if not encoding:
        return False
    try:
        "⚙📄🧩🔧".encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def print_effective_config(
    cfg: dict,
    config_path: str,
    *,
    stream=None,
    use_color=None,
    use_unicode=None,
) -> None:
    stream = stream or sys.stdout
    is_tty = _stream_is_tty(stream)
    term = str(os.getenv("TERM", "") or "").strip().lower()
    if use_color is None:
        use_color = (
            is_tty
            and term not in ("", "dumb")
            and "NO_COLOR" not in os.environ
        )
    if use_unicode is None:
        use_unicode = (
            is_tty
            and term != "dumb"
            and _stream_supports_unicode(stream)
        )

    styles = {
        "header": "\033[1;36m",
        "section": "\033[1;33m",
        "key": "\033[36m",
        "index": "\033[2;36m",
    }

    def colorize(text: str, style: str) -> str:
        if not use_color:
            return text
        return f"{styles.get(style, '')}{text}\033[0m"

    icons = {
        "header": "⚙ " if use_unicode else "",
        "file": "📄 " if use_unicode else "",
        "defaults": "🧩 " if use_unicode else "",
        "normalized": "🔧 " if use_unicode else "",
    }
    resolved_path = os.path.abspath(
        os.path.expanduser(os.path.expandvars(config_path or ""))
    )
    source_state = (
        "loaded and merged"
        if resolved_path and os.path.isfile(resolved_path)
        else "not found; using built-in defaults"
    )
    effective = _effective_config_for_view(cfg)

    print(
        colorize(
            f"{icons['header']}Rust Watchdog v{__version__} "
            "-- effective configuration",
            "header",
        ),
        file=stream,
    )
    print(
        f"{icons['file']}Config file: {resolved_path or '(none)'} "
        f"({source_state})",
        file=stream,
    )
    print(
        f"{icons['defaults']}Built-in defaults: merged",
        file=stream,
    )
    print(
        f"{icons['normalized']}Runtime normalization: paths and recovery "
        "toggles applied",
        file=stream,
    )
    print("Environment variable values: not read", file=stream)

    sections = {name: {} for name in _CONFIG_VIEW_SECTION_ORDER}
    for key, value in effective.items():
        sections[_config_view_section(key)][key] = value

    for name in _CONFIG_VIEW_SECTION_ORDER:
        values = sections[name]
        if not values:
            continue
        rendered_values = (
            values["alerts"]
            if name == "Alerts" and set(values) == {"alerts"}
            else values
        )
        print(file=stream)
        print(colorize(f"[{name}]", "section"), file=stream)
        for line in _config_tree_lines(
            rendered_values,
            indent=2,
            colorize=colorize,
        ):
            print(line, file=stream)


def acquire_lock(lock_path, fp=None):
    """
    Create a lockfile containing our PID.
    If lockfile exists but PID is not running, treat it as stale and replace it.
    """
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        return True
    except FileExistsError:
        # stale lock detection
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                s = f.read().strip()
            pid = int(s) if s else None
        except Exception:
            pid = None

        if pid:
            try:
                os.kill(pid, 0)  # check if pid exists
                log(f"Lock exists at {lock_path} (pid {pid} is alive) -- refusing to start", fp)
                return False
            except ProcessLookupError:
                # stale
                pass
            except PermissionError:
                log(f"Lock exists at {lock_path} (pid {pid}) but no permission to verify -- refusing", fp)
                return False

        # stale or unreadable lockfile -> remove and retry once
        log(f"Stale lock detected at {lock_path} (pid={pid}) -- removing", fp)
        try:
            os.unlink(lock_path)
        except Exception as e:
            log(f"Failed to remove stale lock {lock_path}: {e}", fp)
            return False

        # retry create
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            return True
        except FileExistsError:
            log(f"Lock exists at {lock_path} (race) -- refusing", fp)
            return False

def release_lock(lock_path):
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass

def _request_stop(signum, frame):
    global stop_requested
    stop_requested = True

def sleep_interruptible(seconds):
    end = time.monotonic() + float(seconds)
    while time.monotonic() < end:
        if stop_requested:
            return
        time.sleep(0.2)

# ----------------------------------------------------
# PID finder for RustDedicated
# ----------------------------------------------------
def pgrep_rustdedicated_cmdlines():
    """
    Return lines like: '<pid> ./RustDedicated -batchmode ...'
    This excludes tmux wrapper processes.
    """
    try:
        return subprocess.check_output(["pgrep", "-ax", "RustDedicated"], text=True).splitlines()
    except subprocess.CalledProcessError:
        return []
    except Exception:
        return []

# ----------------------------------------------------
# Find potential duplicate instances of RustDedicated
# ----------------------------------------------------
def find_rustdedicated_identity_matches(identity: str):
    """
    Returns list of (pid:int, cmdline:str) for RustDedicated cmdlines that match +server.identity.
    """
    needle1 = f"+server.identity {identity}"
    needle2 = f'+server.identity "{identity}"'
    try:
        lines = pgrep_rustdedicated_cmdlines()
    except Exception:
        return []

    hits = []
    for line in lines:
        if needle1 in line or needle2 in line:
            try:
                pid_s = line.split(None, 1)[0]
                pid = int(pid_s)
            except Exception:
                continue
            hits.append((pid, line))
    return hits

def pid_listens_udp_port(pid: int, port: int) -> bool:
    """
    Best-effort. Requires ss to show pid mappings (usually OK as same user; sudo always OK).
    """
    if not shutil.which("ss"):
        return False
    try:
        out = subprocess.check_output(["ss", "-Hlunp"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return False

    needle_pid = f"pid={pid},"
    needle_port = f":{port} "
    for line in out.splitlines():
        if needle_port in line and needle_pid in line:
            return True
    return False

def handle_duplicate_rustdedicated(cfg, fp=None) -> bool:
    """
    Returns True if safe to proceed, False if watchdog should skip actions this tick.
    Policy:
      - warn: log only, continue
      - pause: create pause_file, skip actions
      - fatal: exit
      - kill_extra: kill all but the one listening on server_port (only if identifiable)
    """
    identity = str(cfg.get("identity") or "").strip()
    if not identity:
        return True

    hits = find_rustdedicated_identity_matches(identity)
    if len(hits) <= 1:
        return True

    policy = str(cfg.get("dupe_identity_policy", "pause")).strip().lower()
    listen_check = parse_bool(cfg.get("dupe_identity_check_listen_port", True), True)
    server_port = int(cfg.get("server_port", 28015))

    log(f"DUPLICATE: found {len(hits)} RustDedicated instances for identity='{identity}'", fp)
    for pid, line in hits:
        log(f"DUPLICATE: pid={pid} cmd={redact_secrets(line)}", fp)

    active_pid = None
    if listen_check:
        for pid, _ in hits:
            if pid_listens_udp_port(pid, server_port):
                active_pid = pid
                break
        if active_pid:
            log(f"DUPLICATE: active instance appears to be pid={active_pid} (listening UDP {server_port})", fp)
        else:
            log(f"DUPLICATE: could not identify an active listener on UDP {server_port}", fp)

    if policy == "warn":
        return True

    if policy == "fatal":
        fatal(f"Duplicate RustDedicated instances detected for identity '{identity}'", fp=fp)

    if policy == "pause":
        pause_file = (cfg.get("pause_file") or "").strip()
        if pause_file:
            try:
                if os.path.exists(pause_file):
                    # Do NOT overwrite; could be a manual pause.
                    log(f"DUPLICATE: pause file already exists (not overwriting): {pause_file}", fp)
                else:
                    Path(pause_file).write_text(
                        f"reason=duplicate_identity identity={identity} at={ts()}\n",
                        encoding="utf-8"
                    )
                    log(f"DUPLICATE: created pause file: {pause_file}", fp)
            except Exception as e:
                log(f"DUPLICATE: failed to create pause file '{pause_file}': {e}", fp)

        log("DUPLICATE: skipping watchdog actions this tick (policy=pause)", fp)
        return False

    if policy == "kill_extra":
        if not active_pid:
            log("DUPLICATE: policy=kill_extra but active pid not identifiable -> refusing to kill", fp)
            return False
        for pid, _ in hits:
            if pid == active_pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                log(f"DUPLICATE: SIGTERM sent to extra pid={pid}", fp)
            except Exception as e:
                log(f"DUPLICATE: failed to SIGTERM pid={pid}: {e}", fp)
        return False  # let next tick settle

    log(f"DUPLICATE: unknown dupe_identity_policy='{policy}' -> defaulting to pause behavior", fp)
    return False

def autoclear_stale_dupe_pause_on_startup(cfg, fp=None):
    """
    Auto-clear pause_file ONLY if it was auto-created for duplicate identity
    and the duplicate condition is no longer present.
    Never raises.
    """
    pause_file = (cfg.get("pause_file") or "").strip()
    if not pause_file or not os.path.exists(pause_file):
        return False

    try:
        txt = Path(pause_file).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        txt = ""

    # Only auto-clear pauses we created for dupes
    if "reason=duplicate_identity" not in (txt or ""):
        log(f"PAUSE: pause_file exists (manual pause assumed): {pause_file}", fp)
        return False

    identity = str(cfg.get("identity") or "").strip()
    hits = find_rustdedicated_identity_matches(identity) if identity else []

    if len(hits) <= 1:
        try:
            os.unlink(pause_file)
            log(f"PAUSE: auto-cleared stale dupe pause file: {pause_file}", fp)
            return True
        except Exception as e:
            log(f"PAUSE: failed to auto-clear {pause_file}: {e}", fp)
            return False

    log(f"PAUSE: keeping pause file (duplicate still present): {pause_file}", fp)
    for pid, line in hits:
        log(f"PAUSE: still duplicate: pid={pid} cmd={redact_secrets(line)}", fp)
    return False

# ------------------------------------
# PRE-FLIGHT CHECKS
# ------------------------------------
def fatal(msg, code=2, fp=None):
    # Log (if possible) + print to stderr + exit
    try:
        if fp:
            log(f"FATAL: {msg}", fp)
    except Exception:
        pass

    print(f"FATAL: {msg}", file=sys.stderr)

    # --- extra "you probably meant to do X" hints ---
    try:
        u = getpass.getuser()
    except Exception:
        u = "unknown"
    try:
        uid = os.geteuid()
        gid = os.getegid()
    except Exception:
        uid = gid = "unknown"

    cfg = CFG_FOR_HINTS or {}
    server_dir = (cfg.get("server_dir") or "").strip()
    logfile = (cfg.get("logfile") or "").strip()
    lockfile = (cfg.get("lockfile") or "").strip()
    pause_file = (cfg.get("pause_file") or "").strip()

    sep = "-" * 72
    print("", file=sys.stderr)
    print(sep, file=sys.stderr)
    print("rust-linuxgsm-watchdog: startup failed", file=sys.stderr)
    print(sep, file=sys.stderr)

    print(f"Reason: {msg}", file=sys.stderr)
    print("", file=sys.stderr)

    # Best-effort runtime context
    print(f"User: {u} (uid={uid} gid={gid})", file=sys.stderr)
    try:
        print(f"CWD:  {os.getcwd()}", file=sys.stderr)
    except Exception:
        pass

    if server_dir:
        print(f"server_dir: {server_dir}", file=sys.stderr)
        print("  - must exist, be accessible, and contain an executable './rustserver'", file=sys.stderr)

    if server_dir:
        print(f"rustserver: {os.path.join(server_dir, 'rustserver')}", file=sys.stderr)

    if logfile:
        print(f"logfile:   {logfile}", file=sys.stderr)
        print("  - parent directory must be writable by the current user", file=sys.stderr)

    if lockfile:
        print(f"lockfile:  {lockfile}", file=sys.stderr)

    if pause_file:
        print(f"pause_file:{pause_file}", file=sys.stderr)

    print("", file=sys.stderr)
    print(sep, file=sys.stderr)
    print("How to fix (recommended)", file=sys.stderr)
    print(sep, file=sys.stderr)

    print("Edit your config JSON so all paths make sense on THIS machine.", file=sys.stderr)
    print("Minimal example (use real paths):", file=sys.stderr)
    print("", file=sys.stderr)
    print("{", file=sys.stderr)
    print('  "server_dir": "/path/to/your/linuxgsm/rustserver/dir",', file=sys.stderr)
    print('  "logfile": "./log/rust_watchdog.log",', file=sys.stderr)
    print('  "lockfile": "./data/lock/rust_watchdog.lock",', file=sys.stderr)
    print('  "pause_file": "./data/.watchdog_pause"', file=sys.stderr)
    print("}", file=sys.stderr)

    print("", file=sys.stderr)
    print("Then create the local data dirs if needed:", file=sys.stderr)
    print("  mkdir -p ./data/log ./data/lock", file=sys.stderr)

    print("", file=sys.stderr)
    print(sep, file=sys.stderr)
    print("If this IS the LinuxGSM host", file=sys.stderr)
    print(sep, file=sys.stderr)
    print("Run it via systemd as the LinuxGSM user so permissions match your server install.", file=sys.stderr)
    print("(See rust-watchdog.service in the repo.)", file=sys.stderr)
    print(sep, file=sys.stderr)
    print("If you need help with command line options, run:  ./rust_watchdog.py --help")
    print(sep, file=sys.stderr)

    raise SystemExit(code)

def fatal_config_parse(path, msg, fp=None):
    print("", file=sys.stderr)
    print("-" * 72, file=sys.stderr)
    print("rust-linuxgsm-watchdog: config load failed", file=sys.stderr)
    print("-" * 72, file=sys.stderr)
    print(f"Config: {path}", file=sys.stderr)
    print(msg, file=sys.stderr)
    print("", file=sys.stderr)
    print("Common causes:", file=sys.stderr)
    print("  - empty file", file=sys.stderr)
    print("  - truncated file after a bad edit / failed copy", file=sys.stderr)
    print("  - invalid JSON syntax (missing comma, stray comment, trailing comma)", file=sys.stderr)
    print("  - wrong encoding / weird bytes at the beginning", file=sys.stderr)
    print("-" * 72, file=sys.stderr)
    raise SystemExit(2)

def ensure_dir(path, what, fp=None):
    """
    Ensure 'path' exists and is a directory. Create it if missing.
    """
    if not path:
        fatal(f"{what}: empty path", fp=fp)

    if os.path.exists(path):
        if not os.path.isdir(path):
            fatal(f"{what}: exists but is not a directory: {path}", fp=fp)
        return

    try:
        os.makedirs(path, exist_ok=True)
        log(f"PRECHECK: created directory: {path}", fp)
    except Exception as e:
        fatal(f"{what}: cannot create directory '{path}': {e}", fp=fp)

def require_dir_access(path, what, need_write=False, fp=None):
    """
    Directories need X to access. For write we require W+X.
    """
    if not os.path.isdir(path):
        fatal(f"{what}: not a directory: {path}", fp=fp)

    perms = os.R_OK | os.X_OK
    if need_write:
        perms = os.W_OK | os.X_OK

    if not os.access(path, perms):
        mode = "write" if need_write else "read"
        fatal(f"{what}: no {mode} access to directory: {path}", fp=fp)

def require_file_executable(path, what, fp=None):
    if not os.path.exists(path):
        fatal(f"{what}: missing: {path}", fp=fp)
    if not os.path.isfile(path):
        fatal(f"{what}: not a file: {path}", fp=fp)
    if not os.access(path, os.X_OK):
        fatal(f"{what}: not executable: {path}", fp=fp)

def preflight_or_die(cfg, server_dir, rustserver_path):
    """
    Pre-flight checklist:
    - server_dir exists + readable/writable (for updates)
    - rustserver exists + executable
    - lockfile dir exists/creatable + writable
    - logfile dir exists/creatable + writable + logfile openable (if enabled)
    - pause_file parent dir exists/creatable + writable (if enabled)
    - basic config sanity (ports, steps, timeouts)
    Returns an opened logfile handle (or None if logfile disabled).
    """
    # 0) If logfile is enabled, open it as early as possible so failures get written there too.
    fp = None
    logfile = (cfg.get("logfile") or "").strip()
    if logfile:
        log_dir = os.path.dirname(os.path.abspath(logfile)) or "."
        ensure_dir(log_dir, "logfile directory", fp=None)
        require_dir_access(log_dir, "logfile directory", need_write=True, fp=None)

        if os.path.exists(logfile) and os.path.isdir(logfile):
            fatal(f"logfile: path is a directory, not a file: {logfile}", fp=None)

        try:
            fp = open(logfile, "a", encoding="utf-8")
        except Exception as e:
            fatal(f"logfile: cannot open for append '{logfile}': {e}", fp=None)

    log(f"PRECHECK: Rust Watchdog v{__version__} starting pre-flight checklist", fp)
    log(f"PRECHECK: uid={os.geteuid()} gid={os.getegid()} cwd={os.getcwd()}", fp)

    # 1) Basic config sanity (cheap failures first)
    identity = (cfg.get("identity") or "").strip()
    if not identity:
        fatal("config: 'identity' is empty", fp=fp)

    try:
        interval = int(cfg.get("interval_seconds", 0))
        cooldown = int(cfg.get("cooldown_seconds", 0))
        confirmations = int(cfg.get("down_confirmations", 0))
    except Exception as e:
        fatal(f"config: interval/cooldown/confirmations must be integers: {e}", fp=fp)

    if interval <= 0:
        fatal("config: interval_seconds must be > 0", fp=fp)
    if cooldown < 0:
        fatal("config: cooldown_seconds must be >= 0", fp=fp)
    if confirmations <= 0:
        fatal("config: down_confirmations must be > 0", fp=fp)

    # Update-watch sanity (optional)
    if parse_bool(cfg.get("enable_update_watch"), False):
        try:
            uci = int(cfg.get("update_check_interval_seconds", 0))
            uto = int(cfg.get("update_check_timeout", 0))
        except Exception as e:
            fatal(f"config: update_check_* must be integers: {e}", fp=fp)
        if uci <= 0:
            fatal("config: update_check_interval_seconds must be > 0", fp=fp)
        if uto <= 0:
            fatal("config: update_check_timeout must be > 0", fp=fp)

    if (
        parse_bool(cfg.get("wipe_timestamp_rcon_enabled"), True)
        or parse_bool(
            cfg.get("wipe_timestamp_filesystem_fallback_enabled"),
            True,
        )
    ):
        try:
            wipe_timestamp_interval = int(
                cfg.get("wipe_timestamp_interval_seconds", 600)
            )
        except Exception as e:
            fatal(
                "config: wipe_timestamp_interval_seconds must be "
                f"an integer: {e}",
                fp=fp,
            )
        if wipe_timestamp_interval <= 0:
            fatal(
                "config: wipe_timestamp_interval_seconds must be > 0",
                fp=fp,
            )

    forced_wipe_action = str(cfg.get("forced_wipe_action", "off")).strip().lower()
    forced_wipe_reminder_enabled = parse_bool(
        cfg.get("forced_wipe_reminder_enabled"), True
    )
    forced_wipe_trigger = str(
        cfg.get("forced_wipe_trigger", "new-build-after-schedule")
    ).strip().lower()
    if forced_wipe_action not in ForcedWipeCoordinator.VALID_ACTIONS:
        fatal(
            "config: forced_wipe_action must be one of "
            f"{ForcedWipeCoordinator.VALID_ACTIONS}, got {forced_wipe_action!r}",
            fp=fp,
        )
    if forced_wipe_trigger not in ForcedWipeCoordinator.VALID_TRIGGERS:
        fatal(
            "config: forced_wipe_trigger must be one of "
            f"{ForcedWipeCoordinator.VALID_TRIGGERS}, got {forced_wipe_trigger!r}",
            fp=fp,
        )

    if forced_wipe_action != "off":
        if not parse_bool(cfg.get("enable_update_watch"), False):
            fatal(
                "config: automatic forced wipes require enable_update_watch=true",
                fp=fp,
            )
        if not parse_bool(cfg.get("enable_server_update"), True):
            fatal(
                "config: automatic forced wipes require enable_server_update=true",
                fp=fp,
            )
        try:
            tolerance_m = int(
                cfg.get("forced_wipe_early_release_tolerance_minutes", 15)
            )
            action_window_m = int(
                cfg.get("forced_wipe_action_window_minutes", 360)
            )
        except Exception as e:
            fatal(f"config: forced-wipe minute values must be integers: {e}", fp=fp)
        if tolerance_m < 0:
            fatal(
                "config: forced_wipe_early_release_tolerance_minutes must be >= 0",
                fp=fp,
            )
        if action_window_m <= 0:
            fatal(
                "config: forced_wipe_action_window_minutes must be > 0",
                fp=fp,
            )

    if forced_wipe_reminder_enabled:
        try:
            reminder_repeat_m = int(
                cfg.get("forced_wipe_reminder_repeat_minutes", 30)
            )
        except Exception as e:
            fatal(
                "config: forced_wipe_reminder_repeat_minutes must be "
                f"an integer: {e}",
                fp=fp,
            )
        if reminder_repeat_m <= 0:
            fatal(
                "config: forced_wipe_reminder_repeat_minutes must be > 0",
                fp=fp,
            )

    # parse the Smooth Restarter bridge
    if parse_bool(cfg.get("enable_smoothrestarter_bridge"), False) and not parse_bool(cfg.get("enable_update_watch"), False):
        log("PRECHECK: NOTE: enable_smoothrestarter_bridge=true but enable_update_watch=false -- bridge will never trigger", fp)

    # Optional but useful sanity
    if cfg.get("check_tcp_rcon", True):
        try:
            port = int(cfg.get("rcon_port", 0))
        except Exception:
            fatal("config: rcon_port must be an integer", fp=fp)
        if not (1 <= port <= 65535):
            fatal(f"config: rcon_port out of range: {port}", fp=fp)

    # Validate recovery steps are non-empty strings
    steps = cfg.get("recovery_steps", [])
    if not isinstance(steps, list) or not steps:
        fatal("config: recovery_steps must be a non-empty list", fp=fp)
    for s in steps:
        if not isinstance(s, str) or not s.strip():
            fatal(f"config: recovery_steps contains invalid step: {repr(s)}", fp=fp)

    # Validate timeouts are numeric (if present)
    timeouts = cfg.get("timeouts", {})
    if not isinstance(timeouts, dict):
        fatal("config: timeouts must be a dict", fp=fp)
    for k, v in timeouts.items():
        try:
            if v is None:
                continue
            float(v)
        except Exception:
            fatal(f"config: timeout for '{k}' must be numeric or null, got: {repr(v)}", fp=fp)

    # 2) server_dir must exist and be accessible (read + execute + write)
    if not os.path.exists(server_dir):
        fatal(f"server_dir: does not exist: {server_dir}", fp=fp)
    if not os.path.isdir(server_dir):
        fatal(f"server_dir: not a directory: {server_dir}", fp=fp)
    require_dir_access(server_dir, "server_dir", need_write=True, fp=fp)

    # 3) rustserver must exist and be executable
    require_file_executable(rustserver_path, "rustserver executable", fp=fp)

    # 4) lockfile directory: exists/creatable + writable
    lockfile = (cfg.get("lockfile") or "").strip()
    if not lockfile:
        fatal("config: lockfile path is empty", fp=fp)
    lock_dir = os.path.dirname(os.path.abspath(lockfile)) or "."
    ensure_dir(lock_dir, "lockfile directory", fp=fp)
    require_dir_access(lock_dir, "lockfile directory", need_write=True, fp=fp)

    # 5) pause_file parent directory (optional)
    pause_file = (cfg.get("pause_file") or "").strip()
    if pause_file:
        pause_dir = os.path.dirname(os.path.abspath(pause_file)) or "."
        ensure_dir(pause_dir, "pause_file directory", fp=fp)
        require_dir_access(pause_dir, "pause_file directory", need_write=True, fp=fp)

    # 6) Forced-wipe state directory (automatic action and/or reminders)
    forced_wipe_state_file = str(cfg.get("forced_wipe_state_file") or "").strip()
    if forced_wipe_action != "off" or forced_wipe_reminder_enabled:
        if not forced_wipe_state_file:
            fatal(
                "config: forced_wipe_state_file cannot be empty when automatic "
                "wiping or persistent reminders are enabled",
                fp=fp,
            )
        forced_wipe_state_dir = (
            os.path.dirname(os.path.abspath(forced_wipe_state_file)) or "."
        )
        ensure_dir(forced_wipe_state_dir, "forced-wipe state directory", fp=fp)
        require_dir_access(
            forced_wipe_state_dir,
            "forced-wipe state directory",
            need_write=True,
            fp=fp,
        )

    # 7) Summary
    log("PRECHECK: checklist results:", fp)
    log(f"  OK: server_dir writable: {server_dir}", fp)
    log(f"  OK: rustserver executable: {rustserver_path}", fp)
    log(f"  OK: lockfile dir writable: {lock_dir}", fp)
    if pause_file:
        log(f"  OK: pause_file parent dir writable: {os.path.dirname(os.path.abspath(pause_file))}", fp)
    else:
        log("  NOTE: pause_file disabled (empty)", fp)
    if forced_wipe_action != "off" or forced_wipe_reminder_enabled:
        log(f"  OK: forced-wipe action: {forced_wipe_action}", fp)
        log(f"  OK: forced-wipe state file: {forced_wipe_state_file}", fp)
        if forced_wipe_reminder_enabled:
            log(
                "  OK: forced-wipe reminder: "
                f"every {reminder_repeat_m}m until completion is recorded",
                fp,
            )
    if parse_bool(cfg.get("wipe_timestamp_rcon_enabled"), True):
        log(
            "  OK: primary wipe timestamp source: "
            "RCON serverinfo.SaveCreatedTime",
            fp,
        )
    else:
        log("  NOTE: primary RCON wipe timestamp discovery disabled", fp)
    if parse_bool(
        cfg.get("wipe_timestamp_filesystem_fallback_enabled"),
        True,
    ):
        log(
            "  OK: fallback wipe timestamp source: newest LinuxGSM .map "
            f"mtime under {_linuxgsm_server_identity_dir(cfg)} "
            f"(check every {int(cfg.get('wipe_timestamp_interval_seconds', 600))}s)",
            fp,
        )
    else:
        log("  NOTE: filesystem wipe timestamp fallback disabled", fp)

    if logfile:
        log(f"  OK: logfile open: {logfile}", fp)
    else:
        log("  NOTE: logfile disabled (empty)", fp)

    log("PRECHECK: finished OK", fp)
    return fp

# --------------------------------------------------------
# Checks on SmoothRestarter integrity
# --------------------------------------------------------
def _extract_line_matching(text: str, pat: re.Pattern):
    for ln in (text or "").splitlines():
        if pat.search(ln):
            return ln.strip()
    return ""

def smoothrestarter_loaded_via_rcon(cfg, fp=None):
    """
    Returns (state, detail)
      state: "LOADED" | "FAILED" | "NOT_FOUND" | "SKIPPED" | "UNKNOWN"
    """
    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        return ("SKIPPED", f"websocket-client missing ({ws_err})")

    ip, port, pw, src = get_rcon_endpoint(cfg, fp=fp, need_password=True)
    if not (ip and port and pw):
        return ("SKIPPED", "RCON endpoint missing (autodetect+config)")

    # 1) Try framework plugin list commands (best signal)
    probe_cmds = [
        "oxide.plugins",   # uMod/Oxide :contentReference[oaicite:2]{index=2}
        "plugins",         # alias :contentReference[oaicite:3]{index=3}
        "c.plugins",       # Carbon :contentReference[oaicite:4]{index=4}
    ]

    for cmd in probe_cmds:
        ok, resp = rcon_send(cfg, cmd, fp=fp)
        if not ok:
            continue

        msg = rcon_extract_message(resp)
        if not msg:
            continue

        if UNKNOWN_CMD_RE.search(msg):
            continue

        hitline = _extract_line_matching(msg, SR_NAME_RE)
        if hitline:
            # Try to distinguish "loaded" vs "failed" (oxide.plugins includes failed-to-load)
            if re.search(r"\b(fail|error|exception)\b", hitline, re.IGNORECASE):
                return ("FAILED", f"{cmd}: {hitline}")
            return ("LOADED", f"{cmd}: {hitline}")

        # Command worked, but SR not in list
        return ("NOT_FOUND", f"{cmd}: SmoothRestarter not listed")

    # 2) Fallback: ask SR itself (works even if list cmd differs)
    prefix = smoothrestarter_cmd_prefix(cfg)  # "sr" or "srestart" etc
    ok, resp = rcon_send(cfg, f"{prefix} status", fp=fp)
    if ok:
        msg = rcon_extract_message(resp)
        if msg and not UNKNOWN_CMD_RE.search(msg):
            return ("LOADED", f"{prefix} status: {strip_ansi(msg).strip()[:200]}")

    return ("UNKNOWN", "could not verify via oxide.plugins/plugins/c.plugins nor via '<prefix> status'")

def _read_text_best_effort(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def smoothrestarter_probe_cs(sr_plugin_path: Path, *, min_score: int = 2):
    """
    Returns (looks_ok, score, matched[], notes[]).

    "looks_ok" here means "kinda looks like SmoothRestarter" -- it's not a guarantee.
    This should be WARN-only by default (do not use it to hard-fail unless user enables strict mode).
    """
    txt = _read_text_best_effort(sr_plugin_path)
    notes = []
    matched = []

    if not txt.strip():
        notes.append("SmoothRestarter.cs unreadable or empty")
        return False, 0, matched, notes

    # Keep these fairly stable + forgiving. Use regex so whitespace/refactors don't break us.
    signatures = [
        (r"\bnamespace\s+Oxide\.Plugins\b", "namespace Oxide.Plugins"),
        (r'\[Info\(\s*"SmoothRestarter"\s*,', '[Info("SmoothRestarter", ...)]'),
        (r"\bclass\s+SmoothRestarter\b", "class SmoothRestarter"),
        (r"\bCovalencePlugin\b", "CovalencePlugin base"),
        (r"\bAddCovalenceCommand\b", "AddCovalenceCommand usage"),
        (r"\bsmoothrestarter\.(status|restart|cancel)\b", "permission strings"),
    ]

    score = 0
    for pattern, label in signatures:
        if re.search(pattern, txt, flags=re.IGNORECASE | re.MULTILINE):
            matched.append(label)
            score += 1

    looks_ok = score >= min_score
    if not looks_ok:
        notes.append(
            f"SmoothRestarter.cs signature score too low ({score}/{len(signatures)}). "
            f"Matched: {matched or 'none'}"
        )

    return looks_ok, score, matched, notes

def smoothrestarter_probe_config_commands(sr_cfg_path: Path, wanted_cmd: str):
    """
    Returns (ok, problems[]). Checks that watchdog's command alias exists in SmoothRestarter config.
    """
    problems = []
    if not sr_cfg_path.exists():
        problems.append(f"SmoothRestarter config missing: {sr_cfg_path} (may be first run)")
        return True, problems  # warn-only

    try:
        data = json.loads(sr_cfg_path.read_text(encoding="utf-8", errors="ignore") or "{}")
    except Exception as e:
        problems.append(f"SmoothRestarter config unreadable/invalid JSON: {e}")
        return False, problems

    cmds = data.get("Commands")
    if not isinstance(cmds, list) or not cmds:
        problems.append('SmoothRestarter config has no "Commands" list; watchdog cannot verify alias')
        return True, problems  # warn-only

    if wanted_cmd not in cmds:
        problems.append(
            f'SmoothRestarter config Commands does not include "{wanted_cmd}". '
            f"Available: {cmds}"
        )
        return False, problems

    return True, problems


def smoothrestarter_paths(server_dir, cfg=None):
    """
    SmoothRestarter defaults (uMod), under LinuxGSM:
      {server_dir}/serverfiles/oxide/config/SmoothRestarter.json
      {server_dir}/serverfiles/oxide/plugins/SmoothRestarter.cs

    Overrides (optional):
      cfg["smoothrestarter_config_path"]
      cfg["smoothrestarter_plugin_path"]

    If an override is relative, it's resolved relative to server_dir.
    """
    cfg = cfg or {}

    def resolve(p):
        p = (p or "").strip()
        if not p:
            return ""
        p = os.path.expandvars(os.path.expanduser(p))
        if not os.path.isabs(p):
            p = os.path.abspath(os.path.join(server_dir, p))
        return p

    cfg_override = resolve(cfg.get("smoothrestarter_config_path"))
    plugin_override = resolve(cfg.get("smoothrestarter_plugin_path"))

    if cfg_override and plugin_override:
        return (cfg_override, plugin_override)

    base = os.path.join(server_dir, "serverfiles", "oxide")
    default_cfg = os.path.join(base, "config", "SmoothRestarter.json")
    default_plugin = os.path.join(base, "plugins", "SmoothRestarter.cs")

    return (
        cfg_override or default_cfg,
        plugin_override or default_plugin,
    )

def smoothrestarter_available(server_dir: str, cfg: dict):
    sr_cfg_s, sr_plugin_s = smoothrestarter_paths(server_dir, cfg)
    sr_cfg = Path(sr_cfg_s)
    sr_plugin = Path(sr_plugin_s)

    strict_probe = bool(cfg.get("smoothrestarter_probe_strict", False))
    min_score = int(cfg.get("smoothrestarter_probe_min_score", 2))

    notes = []

    if not sr_plugin.exists():
        notes.append(f"SmoothRestarter plugin missing: {sr_plugin}")
        return False, sr_cfg.exists(), str(sr_cfg), str(sr_plugin), notes

    looks_ok, score, matched, probe_notes = smoothrestarter_probe_cs(sr_plugin, min_score=min_score)
    notes.extend(probe_notes)
    notes.append(f"SmoothRestarter.cs probe: score={score}, matched={matched}")

    if strict_probe and not looks_ok:
        notes.append("SmoothRestarter probe strict mode: treating low score as NOT OK")
        return False, sr_cfg.exists(), str(sr_cfg), str(sr_plugin), notes

    # command alias check (warn-only)
    wanted_cmd = smoothrestarter_cmd_prefix(cfg)
    cmd_ok, cmd_notes = smoothrestarter_probe_config_commands(sr_cfg, wanted_cmd)
    notes.extend(cmd_notes)
    if not cmd_ok:
        notes.append("SmoothRestarter command alias check failed (warn-only).")

    # Optional: runtime-loaded check (RCON)
    if parse_bool(cfg.get("smoothrestarter_check_loaded"), False):
        state, detail = smoothrestarter_loaded_via_rcon(cfg, fp=None)
        notes.append(f"SmoothRestarter runtime-loaded check: {state} -- {detail}")

        if parse_bool(cfg.get("smoothrestarter_check_loaded_strict"), False):
            if state not in ("LOADED",):
                notes.append("SmoothRestarter runtime-loaded strict mode: treating as NOT OK")
                return False, sr_cfg.exists(), str(sr_cfg), str(sr_plugin), notes

    return True, sr_cfg.exists(), str(sr_cfg), str(sr_plugin), notes

# --------------------------------------------------------
# Command runners etc
# --------------------------------------------------------
def run_cmd(cmd, cwd, fp=None, timeout=None, dry_run=False):
    """
    Run a command, stream stdout live, and enforce timeout even if the process is silent.
    Raises TimeoutError on timeout.
    """
    if dry_run:
        log(f"DRY_RUN: would run: {' '.join(cmd)} (cwd={cwd})", fp)
        return 0

    log(f"RUN: {' '.join(cmd)} (cwd={cwd})", fp)

    p = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,   # <-- REQUIRED for killpg safety
    )

    start = time.monotonic()
    fd = p.stdout.fileno()

    try:
        while True:

            # If systemd/user asked us to stop, abort this step.
            if stop_requested:
                log(f"Stop requested -- terminating: {' '.join(cmd)}", fp)
                _terminate_process_group(p, fp, grace=5.0)
                raise RuntimeError(f"Stop requested -- aborting: {' '.join(cmd)}")

            # if stop_requested:
            #     log(f"Stop requested -- terminating: {' '.join(cmd)}", fp)
            #     try:
            #         os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            #     except Exception:
            #         try:
            #             p.terminate()
            #         except Exception:
            #             pass

            #     # Give it a moment to die, then force-kill if needed
            #     deadline = time.monotonic() + 5.0
            #     while time.monotonic() < deadline and p.poll() is None:
            #         time.sleep(0.2)

            #     if p.poll() is None:
            #         try:
            #             os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            #         except Exception:
            #             try:
            #                 p.kill()
            #             except Exception:
            #                 pass

            #     raise RuntimeError(f"Stop requested -- aborting: {' '.join(cmd)}")

            # # Hard timeout (works even if child prints nothing)
            # if timeout is not None and (time.monotonic() - start) > timeout:
            #     try:
            #         os.killpg(os.getpgid(p.pid), signal.SIGKILL)  # kill whole group
            #     except Exception:
            #         try:
            #             p.kill()  # fallback: at least kill the parent
            #         except Exception:
            #             pass
            #     raise TimeoutError(f"Timeout after {timeout}s: {' '.join(cmd)}")

            if timeout is not None and (time.monotonic() - start) > timeout:
                _terminate_process_group(p, fp, grace=20.0)  # pick your grace
                raise TimeoutError(f"Timeout after {timeout}s: {' '.join(cmd)}")

            # Wait briefly for output (non-blocking)
            r, _, _ = select.select([fd], [], [], 0.5)

            if r:
                line = p.stdout.readline()
                if line:
                    log(line.rstrip("\n"), fp)
                else:
                    # EOF on pipe
                    if p.poll() is not None:
                        break
            else:
                # No output ready; if process exited, we're done
                if p.poll() is not None:
                    break

        rc = p.wait()
        log(f"EXIT {rc}: {' '.join(cmd)}", fp)
        return rc

    finally:
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:
            pass

def _terminate_process_group(p, fp, *, grace=20.0):
    # Try TERM first so LinuxGSM can clean up locks
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        log(f"TERM sent to process group pid={p.pid}", fp)
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass

    deadline = time.monotonic() + float(grace)
    while time.monotonic() < deadline and p.poll() is None:
        time.sleep(0.2)

    if p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            log(f"KILL sent to process group pid={p.pid}", fp)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # Reap it (best-effort)
    try:
        p.wait(timeout=5.0)
    except Exception:
        pass

def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s or "")

RCON_SAY_PRETTY_RE = re.compile(
    r'^\s*(global\.say|say)\s+(?:"(.*)"|(.*))\s*$',
    re.IGNORECASE
)

def sanitize_rust_console_text(s: str) -> str:
    """
    Make chat text safe to embed in a Rust console command line.

    - strips CR/LF
    - strips ';' (Rust console can treat it as command separator)
    - trims
    """
    s = (s or "")
    s = s.replace("\r", " ").replace("\n", " ")
    s = s.replace(";", " ")
    s = s.strip()
    return s if s else " "

def pretty_rcon_cmd(cmd: str) -> str:
    cmd = (cmd or "").strip()
    m = RCON_SAY_PRETTY_RE.match(cmd)
    if not m:
        return cmd

    verb = m.group(1)
    msg = m.group(2) if m.group(2) is not None else (m.group(3) or "")

    msg = msg.replace("\\\\", "\\").replace('\\"', '"')
    return f"{verb}: {msg}"

def run_cmd_capture(cmd, cwd, fp=None, timeout=None, dry_run=False):
    """
    Run a command and capture combined stdout/stderr.
    Returns (rc, output). rc can be int, or string like "TIMEOUT"/"ERROR".
    """
    if dry_run:
        log(f"DRY_RUN: would run: {' '.join(cmd)} (cwd={cwd})", fp)
        return (0, "")

    log(f"RUN: {' '.join(cmd)} (cwd={cwd})", fp)
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        out = p.stdout or ""
        log(f"EXIT {p.returncode}: {' '.join(cmd)}", fp)
        return (p.returncode, out)
    except subprocess.TimeoutExpired:
        log(f"TIMEOUT after {timeout}s: {' '.join(cmd)}", fp)
        return ("TIMEOUT", "")
    except Exception as e:
        log(f"ERROR running {' '.join(cmd)}: {e}", fp)
        return ("ERROR", "")

def parse_update_available(out: str):
    """
    Returns: True (update), False (no update), None (can't tell)
    """
    text = strip_ansi(out)
    if UPDATE_NO_RE.search(text):
        return False
    if UPDATE_YES_RE.search(text):
        return True
    return None


def parse_update_check(out: str, *, command: str = "") -> UpdateCheckResult:
    text = strip_ansi(out)
    local_m = LOCAL_BUILD_RE.search(text)
    remote_m = REMOTE_BUILD_RE.search(text)
    return UpdateCheckResult(
        verdict=parse_update_available(text),
        local_build=local_m.group(1) if local_m else "",
        remote_build=remote_m.group(1) if remote_m else "",
        command=command,
    )

def extract_rcon_from_cmdline_line(line: str):
    """
    Returns (rcon_ip, rcon_port, rcon_password) or (None, None, None)
    """
    try:
        toks = shlex.split(line)
    except Exception:
        return (None, None, None)

    # Drop leading PID from pgrep -af
    if toks and toks[0].isdigit():
        toks = toks[1:]

    def get_arg(name):
        # arguments look like: +rcon.ip 127.0.0.1
        try:
            i = toks.index(name)
            if i + 1 < len(toks):
                return toks[i + 1]
        except ValueError:
            return None
        return None

    ip = get_arg("+rcon.ip")
    port = get_arg("+rcon.port")
    pw = get_arg("+rcon.password")

    # Normalize
    if ip == "0.0.0.0":
        ip = "127.0.0.1"

    try:
        port = int(port) if port is not None else None
    except Exception:
        port = None

    return (ip, port, pw)

def detect_rcon_from_identity(cfg):
    identity = str(cfg.get("identity") or "").strip()
    if not identity:
        return (None, None, None)

    needle1 = f"+server.identity {identity}"
    needle2 = f'+server.identity "{identity}"'

    try:
        lines = pgrep_rustdedicated_cmdlines()
    except Exception:
        return (None, None, None)

    for line in lines:
        if needle1 not in line and needle2 not in line:
            continue
        ip, port, pw = extract_rcon_from_cmdline_line(line)
        if ip and port and pw:
            return (ip, port, pw)

    return (None, None, None)

# -----------------------------------------------------
# RCON HELPERS
# -----------------------------------------------------
SR_ALREADY_RESTARTING_RE = re.compile(r"\balready\s+restarting\b", re.IGNORECASE)

def rcon_extract_message(resp: str) -> str:
    s = (resp or "").strip()
    if not s:
        return ""
    # Rust WebRCON often returns JSON; try common message keys.
    try:
        obj = json.loads(s)
        for k in ("Message", "message", "Text", "text"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return strip_ansi(v).strip()
    except Exception:
        pass
    return strip_ansi(s).strip()


def _parse_rust_save_created_time(value: str) -> str:
    """
    Parse Rust serverinfo.SaveCreatedTime and return canonical UTC ISO-8601.

    Rust currently emits invariant US month/day order without a timezone. The
    value represents UTC; ISO-8601 is also accepted for forward compatibility.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    parsed_iso = _parse_utc_iso(raw)
    if parsed_iso is not None:
        return _utc_iso(parsed_iso)

    for fmt in (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S.%f",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M:%S.%f %p",
    ):
        try:
            parsed = datetime.strptime(raw, fmt).replace(
                tzinfo=timezone.utc
            )
            return _utc_iso(parsed)
        except ValueError:
            continue
    return ""


def extract_serverinfo_save_created_time(resp: str) -> str:
    """
    Decode the outer WebRCON frame and its JSON-encoded Message payload.

    Returns a normalized UTC timestamp, or an empty string when the response
    does not contain a valid SaveCreatedTime.
    """
    raw = str(resp or "").strip()
    if not raw:
        return ""
    try:
        outer = json.loads(raw)
    except Exception:
        return ""

    candidates = outer if isinstance(outer, list) else [outer]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        inner = candidate
        message = candidate.get(
            "Message",
            candidate.get("message"),
        )
        if isinstance(message, dict):
            inner = message
        elif isinstance(message, str) and message.strip():
            try:
                decoded = json.loads(message)
            except Exception:
                decoded = None
            if isinstance(decoded, dict):
                inner = decoded

        value = inner.get(
            "SaveCreatedTime",
            inner.get("saveCreatedTime"),
        )
        normalized = _parse_rust_save_created_time(value)
        if normalized:
            return normalized
    return ""


def rcon_send(
    cfg,
    command: str,
    fp=None,
    *,
    response_matcher=None,
    timeout_s=5.0,
):
    """
    Send a command via Rust WebRCON and return (ok, response_text).

    IMPORTANT:
    - Rust WebRCON's Identifier is effectively a 32-bit-ish integer in practice.
      Using epoch *milliseconds* can overflow/mismatch and you'll never see a match,
      which breaks plugin checks (oxide.plugins / sr status).
    - We therefore generate a 31-bit safe Identifier and wait for the matching frame.
    - When response_matcher is supplied, a matching command-reply frame is only an
      acknowledgement. Keep the socket open and collect console frames until the
      matcher confirms that the asynchronous command has actually completed.
    """
    ip, port, pw, src = get_rcon_endpoint(cfg, fp=fp)
    if not (ip and port and pw):
        return (False, "RCON endpoint missing (autodetect+config)")

    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        return (False, f"websocket-client not available: {ws_err}")

    from websocket import create_connection

    pw_enc = quote(pw, safe="")   # encode everything unsafe
    url = f"ws://{ip}:{port}/{pw_enc}"

    # 31-bit safe Identifier (avoid ms epoch overflow / mismatch)
    ident = (
        (int(time.time()) << 10) ^
        (os.getpid() & 0x3FF) ^
        (int(time.monotonic() * 1000) & 0x3FF)
    ) & 0x7FFFFFFF
    if ident == 0:
        ident = 1

    payload = {"Identifier": ident, "Message": command, "Name": "watchdog"}

    ws = None
    try:
        ws = create_connection(url, timeout=5)
        ws.settimeout(1.0)  # short recv timeout; we loop ourselves
        ws.send(json.dumps(payload))

        try:
            command_timeout = max(0.1, float(timeout_s))
        except (TypeError, ValueError):
            command_timeout = 5.0
        deadline = time.monotonic() + command_timeout
        last = ""
        candidate_generic = ""
        collected_messages = []

        def collect_message(value):
            message = strip_ansi(str(value or "")).strip()
            if not message:
                return False
            collected_messages.append(message)
            if response_matcher is None:
                return False
            combined = "\n".join(collected_messages)
            try:
                return bool(response_matcher(combined))
            except Exception as e:
                raise RuntimeError(f"RCON response matcher failed: {e}") from e

        while time.monotonic() < deadline:
            try:
                resp = ws.recv()
            except Exception as e:
                last = str(e)
                continue

            if not resp:
                continue

            if isinstance(resp, bytes):
                resp = resp.decode("utf-8", errors="replace")

            last = resp

            # Try JSON decode (WebRCON usually returns a JSON object)
            try:
                obj = json.loads(resp)
            except Exception:
                # Non-JSON response: treat as reply
                if response_matcher is None:
                    return (True, resp)
                if collect_message(resp):
                    return (True, "\n".join(collected_messages))
                continue

            # Sometimes we might get a list/array; scan it for our Identifier
            if isinstance(obj, list):
                for it in obj:
                    if not isinstance(it, dict):
                        continue
                    t = str(
                        it.get("Type", it.get("type", "")) or ""
                    ).strip().lower()
                    msg = it.get(
                        "Message",
                        it.get("message", it.get("Text", it.get("text", ""))),
                    )
                    if response_matcher is not None and t not in (
                        "serverinfo",
                        "chat",
                    ):
                        if collect_message(msg):
                            return (True, "\n".join(collected_messages))
                    rid = it.get("Identifier", it.get("identifier", None))
                    try:
                        rid_i = int(rid) if rid is not None else None
                    except Exception:
                        rid_i = None
                    if rid_i == ident and response_matcher is None:
                        return (True, resp)
                continue

            if not isinstance(obj, dict):
                continue

            rid = obj.get("Identifier", obj.get("identifier", None))
            try:
                rid_i = int(rid) if rid is not None else None
            except Exception:
                rid_i = None

            if rid_i == ident and response_matcher is None:
                return (True, resp)

            # Ignore noise frames
            t = str(obj.get("Type", obj.get("type", "")) or "").strip().lower()
            if t in ("serverinfo", "chat"):
                continue

            # Fallback candidate: Generic frames with a Message (some servers are sloppy about Identifier)
            msg = obj.get(
                "Message",
                obj.get("message", obj.get("Text", obj.get("text", ""))),
            )
            if response_matcher is not None:
                if collect_message(msg):
                    return (True, "\n".join(collected_messages))
                continue
            if msg and (t == "" or t == "generic"):
                candidate_generic = resp
                continue

        if response_matcher is not None:
            received = "\n".join(collected_messages).strip()
            detail = (
                f"RCON recv timeout after {command_timeout:g}s waiting for "
                f"terminal response to {command!r}"
            )
            if received:
                detail += f"\n{received}"
            elif last:
                detail += f" (last={strip_ansi(str(last))[:200]})"
            return (False, detail)

        if candidate_generic:
            return (True, candidate_generic)

        return (
            False,
            f"RCON recv timeout waiting for Identifier={ident} "
            f"(last={strip_ansi(str(last))[:200]})"
        )

    except Exception as e:
        return (False, f"RCON send failed: {e}")
    finally:
        try:
            if ws:
                ws.close()
        except Exception:
            pass

def _parse_tmux_l_and_s_from_cmdline(line: str):
    """
    Accepts a pgrep -af line, e.g.:
      "42245 tmux -L rustserver-<something> new-session ... -s rustserver ./RustDedicated ..."
    Returns (tmux_L_socket_name, tmux_session_name), either can be None.
    """
    try:
        toks = shlex.split(line)
    except Exception:
        return (None, None)

    # pgrep -af includes pid as first token
    if toks and toks[0].isdigit():
        toks = toks[1:]

    l_name = None
    s_name = None

    # tmux -L <socket>
    try:
        if "-L" in toks:
            i = toks.index("-L")
            if i + 1 < len(toks):
                l_name = toks[i + 1]
    except Exception:
        pass

    # tmux ... -s <session>
    try:
        if "-s" in toks:
            i = toks.index("-s")
            if i + 1 < len(toks):
                s_name = toks[i + 1]
    except Exception:
        pass

    return (l_name, s_name)

## // NOTE: this detection method is basically NOT in use!
## // We're using RCON by default
def detect_lgsm_tmux_context(cfg, fp=None):
    """
    Find the LinuxGSM tmux server socket (-L name) and tmux session (-s name)
    that hosts THIS Rust server identity.

    Returns (l_name, session_name) or (None, None).
    """
    identity = str(cfg.get("identity") or "").strip()
    if not identity:
        return (None, None)

    needle1 = f"+server.identity {identity}"
    needle2 = f"+server.identity \"{identity}\""

    try:
        lines = pgrep_rustdedicated_cmdlines()
    except subprocess.CalledProcessError:
        return (None, None)
    except Exception:
        return (None, None)

    # LinuxGSM typically wraps RustDedicated inside a tmux command line
    for line in lines:
        if "tmux" not in line or "RustDedicated" not in line:
            continue
        if needle1 not in line and needle2 not in line:
            continue

        l_name, s_name = _parse_tmux_l_and_s_from_cmdline(line)
        if l_name or s_name:
            return (l_name, s_name)

    return (None, None)

def tmux_base_cmd(l_name=None):
    """
    Build tmux command for either default server or LinuxGSM (-L) server.
    """
    if l_name:
        return ["tmux", "-L", l_name]
    return ["tmux"]

def tmux_list_sessions(l_name=None):
    if not shutil.which("tmux"):
        return None  # tmux missing
    try:
        out = subprocess.check_output(tmux_base_cmd(l_name) + ["ls"], stderr=subprocess.STDOUT, text=True)
    except subprocess.CalledProcessError:
        return []  # tmux exists, but no sessions (rc=1)
    sessions = []
    for line in out.splitlines():
        if ":" in line:
            sessions.append(line.split(":", 1)[0])
    return sessions

def choose_tmux_target(cfg, rustserver_path, l_name=None, prefer_session=None):
    sessions = tmux_list_sessions(l_name)
    if not sessions:
        return None
    if prefer_session and prefer_session in sessions:
        return prefer_session

    script_name = os.path.basename(rustserver_path)  # usually "rustserver"
    identity = str(cfg.get("identity") or "").strip()

    for cand in (script_name, identity, "rustserver"):
        if cand and cand in sessions:
            return cand

    if len(sessions) == 1:
        return sessions[0]

    for s in sessions:
        if "rust" in s.lower():
            return s

    return None

def tmux_send_line(target_session, line, fp=None, dry_run=False, timeout=5, l_name=None):
    """
    Send a line to the server console via tmux send-keys.
    """
    if dry_run:
        log(f"DRY_RUN: would {' '.join(tmux_base_cmd(l_name))} send-keys -t {target_session} '{line}' C-m", fp)
        return True

    if not shutil.which("tmux"):
        log("SMOOTH_BRIDGE: tmux not found in PATH", fp)
        return False

    try:
        p = subprocess.run(
            tmux_base_cmd(l_name) + ["send-keys", "-t", target_session, line, "C-m"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        if p.returncode != 0:
            log(f"SMOOTH_BRIDGE: tmux send-keys failed rc={p.returncode}: {strip_ansi(p.stdout).strip()}", fp)
            return False
        log(f"SMOOTH_BRIDGE: sent to tmux '{target_session}': {line}", fp)
        return True
    except subprocess.TimeoutExpired:
        log("SMOOTH_BRIDGE: tmux send-keys timed out", fp)
        return False
    except Exception as e:
        log(f"SMOOTH_BRIDGE: tmux send-keys error: {e}", fp)
        return False

def screen_list_sessions():
    if not shutil.which("screen"):
        return None  # screen missing
    try:
        out = subprocess.check_output(["screen", "-ls"], stderr=subprocess.STDOUT, text=True)
    except subprocess.CalledProcessError as e:
        out = e.output or ""
    sessions = []
    for line in out.splitlines():
        line = line.strip()
        # Typical: "12345.rustserver  (Detached)"
        m = re.match(r"^(\d+\.\S+)\s", line)
        if m:
            sessions.append(m.group(1))
    return sessions

def choose_screen_target(cfg, rustserver_path):
    sessions = screen_list_sessions()
    if not sessions:
        return None

    identity = str(cfg.get("identity") or "").strip()

    for s in sessions:
        if identity and identity in s:
            return s

    for s in sessions:
        if "rustserver" in s.lower():
            return s

    if len(sessions) == 1:
        return sessions[0]

    return sessions[0]

def screen_send_line(target_session, line, fp=None, dry_run=False, timeout=5):
    if dry_run:
        log(f"DRY_RUN: would screen -S {target_session} -p 0 -X stuff '{line}\\r'", fp)
        return True

    if not shutil.which("screen"):
        log("SMOOTH_BRIDGE: screen not found in PATH", fp)
        return False

    try:
        p = subprocess.run(
            ["screen", "-S", target_session, "-p", "0", "-X", "stuff", line + "\r"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        if p.returncode != 0:
            log(f"SMOOTH_BRIDGE: screen stuff failed rc={p.returncode}: {strip_ansi(p.stdout).strip()}", fp)
            return False
        log(f"SMOOTH_BRIDGE: sent to screen '{target_session}': {line}", fp)
        return True
    except subprocess.TimeoutExpired:
        log("SMOOTH_BRIDGE: screen stuff timed out", fp)
        return False
    except Exception as e:
        log(f"SMOOTH_BRIDGE: screen stuff error: {e}", fp)
        return False

def request_smooth_restart(
    cfg,
    server_dir,
    rustserver_path,
    fp=None,
    announce_message=None,
):
    """
    Ask SmoothRestarter to schedule a restart.

    RCON ONLY.
    We do NOT inject via tmux/screen because LinuxGSM's tmux session is not a real interactive console
    for Rust server commands in your setup (as established earlier).
    """
    
    ok, cfg_ok, sr_cfg, sr_plugin, notes = smoothrestarter_available(server_dir, cfg)
    for n in notes:
        log(f"SMOOTH_BRIDGE: {n}", fp)

    if not ok:
        log(f"SMOOTH_BRIDGE: SmoothRestarter plugin not found: {sr_plugin}", fp)
        log(
            f"SMOOTH_BRIDGE: Install it from: {SMOOTHRESTARTER_URL}",
            fp,
        )
        return False
    if not cfg_ok:
        log(f"SMOOTH_BRIDGE: NOTE: SmoothRestarter config missing (may be first run): {sr_cfg}", fp)

    delay = int(cfg.get("smoothrestarter_restart_delay_seconds", 300))
    template = (cfg.get("smoothrestarter_console_cmd") or "srestart restart {delay}").strip()
    cmd = template.format(delay=delay) if "{delay}" in template else f"{template} {delay}"

    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        log(
            f"SMOOTH_BRIDGE: FAIL: websocket-client missing ({ws_err}) "
            "-- install it with: python3 -m pip install websocket-client",
            fp,
        )
        return False

    message = str(announce_message or "").strip()
    if message:
        ok_say, say_resp = rcon_send(
            cfg,
            rcon_say_cmd("", message),
            fp=fp,
        )
        if ok_say:
            log("SMOOTH_BRIDGE: restart announcement sent via RCON", fp)
        else:
            log(
                "SMOOTH_BRIDGE: WARNING: restart announcement failed; "
                f"continuing with SmoothRestarter request: {say_resp}",
                fp,
            )

    ok_r, resp = rcon_send(cfg, cmd, fp=fp)
    if ok_r:
        log(f"SMOOTH_BRIDGE: RCON OK: {strip_ansi(resp).strip()}", fp)
        return True

    log(f"SMOOTH_BRIDGE: RCON FAIL: {resp}", fp)
    return False

def check_server_update_via_lgsm(cfg, server_dir, rustserver_path, fp=None):
    """
    Run LinuxGSM check-update (or cu) and interpret output.
    Returns UpdateCheckResult. verdict is True/False/None and the result also
    retains LinuxGSM's local/remote Steam build IDs when present.
    """
    timeout = int(cfg.get("update_check_timeout", 60))
    for subcmd in ("check-update", "cu"):
        rc, out = run_cmd_capture(
            [rustserver_path, subcmd],
            server_dir,
            fp=fp,
            timeout=timeout,
            dry_run=False
        )

        # Some scripts print "Unknown command" for unsupported subcommands
        if out and ("Unknown command" in out or "Unknown option" in out):
            continue

        result = parse_update_check(out or "", command=subcmd)
        if result.verdict is not None:
            if result.local_build or result.remote_build:
                log(
                    "UPDATE_WATCH: builds "
                    f"local={result.local_build or '?'} remote={result.remote_build or '?'}",
                    fp,
                )
            return result

        # Can't tell, but command ran
        if out:
            sample = "\n".join(strip_ansi(out).splitlines()[:8])
            log(f"UPDATE_WATCH: could not interpret check-update output. First lines:\n{sample}", fp)
        return result

    log("UPDATE_WATCH: neither 'check-update' nor 'cu' seems available in this LinuxGSM script", fp)
    return UpdateCheckResult(verdict=None)

def check_process_identity(identity, fp=None) -> HealthCheckResult:
    """
    Strong signal: RustDedicated process exists and commandline contains +server.identity identity
    """
    try:
        out = subprocess.check_output(["pgrep", "-af", "RustDedicated"], text=True).splitlines()
    except subprocess.CalledProcessError:
        return HealthCheckResult(
            name="process_identity",
            ok=False,
            code="NO_RUSTDEDI_PROCESS",
            detail="no RustDedicated process",
            weight_down=2,
        )
    except Exception as e:
        return HealthCheckResult(
            name="process_identity",
            ok=False,
            code="NO_RUSTDEDI_PROCESS",
            detail=f"pgrep failed: {e}",
            weight_down=2,
        )

    needle1 = f"+server.identity {identity}"
    needle2 = f'+server.identity "{identity}"'
    hits = [line for line in out if (needle1 in line or needle2 in line or f"+server.identity {identity} " in line)]

    if hits:
        return HealthCheckResult(
            name="process_identity",
            ok=True,
            code="OK",
            detail=f"matched process: {redact_secrets(hits[0])}",
            weight_up=2,
        )

    return HealthCheckResult(
        name="process_identity",
        ok=False,
        code="IDENTITY_MISMATCH",
        detail=f"RustDedicated running, but identity '{identity}' not found in cmdline",
        weight_down=2,
    )

def check_tcp(host, port, timeout_s) -> HealthCheckResult:
    """
    Medium signal: can open TCP connection to RCON websocket port.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return HealthCheckResult(
                name="tcp_rcon",
                ok=True,
                code="OK",
                detail=f"tcp connect ok {host}:{port}",
                weight_up=1,
            )
    except Exception as e:
        code = _tcp_fail_code(e)
        return HealthCheckResult(
            name="tcp_rcon",
            ok=False,
            code=code,
            detail=f"tcp connect failed {host}:{port} ({e})",
            weight_down=1,
        )

def check_lgsm_details(server_dir, rustserver_path, timeout_s) -> HealthCheckResult:
    """
    Parse Status: STARTED/STOPPED from ./rustserver details even if it hangs or returns weird rc.
    Never raise; return UNKNOWN on failure.
    """
    try:
        p = subprocess.run(
            [rustserver_path, "details"],
            cwd=server_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        status = "UNKNOWN"
        for line in (p.stdout or "").splitlines():
            m = STATUS_RE.match(line)
            if m:
                status = m.group(1).upper()
                break

        if status == "STARTED":
            return HealthCheckResult(
                name="lgsm_details",
                ok=True,
                code="OK",
                detail=f"status=STARTED rc={p.returncode}",
                weight_up=1,
            )

        if status == "STOPPED":
            return HealthCheckResult(
                name="lgsm_details",
                ok=False,
                code="LGSM_STOPPED",
                detail=f"status=STOPPED rc={p.returncode}",
                weight_down=1,
            )

        return HealthCheckResult(
            name="lgsm_details",
            ok=False,
            code="LGSM_DETAILS_ERROR",
            detail=f"status={status} rc={p.returncode}",
            weight_down=1,
        )

    except subprocess.TimeoutExpired:
        return HealthCheckResult(
            name="lgsm_details",
            ok=False,
            code="LGSM_DETAILS_TIMEOUT",
            detail=f"details timed out after {timeout_s}s",
            weight_down=1,
        )
    except Exception as e:
        return HealthCheckResult(
            name="lgsm_details",
            ok=False,
            code="LGSM_DETAILS_ERROR",
            detail=f"details error: {e}",
            weight_down=1,
        )

def inside_screen_or_tmux():
    return bool(os.environ.get("STY")) or bool(os.environ.get("TMUX"))

def smoothrestarter_cmd_prefix(cfg):
    """
    Extract the command prefix token used to invoke SmoothRestarter.

    Examples:
      smoothrestarter_console_cmd = "sr restart {delay}"      -> prefix "sr"
      smoothrestarter_console_cmd = "srestart restart {delay}" -> prefix "srestart"
    """
    template = (cfg.get("smoothrestarter_console_cmd") or "srestart restart {delay}").strip()
    try:
        toks = shlex.split(template)
    except Exception:
        toks = template.split()
    if toks:
        return toks[0]
    return "srestart"

def build_smoothrestarter_restart_cmd(cfg, delay_seconds: int):
    """
    Build the configured SmoothRestarter restart command, but with a caller-supplied delay.
    """
    delay = int(delay_seconds)
    template = (cfg.get("smoothrestarter_console_cmd") or "srestart restart {delay}").strip()
    if "{delay}" in template:
        return template.format(delay=delay)
    return f"{template} {delay}"

def send_console_line_via_backend(backend, target, line, *, fp=None, l_name=None, dry_run=False):
    """
    Send a single console line via the selected backend.
    backend: "tmux" or "screen"
    """
    if backend == "tmux":
        return tmux_send_line(target, line, fp=fp, dry_run=dry_run, l_name=l_name)
    if backend == "screen":
        return screen_send_line(target, line, fp=fp, dry_run=dry_run)
    log(f"SMOOTH_TEST: invalid backend: {backend}", fp)
    return False

def rust_console_say(prefix: str, msg: str) -> str:
    """
    Server console broadcast.

    We use server console "say ..." because this works via tmux/screen injection
    and does NOT depend on WebRCON/websocket-client autodetect.
    """
    prefix = (prefix or "").strip()
    msg = (msg or "").strip()
    if prefix:
        return f"say {prefix} {msg}"
    return f"say {msg}"

def rcon_global_say_cmd(prefix: str, msg: str) -> str:
    """
    Build a Rust WebRCON chat broadcast command: global.say "..."
    """
    prefix = (prefix or "").strip()
    msg = (msg or "").strip()
    full = f"{prefix} {msg}".strip() if prefix else msg

    # If full is empty, Rust will show an empty SERVER message (or nothing useful).
    if not full:
        full = " "  # or return "" and treat as "don't send"

    # Escape backslashes + quotes for Rust console string
    full = full.replace("\\", "\\\\").replace('"', '\\"')

    # NO trailing backslash. Just send the command.
    return f'global.say "{full}"'

def rcon_say_cmd(prefix: str, msg: str) -> str:
    """
    Build a Rust chat broadcast using 'say ...' (no quotes).

    This avoids the annoying \"...\" echo you get with global.say "...".
    """
    prefix = (prefix or "").strip()
    msg = (msg or "").strip()
    full = f"{prefix} {msg}".strip() if prefix else msg
    full = sanitize_rust_console_text(full)
    return f"say {full}"

def best_effort_rcon_say(cfg, msg: str, fp=None) -> bool:
    """
    Best-effort: try to say something over RCON.
    If the server is stuck, this will fail and we just continue anyway.
    Never raises.
    """
    msg = (msg or "").strip()
    if not msg:
        return False

    try:
        if parse_bool(cfg.get("dry_run"), False):
            log(f"DRY_RUN: would RCON say: {msg}", fp)
            return True

        ok, resp = rcon_send(cfg, rcon_say_cmd("", msg), fp=fp)
        # Keep the response short-ish in logs:
        if ok:
            log(f"RCON_SAY: OK -- {strip_ansi(resp).strip()[:200]}", fp)
        else:
            log(f"RCON_SAY: FAIL -- {resp}", fp)
        return bool(ok)
    except Exception as e:
        log(f"RCON_SAY: FAIL -- {e}", fp)
        return False

def update_watch_no_sr_countdown(cfg, fp=None):
    """
    Rudimentary countdown (no SR):
      "Time until server update and restart: xx seconds."
    """
    total = int(cfg.get("update_watch_no_sr_countdown_seconds", 30))
    tick = int(cfg.get("update_watch_no_sr_tick_seconds", 10))
    if total <= 0 or tick <= 0:
        return

    tmpl = str(cfg.get(
        "update_watch_countdown_template",
        "Time until server update and restart: {seconds} seconds."
    ))

    # DRY_RUN: don't actually wait; just log the intended announcements.
    if parse_bool(cfg.get("dry_run"), False):
        for s in range(total, 0, -tick):
            try:
                best_effort_rcon_say(cfg, tmpl.format(seconds=s), fp=fp)
            except Exception:
                best_effort_rcon_say(cfg, f"Time until server update and restart: {s} seconds.", fp=fp)
        return

    remaining = total
    while remaining > 0:
        if stop_requested:
            return
        try:
            msg = tmpl.format(seconds=remaining)
        except Exception:
            msg = f"Time until server update and restart: {remaining} seconds."
        best_effort_rcon_say(cfg, msg, fp=fp)

        sleep_interruptible(min(tick, remaining))
        remaining -= tick

def _run_lgsm_step_checked(cfg, server_dir, rustserver_path, step: str, fp=None):
    timeout = None
    try:
        timeout = cfg.get("timeouts", {}).get(step, None)
    except Exception:
        pass

    try:
        rc = run_cmd(
            [rustserver_path, step],
            server_dir,
            fp,
            timeout=timeout,
            dry_run=parse_bool(cfg.get("dry_run"), False),
        )
    except TimeoutError as e:
        return (False, str(e))
    except Exception as e:
        return (False, str(e))

    if rc != 0:
        return (False, f"LinuxGSM exited with rc={rc}")
    return (True, "")


def _forced_wipe_failure(
    coordinator: ForcedWipeCoordinator,
    cfg: dict,
    step: str,
    error: str,
    fp=None,
):
    now_utc = datetime.now(timezone.utc)
    coordinator.mark_failed(step, error, now_utc)
    log(f"FORCED_WIPE: FAILED at {step}: {error}", fp)
    alert(
        "forced_wipe_failed",
        f"Automatic forced wipe failed at {step}",
        level="error",
        fp=fp,
        identity=cfg.get("identity"),
        cycle=coordinator.state.get("cycle"),
        action=coordinator.action,
        candidate_build=coordinator.state.get("candidate_remote_build"),
        failed_step=step,
        error=error,
    )
    return False


def execute_forced_wipe_sequence(
    cfg: dict,
    server_dir: str,
    rustserver_path: str,
    coordinator: ForcedWipeCoordinator,
    *,
    server_already_down: bool,
    fp=None,
) -> bool:
    """
    Own the destructive lifecycle:
      [stop] -> backup -> update -> mu -> full-wipe/map-wipe -> start

    If wipe_done is already persisted, only start is retried. This makes a
    watchdog/system restart after deletion safe.
    """
    now_utc = datetime.now(timezone.utc)
    if not coordinator.needs_recovery(now_utc):
        return False

    armed_action = str(coordinator.state.get("armed_action") or "")
    if armed_action != coordinator.action:
        return _forced_wipe_failure(
            coordinator,
            cfg,
            "config-action-changed",
            f"armed action is {armed_action or '?'} but configured action is "
            f"{coordinator.action}; refusing destructive command",
            fp=fp,
        )

    if not coordinator._save(now_utc):
        return _forced_wipe_failure(
            coordinator,
            cfg,
            "state-persist",
            "cannot persist armed state; refusing lifecycle actions",
            fp=fp,
        )

    if coordinator.mark_started(now_utc):
        alert(
            "forced_wipe_started",
            "Automatic forced-wipe sequence started",
            level="warning",
            fp=fp,
            identity=cfg.get("identity"),
            cycle=coordinator.state.get("cycle"),
            action=coordinator.action,
            candidate_build=coordinator.state.get("candidate_remote_build"),
        )

    if (
        coordinator.state.get("wipe_started_at")
        and not coordinator.state.get("wipe_done")
    ):
        return _forced_wipe_failure(
            coordinator,
            cfg,
            "wipe-state-ambiguous",
            "a previous wipe command started without a persisted success marker; "
            "manual inspection required",
            fp=fp,
        )

    if coordinator.state.get("wipe_done"):
        log(
            "FORCED_WIPE: wipe_done already persisted; retrying start only",
            fp,
        )
        ok, err = _run_lgsm_step_checked(
            cfg, server_dir, rustserver_path, "start", fp=fp
        )
        if not ok:
            return _forced_wipe_failure(
                coordinator, cfg, "start", err, fp=fp
            )
        coordinator.mark_start_done(datetime.now(timezone.utc))
        return True

    if server_already_down:
        log(
            "FORCED_WIPE: health checks report DOWN; enforcing LinuxGSM stopped state",
            fp,
        )
    ok, err = _run_lgsm_step_checked(
        cfg, server_dir, rustserver_path, "stop", fp=fp
    )
    if not ok:
        return _forced_wipe_failure(
            coordinator, cfg, "stop", err, fp=fp
        )

    if parse_bool(cfg.get("forced_wipe_backup_before"), True):
        ok, err = _run_lgsm_step_checked(
            cfg, server_dir, rustserver_path, "backup", fp=fp
        )
        if not ok:
            if parse_bool(cfg.get("forced_wipe_backup_required"), True):
                return _forced_wipe_failure(
                    coordinator, cfg, "backup", err, fp=fp
                )
            log(f"FORCED_WIPE: backup failed but is not required: {err}", fp)

    ok, err = _run_lgsm_step_checked(
        cfg, server_dir, rustserver_path, "update", fp=fp
    )
    if not ok:
        return _forced_wipe_failure(
            coordinator, cfg, "update", err, fp=fp
        )

    if (
        parse_bool(cfg.get("forced_wipe_verify_update_current"), True)
        and not parse_bool(cfg.get("dry_run"), False)
    ):
        verification = check_server_update_via_lgsm(
            cfg, server_dir, rustserver_path, fp=fp
        )
        if verification.verdict is not False:
            detail = (
                "post-update check did not confirm current build "
                f"(verdict={verification.verdict}, "
                f"local={verification.local_build or '?'}, "
                f"remote={verification.remote_build or '?'})"
            )
            return _forced_wipe_failure(
                coordinator, cfg, "verify-update", detail, fp=fp
            )

    if parse_bool(cfg.get("enable_mods_update"), True):
        ok, err = _run_lgsm_step_checked(
            cfg, server_dir, rustserver_path, "mu", fp=fp
        )
        if not ok:
            return _forced_wipe_failure(
                coordinator, cfg, "mu", err, fp=fp
            )

    action = coordinator.action
    if not coordinator.mark_wipe_started(datetime.now(timezone.utc)):
        return _forced_wipe_failure(
            coordinator,
            cfg,
            "state-persist",
            "cannot persist pre-wipe marker; refusing destructive command",
            fp=fp,
        )

    ok, err = _run_lgsm_step_checked(
        cfg, server_dir, rustserver_path, action, fp=fp
    )
    if not ok:
        return _forced_wipe_failure(
            coordinator, cfg, action, err, fp=fp
        )

    # Persist the irreversible boundary before attempting startup.
    if not coordinator.mark_wipe_done(datetime.now(timezone.utc)):
        return _forced_wipe_failure(
            coordinator,
            cfg,
            "state-persist",
            "wipe succeeded but wipe_done could not be persisted; "
            "server left stopped to prevent an unsafe retry",
            fp=fp,
        )

    ok, err = _run_lgsm_step_checked(
        cfg, server_dir, rustserver_path, "start", fp=fp
    )
    if not ok:
        return _forced_wipe_failure(
            coordinator, cfg, "start", err, fp=fp
        )

    coordinator.mark_start_done(datetime.now(timezone.utc))
    return True


def update_watch_fallback_restart_now(
    cfg,
    server_dir,
    rustserver_path,
    fp=None,
    *,
    forced_wipe: ForcedWipeCoordinator = None,
):
    """
    No-SR path (or SR failed):
      - announce (best-effort)
      - crude countdown
      - final message
      - stop + update + mu + restart
      - or, for an armed monthly build/calendar fallback, the idempotent
        forced-wipe sequence
    """
    calendar_fallback = bool(
        forced_wipe
        and forced_wipe.state.get("armed_trigger") == "window-end-fallback"
    )
    if calendar_fallback:
        action = forced_wipe.action
        announce_message = (
            f"Facepunch forced-wipe cutoff reached -- {action} incoming."
        )
        final_message = (
            f"Forced {action} starting now -- come back in a few minutes!"
        )
        countdown_cfg = dict(cfg)
        countdown_cfg["update_watch_countdown_template"] = (
            f"Time until forced {action}: {{seconds}} seconds."
        )
    else:
        announce_message = str(
            cfg.get("update_watch_announce_message", "")
        ).strip()
        final_message = str(
            cfg.get("update_watch_final_message", "")
        ).strip()
        countdown_cfg = cfg

    # Announce (best-effort)
    best_effort_rcon_say(cfg, announce_message, fp=fp)

    # Countdown
    update_watch_no_sr_countdown(countdown_cfg, fp=fp)

    # Final message (best-effort)
    best_effort_rcon_say(cfg, final_message, fp=fp)

    if forced_wipe and forced_wipe.needs_recovery(datetime.now(timezone.utc)):
        return execute_forced_wipe_sequence(
            cfg,
            server_dir,
            rustserver_path,
            forced_wipe,
            server_already_down=False,
            fp=fp,
        )

    # Now do the actual sequence
    base = [s.strip().lower() for s in cfg.get("recovery_steps", [])]
    base = [s for s in base if s]  # sanitize

    if "restart" in base:
        base = [s for s in base if s != "restart"]
        base.append("start")  # or keep restart and drop explicit stop

    steps = ["stop"] + base

    for step in steps:
        if stop_requested:
            log("Stop requested -- aborting update-watch fallback restart", fp)
            return

        s = (step or "").strip().lower()
        if not s:
            continue

        timeout = None
        try:
            timeout = cfg.get("timeouts", {}).get(s, None)
        except Exception:
            timeout = None

        try:
            run_cmd([rustserver_path, s], server_dir, fp, timeout=timeout, dry_run=cfg["dry_run"])
        except TimeoutError as e:
            log(f"STEP TIMEOUT ({s}): {e}", fp)
        except Exception as e:
            log(f"STEP ERROR ({s}): {e}", fp)

    return True

def test_smoothrestarter_bridge(cfg, server_dir, rustserver_path, fp=None, send=False):
    """
    RCON-only SmoothRestarter ceremony test.
    No tmux/screen injection.
    """

    ok, cfg_ok, sr_cfg, sr_plugin, notes = smoothrestarter_available(server_dir, cfg)
    for n in notes:
        log(f"SMOOTH_BRIDGE: {n}", fp)

    log(f"SMOOTH_TEST: plugin path: {sr_plugin}", fp)
    log(f"SMOOTH_TEST: config path: {sr_cfg}", fp)

    if not ok:
        log(f"SMOOTH_TEST: FAIL: SmoothRestarter plugin missing. Get it from: {SMOOTHRESTARTER_URL}", fp)
        return 2

    if not cfg_ok:
        log(f"SMOOTH_TEST: NOTE: SmoothRestarter config missing (may be first run): {sr_cfg}", fp)

    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        log(f"SMOOTH_TEST: FAIL: websocket-client missing ({ws_err}) -- RCON path unavailable", fp)
        return 2

    # Verify we can autodetect WebRCON endpoint for this identity
    ip, port, pw = detect_rcon_from_identity(cfg)
    if not (ip and port and pw):
        log("SMOOTH_TEST: FAIL: RCON autodetect failed (missing ip/port/password in RustDedicated cmdline)", fp)
        return 2

    log(f"SMOOTH_TEST: RCON autodetect OK: ws://{ip}:{port}/<password>", fp)

    # ---- ceremony commands ----
    prefix = smoothrestarter_cmd_prefix(cfg)
    test_delay = int(cfg.get("smoothrestarter_test_delay_seconds", 120))
    cancel_after = int(cfg.get("smoothrestarter_test_cancel_after_seconds", 8))
    want_status = parse_bool(cfg.get("smoothrestarter_test_send_status", True), True)
    chat_prefix = (cfg.get("smoothrestarter_test_chat_prefix") or "[Rust Watchdog]").strip()

    restart_cmd = build_smoothrestarter_restart_cmd(cfg, test_delay)
    status_cmd = f"{prefix} status"
    cancel_cmd = f"{prefix} cancel"

    log("SMOOTH_TEST: ceremony plan (RCON only):", fp)
    log("  announce: dry-run start (say)", fp)
    if want_status:
        log(f"  send: {status_cmd}", fp)
    log(f"  send: {restart_cmd}", fp)
    log(f"  wait: {cancel_after}s", fp)
    log(f"  send: {cancel_cmd}", fp)
    if want_status:
        log(f"  send: {status_cmd}", fp)
    log("  announce: test over (say)", fp)

    if not send:
        log("SMOOTH_TEST: OK: wiring looks good (dry test; not sending anything)", fp)
        return 0

    def rcon_line(cmd: str) -> bool:
        ok_r, resp = rcon_send(cfg, cmd, fp=fp)
        if ok_r:
            # resp is JSON-ish; don't spam, but keep *some* visibility
            log(f"SMOOTH_TEST: RCON OK: {cmd} -- resp={strip_ansi(resp).strip()}", fp)
            return True
        log(f"SMOOTH_TEST: RCON FAIL: {cmd} -- {resp}", fp)
        return False

    log("SMOOTH_TEST: SENDING ceremony via RCON (countdown will be started, then cancelled)", fp)

    # 1) announce start
    if not rcon_line(rcon_say_cmd(chat_prefix, "SmoothRestarter bridge DRY RUN test starting. Don't Panic! Server is NOT restarting.")):
        return 2

    # 2) optional status
    if want_status:
        rcon_line(status_cmd)

    # 3) start countdown
    if not rcon_line(restart_cmd):
        return 2

    # 4) wait a bit
    time.sleep(max(0, cancel_after))

    # 5) cancel countdown
    if not rcon_line(cancel_cmd):
        log("SMOOTH_TEST: FAIL: cancel failed (countdown may still be active!)", fp)
        return 2

    # 6) optional status
    if want_status:
        rcon_line(status_cmd)

    # 7) announce end
    rcon_line(rcon_say_cmd(chat_prefix, "SmoothRestarter bridge dry run test over, countdown cancelled. Back to Rust!"))

    log("SMOOTH_TEST: OK: ceremony complete (RCON only)", fp)
    return 0

def health_report(cfg, server_dir, rustserver_path, fp=None):
    """
    Returns (state, evidence_lines)
    state in: RUNNING, DOWN, UNKNOWN
    """
    results = []

    # 1) Process+identity (strong)
    if cfg.get("check_process_identity", True):
        results.append(check_process_identity(cfg["identity"], fp))

    # 2) TCP connect to RCON port (medium)
    if cfg.get("check_tcp_rcon", True):
        ip, port, _pw, src = get_rcon_endpoint(cfg, fp=fp, need_password=False)
        if ip and port:
            r = check_tcp(ip, port, float(cfg["tcp_timeout"]))
            r.detail = f"{r.detail} (src={src})"
            results.append(r)
        else:
            results.append(HealthCheckResult(
                name="tcp_rcon",
                ok=False,
                code="RCON_ENDPOINT_MISSING",
                detail="no RCON endpoint (autodetect+config both missing)",
                weight_down=1,
            ))

    # 3) LGSM details (weak-ish but informative)
    if cfg.get("check_lgsm_details", True):
        results.append(check_lgsm_details(server_dir, rustserver_path, int(cfg["details_timeout"])))

    up = sum(r.weight_up for r in results if r.ok)
    down = sum(r.weight_down for r in results if not r.ok)

    if up > 0:
        state = "RUNNING"
    elif down > 0:
        state = "DOWN"
    else:
        state = "UNKNOWN"

    primary = _pick_primary_cause(results)
    hint = HEALTH_HINTS.get(primary, "")

    evidence = []
    if primary != "OK":
        evidence.append(f"PRIMARY_CAUSE: {primary} -- {hint}")

    for r in results:
        evidence.append(f"{r.name}: {'PASS' if r.ok else 'FAIL'} [{r.code}] -- {r.detail}")

    return (state, evidence)


def print_forced_wipe_status(cfg: dict) -> None:
    coordinator = ForcedWipeCoordinator(cfg, persist=False)
    now_utc = datetime.now(timezone.utc)
    status = coordinator.status(now_utc)
    status["last_wipe_age"] = _elapsed_ago(
        str(status.get("last_wipe_at") or ""),
        now_utc,
    )
    status["last_restart_age"] = _elapsed_ago(
        str(status.get("last_restart_at") or ""),
        now_utc,
    )
    ordered = (
        "enabled",
        "action",
        "armed_action",
        "armed_trigger",
        "trigger",
        "fallback_at_window_end",
        "cycle",
        "scheduled_utc",
        "action_window_ends_utc",
        "cycle_first_observed_at",
        "cycle_last_observed_at",
        "prewipe_remote_build",
        "candidate_remote_build",
        "pending",
        "started_at",
        "wipe_started_at",
        "wipe_done",
        "wipe_done_at",
        "start_done",
        "completed",
        "completed_at",
        "completion_source",
        "last_wipe_at",
        "last_wipe_age",
        "last_wipe_source",
        "last_wipe_kind",
        "last_restart_at",
        "last_restart_age",
        "last_restart_source",
        "reminder_enabled",
        "reminder_due",
        "reminder_repeat_minutes",
        "reminder_last_sent_at",
        "latest_local_build",
        "latest_remote_build",
        "latest_update_verdict",
        "latest_build_seen_at",
        "failed_step",
        "last_error",
        "state_file",
    )
    for key in ordered:
        value = status.get(key)
        if value in ("", None):
            value = "-"
        print(f"{key}: {value}")


def maybe_emit_forced_wipe_reminder(
    coordinator: ForcedWipeCoordinator,
    cfg: dict,
    fp=None,
    *,
    now_utc: datetime = None,
) -> bool:
    now_utc = now_utc or datetime.now(timezone.utc)
    status = coordinator.reminder_status(now_utc)
    if not status.get("send_due"):
        return False

    message = coordinator.render_reminder(status)
    # Save the rate-limit marker before queuing the alert. A crash immediately
    # after delivery must not turn a watchdog restart into duplicate spam.
    coordinator.mark_reminder_sent(now_utc)
    log(message, fp)
    alert(
        "forced_wipe_due",
        message,
        level="warning",
        fp=fp,
        identity=cfg.get("identity"),
        cycle=status.get("cycle"),
        scheduled_utc=status.get("scheduled_utc"),
        action=status.get("action"),
        last_wipe_at=status.get("last_wipe_at"),
        last_wipe_age=status.get("last_wipe_age"),
        local_build=status.get("latest_local_build"),
        remote_build=status.get("latest_remote_build"),
        update_verdict=status.get("latest_update_verdict"),
    )
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(PROJECT_DIR, "rust_watchdog.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--version", action="store_true", help="print version and exit")
    ap.add_argument(
        "--view-config",
        "--viewconfig",
        dest="view_config",
        action="store_true",
        help=(
            "show the complete effective configuration after defaults, path "
            "normalization, and recovery toggles; then exit"
        ),
    )
    ap.add_argument(
        "--change-home-user",
        "--changeuser",
        dest="change_home_user",
        metavar="USER",
        help=(
            "persistently rewrite /home/<user> prefixes in JSON config path "
            "values, save a backup, and exit"
        ),
    )
    ap.add_argument(
        "--set-forced-wipe-action",
        choices=_FORCED_WIPE_ACTIONS,
        help=(
            "persist forced_wipe_action as off, map-wipe, or full-wipe and exit"
        ),
    )
    ap.add_argument(
        "--full-wipe-wipeday",
        type=_cli_bool,
        metavar="{on|off}",
        help=(
            "persistently enable/disable full wipes on the forced-wipe day "
            "(also accepts true/false, yes/no, and 1/0)"
        ),
    )
    ap.add_argument(
        "--map-wipe-wipeday",
        type=_cli_bool,
        metavar="{on|off}",
        help=(
            "persistently enable/disable map wipes on the forced-wipe day "
            "(full-wipe takes precedence if both are enabled)"
        ),
    )
    ap.add_argument(
        "--forced-wipe-status",
        action="store_true",
        help="show forced-wipe schedule/build fence/persisted state and exit",
    )
    ap.add_argument(
        "--mark-forced-wipe-done",
        nargs="?",
        const="now",
        metavar="UTC_TIMESTAMP",
        help=(
            "record a manual wipe and exit; omit UTC_TIMESTAMP to use now "
            "(example: 2026-08-06T18:23:00Z)"
        ),
    )
    ap.add_argument(
        "--forced-wipe-kind",
        choices=("unknown", "map-wipe", "full-wipe"),
        default="unknown",
        help="wipe kind stored with --mark-forced-wipe-done",
    )
    ap.add_argument("--test-rcon-say", metavar="MSG",
                help="send a global chat message via RCON (no plugins required) and exit")
    ap.add_argument("--test-rcon-cmd", metavar="CMD",
                help="send an arbitrary RCON command and print the response; then exit")
    ap.add_argument("--test-smoothrestarter", action="store_true",
                    help="validate SmoothRestarter bridge wiring and print what would be sent; then exit")
    ap.add_argument("--test-smoothrestarter-send", action="store_true",
        help="same as --test-smoothrestarter but actually sends the ceremony via RCON; then exit")
    ap.add_argument(
        "--smooth-restart-server",
        nargs="?",
        const="",
        metavar="MESSAGE",
        help=(
            "request a real restart through SmoothRestarter and exit; "
            "optionally broadcast MESSAGE first"
        ),
    )
    ap.add_argument(
        "--test-telegram-status",
        action="store_true",
        help="send a direct Telegram status test message and exit",
    )    
    args = ap.parse_args()

    if args.version:
        print(__version__)
        return

    config_edit_requested = any(
        value is not None
        for value in (
            args.change_home_user,
            args.set_forced_wipe_action,
            args.full_wipe_wipeday,
            args.map_wipe_wipeday,
        )
    )
    if config_edit_requested:
        if args.view_config:
            ap.error(
                "--view-config cannot be combined with persistent "
                "config-edit options"
            )
        try:
            result = edit_config_file(
                args.config,
                change_home_user=args.change_home_user,
                set_forced_wipe_action=args.set_forced_wipe_action,
                full_wipe_wipeday=args.full_wipe_wipeday,
                map_wipe_wipeday=args.map_wipe_wipeday,
            )
        except (OSError, ValueError) as e:
            ap.error(f"could not edit config: {e}")
        print_config_edit_result(
            result,
            changed_home_user=args.change_home_user,
        )
        return

    cfg = load_cfg(args.config)
    cfg = normalize_cfg_paths(cfg, args.config)
    apply_recovery_toggles(cfg)

    if args.view_config:
        print_effective_config(cfg, args.config)
        return

    global CFG_FOR_HINTS, STATUS_COORDINATOR
    CFG_FOR_HINTS = cfg

    if args.forced_wipe_status:
        print_forced_wipe_status(cfg)
        return

    if args.mark_forced_wipe_done is not None:
        now_utc = datetime.now(timezone.utc)
        raw = str(args.mark_forced_wipe_done or "now").strip()
        wiped_at = now_utc if raw.lower() == "now" else _parse_utc_iso(raw)
        if wiped_at is None:
            ap.error(
                "--mark-forced-wipe-done requires an ISO-8601 UTC timestamp "
                "such as 2026-08-06T18:23:00Z"
            )
        coordinator = ForcedWipeCoordinator(cfg, persist=True)
        try:
            result = coordinator.mark_manual_complete(
                now_utc,
                wiped_at=wiped_at,
                wipe_kind=args.forced_wipe_kind,
            )
        except Exception as e:
            ap.error(f"could not record forced wipe: {e}")
        print(f"cycle: {result['cycle']}")
        print(f"scheduled_utc: {result['scheduled_utc']}")
        print(f"last_wipe_at: {result['last_wipe_at']}")
        print(f"last_wipe_age: {result['last_wipe_age']}")
        print(f"last_wipe_kind: {result['last_wipe_kind']}")
        print(f"completed_cycle: {result['completed_cycle']}")
        return

    # Now server_dir is already absolute+stable (no CWD surprises)
    server_dir = cfg["server_dir"]
    rustserver_path = os.path.join(server_dir, "rustserver")

    # Clean shutdown behavior under systemd (SIGTERM) and Ctrl-C (SIGINT)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    # Pre-flight checklist (also opens logfile if enabled)
    fp = preflight_or_die(cfg, server_dir, rustserver_path)
    log(f"Rust Watchdog v{__version__} starting (dry_run={cfg.get('dry_run')})", fp)
    log(f"SOURCE: {os.path.abspath(__file__)}", fp)
    log(f"CONFIG: {os.path.abspath(args.config)}", fp)

    # Auto-clear stale dupe pause files created by our dupe-guard (if safe)
    autoclear_stale_dupe_pause_on_startup(cfg, fp)

    # Telegram status test path:
    # do this BEFORE init_alerts(), so the test can diagnose missing env vars cleanly
    # instead of being gated by AlertManager startup.
    if args.test_telegram_status:
        rc = test_telegram_status(cfg, args, fp=fp)
        if fp:
            fp.close()
        raise SystemExit(rc)

    # One-time dependency hint
    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        log(
            f"DEPS: websocket-client missing ({ws_err}) -- RCON features disabled "
            f"(wipe timestamp discovery, --test-rcon-say, and the RCON "
            f"SmoothRestarter bridge won't work).",
            fp
        )

    # test rcon
    if args.test_rcon_say:
        # // (old method)
        # msg = args.test_rcon_say.replace('"', '\\"')
        # ok, resp = rcon_send(cfg, rcon_global_say_cmd("", args.test_rcon_say), fp=fp)
        ok, resp = rcon_send(cfg, rcon_say_cmd("", args.test_rcon_say), fp=fp)
        log(f"RCON_SAY: {'OK' if ok else 'FAIL'} -- {resp}", fp)
        if fp: fp.close()
        raise SystemExit(0 if ok else 2)

    # test rcon: arbitrary command
    if args.test_rcon_cmd:
        cmd = args.test_rcon_cmd.strip()
        if not cmd:
            log("RCON_CMD: FAIL -- empty command", fp)
            if fp: fp.close()
            raise SystemExit(2)

        ok, resp = rcon_send(cfg, cmd, fp=fp)
        log(f"RCON_CMD: {'OK' if ok else 'FAIL'} -- cmd={cmd}", fp)

        # Pretty-print JSON responses if they look like JSON
        s = (resp or "").strip()
        if s.startswith("{") or s.startswith("["):
            try:
                log("RCON_CMD: response (json):", fp)
                for line in json.dumps(json.loads(s), indent=2).splitlines():
                    log(line, fp)
            except Exception:
                log(f"RCON_CMD: response: {resp}", fp)
        else:
            log(f"RCON_CMD: response: {resp}", fp)

        if fp: fp.close()
        raise SystemExit(0 if ok else 2)

    # Bridge test mode (exit immediately after)
    if args.test_smoothrestarter or args.test_smoothrestarter_send:
        rc = test_smoothrestarter_bridge(
            cfg, server_dir, rustserver_path, fp=fp, send=bool(args.test_smoothrestarter_send)
        )
        if fp:
            fp.close()
        raise SystemExit(rc)

    if args.smooth_restart_server is not None:
        ok = request_smooth_restart(
            cfg,
            server_dir,
            rustserver_path,
            fp=fp,
            announce_message=args.smooth_restart_server,
        )
        if fp:
            fp.close()
        raise SystemExit(0 if ok else 2)

    if not (cfg.get("check_process_identity") or cfg.get("check_tcp_rcon") or cfg.get("check_lgsm_details")):
        fatal("config: at least one health check must be enabled", fp=fp)

    # if not (os.path.isfile(rustserver_path) and os.access(rustserver_path, os.X_OK)):
    #     print(f"FATAL: not executable: {rustserver_path}", file=sys.stderr)
    #     sys.exit(2)

    # fp = None
    # if cfg.get("logfile"):
    #     os.makedirs(os.path.dirname(os.path.abspath(cfg["logfile"])), exist_ok=True)
    #     fp = open(cfg["logfile"], "a", encoding="utf-8")

    # Guard: don’t allow recovery from inside screen/tmux
    if inside_screen_or_tmux() and not cfg.get("dry_run", False):
        log("WARNING: running inside screen/tmux -> forcing dry_run=true (prevents tmuxception loops)", fp)
        cfg["dry_run"] = True

    if not acquire_lock(cfg["lockfile"], fp):
        if fp:
            fp.close()
        sys.exit(1)

    # init alerts only after we actually own the lock
    init_alerts(cfg, fp)
    forced_wipe = ForcedWipeCoordinator(
        cfg,
        fp=fp,
        persist=not parse_bool(cfg.get("dry_run"), False),
    )
    STATUS_COORDINATOR = forced_wipe
    _refresh_server_restart_ledger(cfg, forced_wipe)
    last_wipe_timestamp_check = 0.0
    if (
        parse_bool(cfg.get("wipe_timestamp_rcon_enabled"), True)
        or parse_bool(
            cfg.get("wipe_timestamp_filesystem_fallback_enabled"),
            True,
        )
    ):
        wipe_timestamp_ok, _wiped_at, _wipe_detail = (
            _refresh_server_wipe_ledger(
                cfg,
                forced_wipe,
                fp=fp,
                log_failure=True,
            )
        )
        if wipe_timestamp_ok:
            last_wipe_timestamp_check = time.monotonic()

    log(f"Rust Watchdog v{__version__} by FlyingFathead started (dry_run={cfg['dry_run']})", fp)
    log(f"server_dir={server_dir} identity={cfg['identity']}", fp)
    log(f"recovery_steps={cfg['recovery_steps']}", fp)
    log(
        f"forced_wipe_action={forced_wipe.action} "
        f"state_file={forced_wipe.state_path or '(disabled)'}",
        fp,
    )

    alert(
        "watchdog_started",
        fp=fp,
        identity=cfg.get("identity"),
        dry_run=cfg.get("dry_run"),
        pid=os.getpid(),
        started_at=ts(),
    )

    # One-time forced wipe info on startup
    forced_wipe_enabled = parse_bool(cfg.get("enable_forced_wipe_highlight"), True)
    last_forced_wipe_log = 0.0
    forced_wipe_log_interval = int(cfg.get("forced_wipe_log_interval_seconds", 3600))
    if forced_wipe_enabled:
        try:
            forced_wipe_log_interval, _ = forced_wipe_highlight_log(cfg, fp=fp)
            last_forced_wipe_log = time.monotonic()
        except Exception as e:
            log(f"FORCED_WIPE: WARNING: failed to compute/log forced wipe schedule: {e}", fp)

    try:
        maybe_emit_forced_wipe_reminder(forced_wipe, cfg, fp=fp)
    except Exception as e:
        log(f"FORCED_WIPE: WARNING: reminder check failed: {e}", fp)

    # One-time SmoothRestarter info on startup (even if bridge is disabled)
    if parse_bool(cfg.get("smoothrestarter_check_loaded"), False) or parse_bool(cfg.get("enable_smoothrestarter_bridge"), False):
        ok, cfg_ok, sr_cfg, sr_plugin, notes = smoothrestarter_available(server_dir, cfg)
        for n in notes:
            log(f"SMOOTH_BRIDGE: {n}", fp)

        # Keep the original "expected paths" (what we will look for)
        log(f"SMOOTH_BRIDGE: expected plugin path: {sr_plugin}", fp)
        log(f"SMOOTH_BRIDGE: expected config path: {sr_cfg}", fp)

        # Add explicit existence verdicts
        plugin_state = "FOUND" if ok else "MISSING"
        cfg_state = "FOUND" if cfg_ok else "MISSING"
        log(f"SMOOTH_BRIDGE: plugin: {plugin_state}", fp)
        log(f"SMOOTH_BRIDGE: config: {cfg_state}", fp)

        # And a human summary
        if not ok:
            log(f"SMOOTH_BRIDGE: SmoothRestarter not installed (plugin missing). Get it from: {SMOOTHRESTARTER_URL}", fp)
        elif not cfg_ok:
            log(f"SMOOTH_BRIDGE: SmoothRestarter installed (plugin found), but config missing (OK on first run): {sr_cfg}", fp)
        else:
            log("SMOOTH_BRIDGE: SmoothRestarter installed (plugin+config found) -- bridge ready.", fp)

    if cfg.get("_recovery_steps_original") != cfg.get("recovery_steps"):
        log(
            f"NOTE: recovery_steps filtered by toggles "
            f"(enable_server_update={cfg.get('enable_server_update', True)}, "
            f"enable_mods_update={cfg.get('enable_mods_update', True)}): "
            f"{cfg.get('_recovery_steps_original')} -> {cfg.get('recovery_steps')}",
            fp
        )

    down_streak = 0
    paused = False

    last_update_check = 0.0
    last_restart_request = 0.0

    startup_ok_sent = False

    try:
        while True:
            if stop_requested:
                log("Stop requested -- exiting watchdog loop", fp)
                break

            # Reminder delivery is independent of health/recovery and remains
            # active even while a pause file suppresses operational actions.
            try:
                maybe_emit_forced_wipe_reminder(forced_wipe, cfg, fp=fp)
            except Exception as e:
                log(f"FORCED_WIPE: WARNING: reminder check failed: {e}", fp)

            pause_file = cfg.get("pause_file")

            if pause_file and os.path.exists(pause_file):
                if not paused:
                    log(f"PAUSED: {pause_file} exists -- skipping checks/recovery", fp)
                    paused = True
                    down_streak = 0  # optional: don't "resume" mid-DOWN streak                 
                if args.once:
                    break
                sleep_interruptible(int(cfg["interval_seconds"]))
                continue
            else:
                if paused:
                    log(f"UNPAUSED: {pause_file} removed -- resuming", fp)
                    paused = False
                    down_streak = 0

            # ---------------------------------------------------------
            # DUPE CHECKS
            # ---------------------------------------------------------

            # 0) Duplicate RustDedicated identity guard (pause/fatal/kill_extra etc)
            if not handle_duplicate_rustdedicated(cfg, fp=fp):
                # policy decided to skip actions this tick
                if args.once:
                    break
                sleep_interruptible(int(cfg["interval_seconds"]))
                continue

            state, evidence = health_report(cfg, server_dir, rustserver_path, fp)
            log(f"HEALTH: {state}", fp)
            for line in evidence:
                log(f"  {line}", fp)
            if state == "RUNNING":
                _refresh_server_restart_ledger(cfg, forced_wipe)
                if (
                    parse_bool(
                        cfg.get("wipe_timestamp_rcon_enabled"),
                        True,
                    )
                    or parse_bool(
                        cfg.get(
                            "wipe_timestamp_filesystem_fallback_enabled"
                        ),
                        True,
                    )
                ):
                    now_wipe_check = time.monotonic()
                    wipe_check_interval = int(
                        cfg.get(
                            "wipe_timestamp_interval_seconds",
                            600,
                        )
                    )
                    if (
                        last_wipe_timestamp_check <= 0
                        or (
                            now_wipe_check
                            - last_wipe_timestamp_check
                        )
                        >= wipe_check_interval
                    ):
                        _refresh_server_wipe_ledger(
                            cfg,
                            forced_wipe,
                            fp=fp,
                        )
                        last_wipe_timestamp_check = now_wipe_check

            # Send startup OK
            if state == "RUNNING" and not startup_ok_sent:
                alert(
                    "startup_ok",
                    level="info",
                    fp=fp,
                    identity=cfg.get("identity"),
                )
                startup_ok_sent = True

            if (
                state == "RUNNING"
                and forced_wipe.enabled
                and not parse_bool(cfg.get("dry_run"), False)
                and forced_wipe.finish_if_running(datetime.now(timezone.utc))
            ):
                log(
                    "FORCED_WIPE: server healthy; cycle marked completed",
                    fp,
                )
                alert(
                    "forced_wipe_completed",
                    "Automatic forced wipe completed and server is healthy",
                    level="info",
                    fp=fp,
                    identity=cfg.get("identity"),
                    cycle=forced_wipe.state.get("cycle"),
                    action=forced_wipe.action,
                    candidate_build=forced_wipe.state.get(
                        "candidate_remote_build"
                    ),
                )

            # Forced wipe highlighter (rate-limited)
            if forced_wipe_enabled:
                nowm = time.monotonic()
                if (nowm - last_forced_wipe_log) >= float(forced_wipe_log_interval):
                    try:
                        forced_wipe_log_interval, _active = forced_wipe_highlight_log(cfg, fp=fp)
                    except Exception as e:
                        log(f"FORCED_WIPE: WARNING: schedule calc failed: {e}", fp)
                        # back off so we don't spam exceptions
                        forced_wipe_log_interval = max(3600, forced_wipe_log_interval)
                    last_forced_wipe_log = nowm

            if state == "DOWN":
                down_streak += 1
                log(f"DOWN streak: {down_streak}/{cfg['down_confirmations']}", fp)
            else:
                down_streak = 0

            # ---------------------------------------------------------
            # Optional: watch for updates while server is RUNNING
            # If update is found -> optionally request SmoothRestarter
            # ---------------------------------------------------------
            if state == "RUNNING" and parse_bool(cfg.get("enable_update_watch"), False):
                now = time.monotonic()
                interval = int(cfg.get("update_check_interval_seconds", 600))

                if (now - last_update_check) >= interval:
                    last_update_check = now

                    # If the bridge is enabled, warn on every update-check tick
                    # if SmoothRestarter isn't installed (non-fatal).
                    if parse_bool(cfg.get("enable_smoothrestarter_bridge"), False):
                        ok, cfg_ok, sr_cfg, sr_plugin, notes = smoothrestarter_available(server_dir, cfg)
                        for n in notes:
                            log(f"SMOOTH_BRIDGE: {n}", fp)
                        if not ok:
                            log(f"SMOOTH_BRIDGE: enabled but SmoothRestarter plugin not found: {sr_plugin}", fp)
                        elif not cfg_ok:
                            log(f"SMOOTH_BRIDGE: NOTE: SmoothRestarter config missing (may be first run): {sr_cfg}", fp)

                    update_result = check_server_update_via_lgsm(
                        cfg, server_dir, rustserver_path, fp
                    )
                    now_utc = datetime.now(timezone.utc)
                    wipe_decision = forced_wipe.observe_update(
                        update_result, now_utc
                    )
                    verdict = update_result.verdict
                    calendar_fallback_due = bool(
                        wipe_decision.action_due
                        and wipe_decision.armed_trigger
                        == "window-end-fallback"
                    )

                    if wipe_decision.armed_now:
                        armed_description = (
                            "window-end calendar fallback"
                            if wipe_decision.armed_trigger
                            == "window-end-fallback"
                            else "monthly build"
                        )
                        log(
                            "FORCED_WIPE: ARMED "
                            f"cycle={wipe_decision.cycle} "
                            f"trigger={wipe_decision.armed_trigger or '?'} "
                            f"candidate_build="
                            f"{wipe_decision.candidate_remote_build or '-'} "
                            f"({wipe_decision.reason})",
                            fp,
                        )
                        alert(
                            "forced_wipe_armed",
                            f"Forced-wipe {armed_description} armed",
                            level="warning",
                            fp=fp,
                            identity=cfg.get("identity"),
                            cycle=wipe_decision.cycle,
                            scheduled_utc=wipe_decision.scheduled_utc,
                            candidate_build=wipe_decision.candidate_remote_build,
                            armed_trigger=wipe_decision.armed_trigger,
                            reason=wipe_decision.reason,
                        )

                    hold = wipe_decision.hold
                    reason = wipe_decision.reason
                    if not forced_wipe.enabled:
                        hold, reason = in_forced_wipe_update_hold(
                            cfg, now_utc, fp=fp
                        )

                    should_act = bool(
                        verdict is True or wipe_decision.action_due
                    )

                    if should_act:
                        if hold:
                            log(f"UPDATE_WATCH: update available, but HOLDING until wipe ({reason})", fp)
                            alert(
                                "update_held",
                                "Rust update detected, but restart is being held",
                                level="info",
                                fp=fp,
                                identity=cfg.get("identity"),
                                hold_reason=reason,
                                local_build=update_result.local_build,
                                remote_build=update_result.remote_build,
                            )
                        else:
                            action_reason = (
                                "forced-wipe window-end calendar fallback"
                                if calendar_fallback_due
                                else "armed monthly forced-wipe build"
                                if wipe_decision.action_due
                                else "update detected"
                            )
                            log(
                                f"UPDATE_WATCH: action required ({action_reason})",
                                fp,
                            )
                            if verdict is True:
                                alert(
                                    "update_available",
                                    "Rust update detected",
                                    level="info",
                                    fp=fp,
                                    identity=cfg.get("identity"),
                                    source="linuxgsm check-update",
                                    local_build=update_result.local_build,
                                    remote_build=update_result.remote_build,
                                    forced_wipe_pending=wipe_decision.pending,
                                )

                            cooldown = int(cfg.get("restart_request_cooldown_seconds", 3600))
                            if (now - last_restart_request) < cooldown:
                                left = int(cooldown - (now - last_restart_request))
                                log(f"UPDATE_WATCH: restart cooldown active ({left}s left) -- not acting again yet", fp)
                            else:
                                # ---------------------------------------------------------
                                # ALWAYS announce "reboot incoming", regardless of SR usage.
                                # ---------------------------------------------------------
                                announce_message = (
                                    f"Facepunch forced-wipe cutoff reached -- "
                                    f"{forced_wipe.action} incoming."
                                    if calendar_fallback_due
                                    else str(
                                        cfg.get(
                                            "update_watch_announce_message",
                                            "",
                                        )
                                    ).strip()
                                )
                                best_effort_rcon_say(
                                    cfg, announce_message, fp=fp
                                )

                                # If SR is enabled, SR will do the real countdown, but we still
                                # emit the "time until..." line + the final reboot message once.
                                if parse_bool(cfg.get("enable_smoothrestarter_bridge"), False):
                                    sr_delay = int(cfg.get("smoothrestarter_restart_delay_seconds", 300))

                                    # One-line "time until..." even when SR is used
                                    try:
                                        tmpl = (
                                            f"Time until forced "
                                            f"{forced_wipe.action}: "
                                            f"{{seconds}} seconds."
                                            if calendar_fallback_due
                                            else str(cfg.get(
                                                "update_watch_countdown_template",
                                                "Time until server update and "
                                                "restart: {seconds} seconds."
                                            ))
                                        )
                                        best_effort_rcon_say(cfg, tmpl.format(seconds=sr_delay), fp=fp)
                                    except Exception:
                                        best_effort_rcon_say(
                                            cfg,
                                            f"Time until server update and restart: {sr_delay} seconds.",
                                            fp=fp
                                        )

                                    # And your required final message (best-effort)
                                    best_effort_rcon_say(
                                        cfg,
                                        (
                                            f"Forced {forced_wipe.action} "
                                            f"starting now -- come back in a "
                                            f"few minutes!"
                                            if calendar_fallback_due
                                            else str(
                                                cfg.get(
                                                    "update_watch_final_message",
                                                    "",
                                                )
                                            ).strip()
                                        ),
                                        fp=fp
                                    )

                                    ok = request_smooth_restart(cfg, server_dir, rustserver_path, fp)

                                    if ok:
                                        last_restart_request = now
                                        log(
                                            f"SMOOTH_BRIDGE: requested SmoothRestarter restart "
                                            f"(delay={sr_delay}s)",
                                            fp
                                        )
                                        alert(
                                            "restart_requested",
                                            "SmoothRestarter restart requested",
                                            level="warning",
                                            fp=fp,
                                            identity=cfg.get("identity"),
                                            delay_seconds=sr_delay,
                                            path="smoothrestarter",
                                            reason=action_reason,
                                        )

                                    else:
                                        log("SMOOTH_BRIDGE: failed -> falling back to no-SR countdown + restart NOW", fp)
                                        alert(
                                            "restart_requested",
                                            "Immediate restart/update sequence requested",
                                            level="warning",
                                            fp=fp,
                                            identity=cfg.get("identity"),
                                            path="watchdog-fallback",
                                            reason=action_reason,
                                        )
                                        update_watch_fallback_restart_now(
                                            cfg,
                                            server_dir,
                                            rustserver_path,
                                            fp=fp,
                                            forced_wipe=forced_wipe,
                                        )

                                        last_restart_request = now
                                        down_streak = 0
                                        log(f"Cooldown {cfg['cooldown_seconds']}s after update-watch fallback restart", fp)
                                        sleep_interruptible(int(cfg["cooldown_seconds"]))
                                        if args.once:
                                            break
                                        continue

                                else:
                                    # No SR: do crude countdown + stop/update/mu/restart immediately
                                    alert(
                                        "restart_requested",
                                        "Immediate restart/update sequence requested",
                                        level="warning",
                                        fp=fp,
                                        identity=cfg.get("identity"),
                                        path="watchdog-fallback",
                                        reason=action_reason,
                                    )
                                    update_watch_fallback_restart_now(
                                        cfg,
                                        server_dir,
                                        rustserver_path,
                                        fp=fp,
                                        forced_wipe=forced_wipe,
                                    )

                                    last_restart_request = now
                                    down_streak = 0
                                    log(f"Cooldown {cfg['cooldown_seconds']}s after update-watch fallback restart", fp)
                                    sleep_interruptible(int(cfg["cooldown_seconds"]))
                                    if args.once:
                                        break
                                    continue

                    elif verdict is False:
                        log("UPDATE_WATCH: no update available", fp)
                    else:
                        log("UPDATE_WATCH: unknown (could not determine update availability)", fp)

            if state == "DOWN" and down_streak >= int(cfg["down_confirmations"]):
                log("CONFIRMED DOWN -> recovery sequence", fp)
                primary = next((l for l in evidence if l.startswith("PRIMARY_CAUSE:")), "")

                alert(
                    "server_down",
                    f"Server '{cfg.get('identity')}' confirmed DOWN -- starting recovery",
                    level="warning",
                    fp=fp,
                    identity=cfg.get("identity"),
                    primary_cause=primary,
                    reason="health_report down_confirmed",
                )

                if forced_wipe.needs_recovery(datetime.now(timezone.utc)):
                    steps = [
                        "stop",
                        "backup",
                        "update",
                        "mu",
                        forced_wipe.action,
                        "start",
                    ]
                    execute_forced_wipe_sequence(
                        cfg,
                        server_dir,
                        rustserver_path,
                        forced_wipe,
                        server_already_down=True,
                        fp=fp,
                    )
                else:
                    steps = list(cfg["recovery_steps"])

                    if parse_bool(cfg.get("forced_wipe_recovery_restart_only_prewipe"), False):
                        hold, reason = in_forced_wipe_update_hold(cfg, datetime.now(timezone.utc), fp=fp)
                        if hold:
                            # Drop update/mu during pre-wipe hold; keep server alive without chasing builds
                            steps = [s for s in steps if s.strip().lower() not in ("update", "mu")]
                            if not steps:
                                steps = ["restart"]
                            log(f"RECOVERY: pre-wipe HOLD active -> skipping update/mu ({reason})", fp)

                    for step in steps:
                        if stop_requested:
                            log("Stop requested -- aborting recovery sequence", fp)
                            break

                        step = step.strip().lower()
                        timeout = cfg["timeouts"].get(step, None)
                        try:
                            run_cmd([rustserver_path, step], server_dir, fp, timeout=timeout, dry_run=cfg["dry_run"])
                        except TimeoutError as e:
                            log(f"STEP TIMEOUT ({step}): {e}", fp)
                        except Exception as e:
                            log(f"STEP ERROR ({step}): {e}", fp)

                if stop_requested:
                    log("Stop requested -- skipping cooldown and exiting", fp)
                    break

                alert(
                    "recovery_attempted",
                    f"Recovery sequence finished for '{cfg.get('identity')}'",
                    level="warning",
                    fp=fp,
                    identity=cfg.get("identity"),
                    steps=steps,
                )

                cooldown = int(cfg.get("cooldown_seconds", 0) or 0)
                if cooldown > 0:
                    log(f"Cooldown {cooldown}s after recovery -- waiting before health re-check", fp)
                    sleep_interruptible(cooldown)

                if stop_requested:
                    log("Stop requested during cooldown -- exiting", fp)
                    break

                st2, ev2 = health_report(cfg, server_dir, rustserver_path, fp)

                if st2 == "RUNNING":
                    _refresh_server_restart_ledger(cfg, forced_wipe)
                    alert(
                        "server_recovered",
                        f"Server '{cfg.get('identity')}' is RUNNING -- cooldown passed",
                        level="info",
                        fp=fp,
                        identity=cfg.get("identity"),
                    )
                    if (
                        forced_wipe.enabled
                        and not parse_bool(cfg.get("dry_run"), False)
                        and forced_wipe.finish_if_running(
                            datetime.now(timezone.utc)
                        )
                    ):
                        log(
                            "FORCED_WIPE: server healthy; cycle marked completed",
                            fp,
                        )
                        alert(
                            "forced_wipe_completed",
                            "Automatic forced wipe completed and server is healthy",
                            level="info",
                            fp=fp,
                            identity=cfg.get("identity"),
                            cycle=forced_wipe.state.get("cycle"),
                            action=forced_wipe.action,
                            candidate_build=forced_wipe.state.get(
                                "candidate_remote_build"
                            ),
                        )
                else:
                    primary = next((l for l in ev2 if l.startswith("PRIMARY_CAUSE:")), "")
                    alert(
                        "recovery_failed",
                        f"Recovery finished, but server health is {st2}",
                        level="error",
                        fp=fp,
                        identity=cfg.get("identity"),
                        primary_cause=primary,
                    )

                down_streak = 0

            else:
                if args.once:
                    break
                sleep_interruptible(int(cfg["interval_seconds"]))
                if args.once:
                    break
    finally:
        STATUS_COORDINATOR = None
        
        # // try alerts
        try:
            if ALERTS:
                if hasattr(ALERTS, "close"):
                    ALERTS.close()
                elif hasattr(ALERTS, "stop"):
                    ALERTS.stop()
        except Exception:
            pass

        release_lock(cfg["lockfile"])
        if fp:
            fp.close()

if __name__ == "__main__":
    main()
