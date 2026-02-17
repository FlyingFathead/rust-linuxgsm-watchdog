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

__version__ = "0.3.2"

SMOOTHRESTARTER_URL = "https://umod.org/plugins/smooth-restarter"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FOR_HINTS = None
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

}

STATUS_RE = re.compile(r"^\s*Status:\s*(\S+)\s*$", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
UPDATE_YES_RE = re.compile(r"\b(update available|update required|available:\s*yes)\b", re.IGNORECASE)
UPDATE_NO_RE  = re.compile(r"\b(no update available|available:\s*no|already up to date|up to date)\b", re.IGNORECASE)
RCON_PW_RE = re.compile(r'(\+rcon\.password\s+)(\".*?\"|\S+)', re.IGNORECASE)
UNKNOWN_CMD_RE = re.compile(r"\bunknown\s+(command|console\s+command)\b", re.IGNORECASE)
SR_NAME_RE = re.compile(r"\bsmooth\s*restarter\b", re.IGNORECASE)

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
        # log_fn(level, msg)
        ALERTS = AlertManager(
            cfg,
            log_fn=lambda level, msg: log(f"ALERTS: {level}: {msg}", fp)
        )
        log("ALERTS: enabled", fp)
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
        lvl = str(level or "info").upper()
        ALERTS.emit(
            event=str(event or "event"),
            level=lvl,
            title=str(event or "event"),
            text=str(message or ""),
            **ctx
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

def next_forced_wipe(now_utc: datetime, cfg, fp=None):
    """
    Compute next "monthly forced wipe baseline":
      first Thursday of month @ forced_wipe_hour:forced_wipe_minute in forced_wipe_tz.

    Returns dict with:
      - wipe_tz_dt (aware, in forced wipe tz)
      - wipe_utc_dt (aware, in UTC)
      - tz (tz object)
    """
    tz_name = str(cfg.get("forced_wipe_tz") or "Europe/London")
    tz = _get_tz(tz_name, fp=fp)

    hour = int(cfg.get("forced_wipe_hour", 19))
    minute = int(cfg.get("forced_wipe_minute", 0))

    now_tz = now_utc.astimezone(tz)
    cand = _first_thursday_dt(now_tz.year, now_tz.month, hour=hour, minute=minute, tz=tz)
    if now_tz >= cand:
        # next month
        y = now_tz.year
        m = now_tz.month + 1
        if m == 13:
            y += 1
            m = 1
        cand = _first_thursday_dt(y, m, hour=hour, minute=minute, tz=tz)

    cand_utc = cand.astimezone(timezone.utc)
    return {"wipe_tz_dt": cand, "wipe_utc_dt": cand_utc, "tz": tz, "tz_name": tz_name}

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

    info = next_forced_wipe(now_utc, cfg, fp=fp)
    wipe_utc = info["wipe_utc_dt"]
    dt = wipe_utc - now_utc

    if timedelta(0) < dt <= timedelta(minutes=hold_m):
        when = info["wipe_tz_dt"].strftime("%Y-%m-%d %H:%M")
        return (True, f"within {hold_m}m of wipe ({when} {info.get('tz_name','')})")

    return (False, "")

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

    for k in ("server_dir", "lockfile", "logfile", "pause_file"):
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
def load_cfg(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # shallow merge + nested merge for timeouts
        cfg.update({k: v for k, v in data.items() if k != "timeouts"})
        if "timeouts" in data and isinstance(data["timeouts"], dict):
            t = dict(cfg.get("timeouts", {}))
            t.update(data["timeouts"])
            cfg["timeouts"] = t
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

    # 6) Summary
    log("PRECHECK: checklist results:", fp)
    log(f"  OK: server_dir writable: {server_dir}", fp)
    log(f"  OK: rustserver executable: {rustserver_path}", fp)
    log(f"  OK: lockfile dir writable: {lock_dir}", fp)
    if pause_file:
        log(f"  OK: pause_file parent dir writable: {os.path.dirname(os.path.abspath(pause_file))}", fp)
    else:
        log("  NOTE: pause_file disabled (empty)", fp)

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

def rcon_send(cfg, command: str, fp=None):
    """
    Send a command via Rust WebRCON and return (ok, response_text).

    IMPORTANT:
    - Rust WebRCON's Identifier is effectively a 32-bit-ish integer in practice.
      Using epoch *milliseconds* can overflow/mismatch and you'll never see a match,
      which breaks plugin checks (oxide.plugins / sr status).
    - We therefore generate a 31-bit safe Identifier and wait for the matching frame.
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

        deadline = time.monotonic() + 5.0
        last = ""
        candidate_generic = ""

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
                return (True, resp)

            # Sometimes we might get a list/array; scan it for our Identifier
            if isinstance(obj, list):
                for it in obj:
                    if not isinstance(it, dict):
                        continue
                    rid = it.get("Identifier", it.get("identifier", None))
                    try:
                        rid_i = int(rid) if rid is not None else None
                    except Exception:
                        rid_i = None
                    if rid_i == ident:
                        return (True, resp)
                continue

            if not isinstance(obj, dict):
                continue

            rid = obj.get("Identifier", obj.get("identifier", None))
            try:
                rid_i = int(rid) if rid is not None else None
            except Exception:
                rid_i = None

            if rid_i == ident:
                return (True, resp)

            # Ignore noise frames
            t = str(obj.get("Type", obj.get("type", "")) or "").strip().lower()
            if t in ("serverinfo", "chat"):
                continue

            # Fallback candidate: Generic frames with a Message (some servers are sloppy about Identifier)
            msg = obj.get("Message", obj.get("message", ""))
            if msg and (t == "" or t == "generic"):
                candidate_generic = resp
                continue

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

def request_smooth_restart(cfg, server_dir, rustserver_path, fp=None):
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
        return False
    if not cfg_ok:
        log(f"SMOOTH_BRIDGE: NOTE: SmoothRestarter config missing (may be first run): {sr_cfg}", fp)

    delay = int(cfg.get("smoothrestarter_restart_delay_seconds", 300))
    template = (cfg.get("smoothrestarter_console_cmd") or "srestart restart {delay}").strip()
    cmd = template.format(delay=delay) if "{delay}" in template else f"{template} {delay}"

    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        log(f"SMOOTH_BRIDGE: FAIL: websocket-client missing ({ws_err}) -- cannot use RCON", fp)
        return False

    ok_r, resp = rcon_send(cfg, cmd, fp=fp)
    if ok_r:
        log(f"SMOOTH_BRIDGE: RCON OK: {strip_ansi(resp).strip()}", fp)
        return True

    log(f"SMOOTH_BRIDGE: RCON FAIL: {resp}", fp)
    return False

def check_server_update_via_lgsm(cfg, server_dir, rustserver_path, fp=None):
    """
    Run LinuxGSM check-update (or cu) and interpret output.
    Returns: True/False/None
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

        verdict = parse_update_available(out or "")
        if verdict is not None:
            return verdict

        # Can't tell, but command ran
        if out:
            sample = "\n".join(strip_ansi(out).splitlines()[:8])
            log(f"UPDATE_WATCH: could not interpret check-update output. First lines:\n{sample}", fp)
        return None

    log("UPDATE_WATCH: neither 'check-update' nor 'cu' seems available in this LinuxGSM script", fp)
    return None

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

def update_watch_fallback_restart_now(cfg, server_dir, rustserver_path, fp=None):
    """
    No-SR path (or SR failed):
      - announce (best-effort)
      - crude countdown
      - final message
      - stop + update + mu + restart
    """
    # Announce (best-effort)
    best_effort_rcon_say(cfg, str(cfg.get("update_watch_announce_message", "")).strip(), fp=fp)

    # Countdown
    update_watch_no_sr_countdown(cfg, fp=fp)

    # Final message (best-effort)
    best_effort_rcon_say(cfg, str(cfg.get("update_watch_final_message", "")).strip(), fp=fp)

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(PROJECT_DIR, "rust_watchdog.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--version", action="store_true", help="print version and exit")
    ap.add_argument("--test-rcon-say", metavar="MSG",
                help="send a global chat message via RCON (no plugins required) and exit")
    ap.add_argument("--test-rcon-cmd", metavar="CMD",
                help="send an arbitrary RCON command and print the response; then exit")
    ap.add_argument("--test-smoothrestarter", action="store_true",
                    help="validate SmoothRestarter bridge wiring and print what would be sent; then exit")
    ap.add_argument("--test-smoothrestarter-send", action="store_true",
        help="same as --test-smoothrestarter but actually sends the ceremony via RCON; then exit")
    args = ap.parse_args()

    if args.version:
        print(__version__)
        return

    cfg = load_cfg(args.config)
    cfg = normalize_cfg_paths(cfg, args.config)

    global CFG_FOR_HINTS
    CFG_FOR_HINTS = cfg
    
    apply_recovery_toggles(cfg)

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

    # init alerts if in use
    init_alerts(cfg, fp)
    alert("watchdog_start", f"rust-linuxgsm-watchdog v{__version__} started", fp=fp,
        identity=cfg.get("identity"), dry_run=cfg.get("dry_run"))

    # One-time dependency hint
    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        log(
            f"DEPS: websocket-client missing ({ws_err}) -- RCON features disabled "
            f"(--test-rcon-say and RCON SmoothRestarter bridge won't work).",
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
        sys.exit(1)

    log(f"Rust Watchdog v{__version__} by FlyingFathead started (dry_run={cfg['dry_run']})", fp)
    log(f"server_dir={server_dir} identity={cfg['identity']}", fp)
    log(f"recovery_steps={cfg['recovery_steps']}", fp)

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

    try:
        while True:
            if stop_requested:
                log("Stop requested -- exiting watchdog loop", fp)
                break

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

                    verdict = check_server_update_via_lgsm(cfg, server_dir, rustserver_path, fp)

                    hold, reason = in_forced_wipe_update_hold(cfg, datetime.now(timezone.utc), fp=fp)

                    if verdict is True:
                        if hold:
                            log(f"UPDATE_WATCH: update available, but HOLDING until wipe ({reason})", fp)
                        else:
                            log("UPDATE_WATCH: update available", fp)

                            cooldown = int(cfg.get("restart_request_cooldown_seconds", 3600))
                            if (now - last_restart_request) < cooldown:
                                left = int(cooldown - (now - last_restart_request))
                                log(f"UPDATE_WATCH: restart cooldown active ({left}s left) -- not acting again yet", fp)
                            else:
                                # ---------------------------------------------------------
                                # ALWAYS announce "reboot incoming", regardless of SR usage.
                                # ---------------------------------------------------------
                                best_effort_rcon_say(
                                    cfg,
                                    str(cfg.get("update_watch_announce_message", "")).strip(),
                                    fp=fp
                                )

                                # If SR is enabled, SR will do the real countdown, but we still
                                # emit the "time until..." line + the final reboot message once.
                                if parse_bool(cfg.get("enable_smoothrestarter_bridge"), False):
                                    sr_delay = int(cfg.get("smoothrestarter_restart_delay_seconds", 300))

                                    # One-line "time until..." even when SR is used
                                    try:
                                        tmpl = str(cfg.get(
                                            "update_watch_countdown_template",
                                            "Time until server update and restart: {seconds} seconds."
                                        ))
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
                                        str(cfg.get("update_watch_final_message", "")).strip(),
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
                                    else:
                                        log("SMOOTH_BRIDGE: failed -> falling back to no-SR countdown + restart NOW", fp)
                                        update_watch_fallback_restart_now(cfg, server_dir, rustserver_path, fp=fp)

                                        last_restart_request = now
                                        down_streak = 0
                                        log(f"Cooldown {cfg['cooldown_seconds']}s after update-watch fallback restart", fp)
                                        sleep_interruptible(int(cfg["cooldown_seconds"]))
                                        if args.once:
                                            break
                                        continue

                                else:
                                    # No SR: do crude countdown + stop/update/mu/restart immediately
                                    update_watch_fallback_restart_now(cfg, server_dir, rustserver_path, fp=fp)

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

                alert(
                    "confirmed_down",
                    f"Server '{cfg.get('identity')}' confirmed DOWN -- starting recovery",
                    level="warning",
                    fp=fp,
                    identity=cfg.get("identity")
                )

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
                    f"Recovery sequence finished for '{cfg.get('identity')}' -- steps={steps}",
                    level="info",
                    fp=fp,
                    identity=cfg.get("identity"),
                    steps=steps
                )

                log(f"Cooldown {cfg['cooldown_seconds']}s after recovery attempt", fp)
                sleep_interruptible(int(cfg["cooldown_seconds"]))

                down_streak = 0

            else:
                if args.once:
                    break
                sleep_interruptible(int(cfg["interval_seconds"]))
                if args.once:
                    break
    finally:
        
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

