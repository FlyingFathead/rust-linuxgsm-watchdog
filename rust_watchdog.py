#!/usr/bin/env python3

# =============================================================
# https://github.com/FlyingFathead/rust-linuxgsm-watchdog
# -------------------------------------------------------------
# A restart & update watchdog for Rust game servers on LinuxGSM
# -------------------------------------------------------------
# FlyingFathead / 2026 / https://github.com/FlyingFathead/
# =============================================================

import argparse
import getpass
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

__version__ = "0.2.4"

SMOOTHRESTARTER_URL = "https://umod.org/plugins/smooth-restarter"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FOR_HINTS = None
DEFAULTS = {
    "server_dir": "/home/rustserver",
    "identity": "rustserver",

    "interval_seconds": 30,
    "cooldown_seconds": 120,

    "lockfile": "/tmp/rustserver_watchdog.lock",

    # NOTE: this must be a FILE path, not a directory
    "logfile": "/home/rustserver/rust-linuxgsm-watchdog/log/rust_watchdog.log",

    # Pause feature enabled by default (only pauses if the file exists)
    "pause_file": "/home/rustserver/rust-linuxgsm-watchdog/.watchdog_pause",

    # DRY RUN MODE: when true, never runs recovery steps
    "dry_run": False,

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
    # Optional: watch for LinuxGSM server updates while RUNNING
    # ---------------------------------------------------------
    "enable_update_watch": False,
    "update_check_interval_seconds": 600,
    "update_check_timeout": 60,

    # ---------------------------------------------------------
    # Optional: SmoothRestarter bridge
    # ---------------------------------------------------------
    "enable_smoothrestarter_bridge": False,
    "smoothrestarter_restart_delay_seconds": 300,
    "smoothrestarter_console_cmd": "srestart restart {delay}",

    # Rate-limit restart requests (avoid spamming SR during loops)
    "restart_request_cooldown_seconds": 3600,

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
# ---------------------------------------------------------
# Optional dependency: websocket-client (for Rust WebRCON)
# ---------------------------------------------------------
_WS_CACHE = {"checked": False, "ok": False, "err": ""}

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

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(line, fp=None):
    msg = f"[{ts()}] {line}"
    print(msg, flush=True)
    if fp:
        fp.write(msg + "\n")
        fp.flush()

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
    print('  "lockfile": "/tmp/rustserver_watchdog.lock",', file=sys.stderr)
    print('  "pause_file": ""', file=sys.stderr)
    print("}", file=sys.stderr)

    print("", file=sys.stderr)
    print("Then create the local log dir if you used ./log:", file=sys.stderr)
    print("  mkdir -p ./log", file=sys.stderr)

    print("", file=sys.stderr)
    print(sep, file=sys.stderr)
    print("If this IS the LinuxGSM host", file=sys.stderr)
    print(sep, file=sys.stderr)
    print("Run it via systemd as the LinuxGSM user so permissions match your server install.", file=sys.stderr)
    print("(See rust-watchdog.service in the repo.)", file=sys.stderr)
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

    log(f"PRECHECK: watchdog v{__version__} starting pre-flight checklist", fp)
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
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass

                # Give it a moment to die, then force-kill if needed
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and p.poll() is None:
                    time.sleep(0.2)

                if p.poll() is None:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            p.kill()
                        except Exception:
                            pass

                raise RuntimeError(f"Stop requested -- aborting: {' '.join(cmd)}")

            # Hard timeout (works even if child prints nothing)
            if timeout is not None and (time.monotonic() - start) > timeout:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)  # kill whole group
                except Exception:
                    try:
                        p.kill()  # fallback: at least kill the parent
                    except Exception:
                        pass
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

def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s or "")

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
        lines = subprocess.check_output(["pgrep", "-af", "RustDedicated"], text=True).splitlines()
    except Exception:
        return (None, None, None)

    for line in lines:
        if needle1 not in line and needle2 not in line:
            continue
        ip, port, pw = extract_rcon_from_cmdline_line(line)
        if ip and port and pw:
            return (ip, port, pw)

    return (None, None, None)

def rcon_send(cfg, command: str, fp=None):
    """
    Send a command via Rust WebRCON and return (ok, response_text).
    """
    ip, port, pw = detect_rcon_from_identity(cfg)
    if not (ip and port and pw):
        return (False, "RCON autodetect failed (missing ip/port/password)")

    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        return (False, f"websocket-client not available: {ws_err}")

    from websocket import create_connection

    url = f"ws://{ip}:{port}/{pw}"

    ident = int(time.time())  # unique-ish
    payload = {"Identifier": ident, "Message": command, "Name": "watchdog"}

    try:
        ws = create_connection(url, timeout=5)
        ws.send(json.dumps(payload))
        ws.settimeout(3)
        resp = ws.recv()
        ws.close()
        return (True, resp)
    except Exception as e:
        return (False, f"RCON send failed: {e}")

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

def smoothrestarter_available(server_dir, cfg=None):
    cfg_path, plugin_path = smoothrestarter_paths(server_dir, cfg)
    plugin_ok = os.path.isfile(plugin_path)
    cfg_ok = os.path.isfile(cfg_path)

    # Treat plugin as "available" if the plugin file exists.
    # Config may not exist yet on fresh installs.
    ok = plugin_ok
    return ok, cfg_ok, cfg_path, plugin_path

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
        lines = subprocess.check_output(["pgrep", "-af", "RustDedicated"], text=True).splitlines()
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

    Priority order:
      1) WebRCON (requires python dep: websocket-client)
      2) tmux send-keys (LinuxGSM console)
      3) screen stuff (fallback)

    Returns True if we successfully sent the command via ANY backend.
    """
    ok, cfg_ok, cfg_path, plugin_path = smoothrestarter_available(server_dir, cfg)
    if not ok:
        log(f"SMOOTH_BRIDGE: enabled but SmoothRestarter plugin not found: {plugin_path}", fp)
        return False
    if not cfg_ok:
        log(f"SMOOTH_BRIDGE: NOTE: SmoothRestarter config missing (may be first run): {cfg_path}", fp)

    delay = int(cfg.get("smoothrestarter_restart_delay_seconds", 300))
    template = (cfg.get("smoothrestarter_console_cmd") or "srestart restart {delay}").strip()
    cmd = template.format(delay=delay) if "{delay}" in template else f"{template} {delay}"

    # ---------------------------------------------------------
    # 1) Try WebRCON first (optional dependency: websocket-client)
    # ---------------------------------------------------------
    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        log(
            f"SMOOTH_BRIDGE: couldn't load websocket-client ({ws_err}) -- "
            f"RCON bridge disabled; falling back to tmux/screen",
            fp
        )
    else:
        ok_r, resp = rcon_send(cfg, cmd, fp=fp)
        if ok_r:
            log(f"SMOOTH_BRIDGE: RCON OK: {strip_ansi(resp).strip()}", fp)
            return True
        log(f"SMOOTH_BRIDGE: RCON failed: {resp} -- falling back to tmux/screen", fp)

    # ---------------------------------------------------------
    # 2) Try tmux (LinuxGSM console)
    # ---------------------------------------------------------
    l_name, prefer_session = detect_lgsm_tmux_context(cfg, fp=fp)

    t_sessions = tmux_list_sessions(l_name)
    t_target = (
        choose_tmux_target(
            cfg, rustserver_path,
            l_name=l_name,
            prefer_session=prefer_session
        )
        if t_sessions is not None else None
    )

    if t_target:
        ok_t = tmux_send_line(
            t_target, cmd,
            fp=fp,
            dry_run=cfg.get("dry_run", False),
            l_name=l_name
        )
        if ok_t:
            return True

    # ---------------------------------------------------------
    # 3) Fallback to screen
    # ---------------------------------------------------------
    s_sessions = screen_list_sessions()
    s_target = choose_screen_target(cfg, rustserver_path) if s_sessions is not None else None
    if s_target:
        ok_s = screen_send_line(s_target, cmd, fp=fp, dry_run=cfg.get("dry_run", False))
        if ok_s:
            return True

    log(f"SMOOTH_BRIDGE: no console session found / send failed. tmux={t_sessions} screen={s_sessions}", fp)
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

def check_process_identity(identity, fp=None):
    """
    Strong signal: RustDedicated process exists and commandline contains +server.identity identity
    """
    try:
        out = subprocess.check_output(["pgrep", "-af", "RustDedicated"], text=True).splitlines()
    except subprocess.CalledProcessError:
        return (False, "no RustDedicated process")

    hits = []
    needle1 = f"+server.identity {identity}"
    needle2 = f"+server.identity \"{identity}\""
    for line in out:
        if needle1 in line or needle2 in line or f"+server.identity {identity} " in line:
            hits.append(line)

    if hits:
        return (True, f"matched process: {redact_secrets(hits[0])}")
    return (False, f"RustDedicated running, but identity '{identity}' not found in cmdline")

def check_tcp(host, port, timeout_s):
    """
    Medium signal: can open TCP connection to RCON websocket port.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return (True, f"tcp connect ok {host}:{port}")
    except Exception as e:
        return (False, f"tcp connect failed {host}:{port} ({e})")

def check_lgsm_details(server_dir, rustserver_path, timeout_s):
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
        for line in p.stdout.splitlines():
            m = STATUS_RE.match(line)
            if m:
                status = m.group(1).upper()
                break
        return (status, p.returncode, p.stdout)
    except subprocess.TimeoutExpired:
        return ("UNKNOWN", "TIMEOUT", f"details timed out after {timeout_s}s")
    except Exception as e:
        return ("UNKNOWN", "ERROR", f"details error: {e}")

def inside_screen_or_tmux():
    return bool(os.environ.get("STY")) or bool(os.environ.get("TMUX"))

def build_smoothrestarter_cmd(cfg):
    delay = int(cfg.get("smoothrestarter_restart_delay_seconds", 300))
    template = (cfg.get("smoothrestarter_console_cmd") or "srestart restart {delay}").strip()
    if "{delay}" in template:
        return template.format(delay=delay)
    return f"{template} {delay}"

def test_smoothrestarter_bridge(cfg, server_dir, rustserver_path, fp=None, send=False):
    ok, cfg_ok, sr_cfg, sr_plugin = smoothrestarter_available(server_dir, cfg)
    log(f"SMOOTH_TEST: plugin path: {sr_plugin}", fp)
    log(f"SMOOTH_TEST: config path: {sr_cfg}", fp)

    ok_ws, ws_err = websocket_dep_status()
    if not ok_ws:
        log(f"SMOOTH_TEST: websocket-client missing ({ws_err}) -- RCON path unavailable", fp)
    else:
        log("SMOOTH_TEST: websocket-client OK -- RCON path available", fp)

    if not ok:
        log(f"SMOOTH_TEST: FAIL: SmoothRestarter plugin missing. Get it from: {SMOOTHRESTARTER_URL}", fp)
        return 2

    if not cfg_ok:
        log(f"SMOOTH_TEST: NOTE: SmoothRestarter config missing (may be first run): {sr_cfg}", fp)

    # Detect LinuxGSM tmux socket/session (tmux -L <name> ... -s <session>)
    l_name, prefer_session = detect_lgsm_tmux_context(cfg, fp=fp)
    if l_name:
        log(f"SMOOTH_TEST: detected LinuxGSM tmux server (-L): {l_name}", fp)
    if prefer_session:
        log(f"SMOOTH_TEST: detected LinuxGSM tmux session (-s): {prefer_session}", fp)

    t_sessions = tmux_list_sessions(l_name)
    s_sessions = screen_list_sessions()
    log(f"SMOOTH_TEST: tmux sessions: {t_sessions}", fp)
    log(f"SMOOTH_TEST: screen sessions: {s_sessions}", fp)

    backend = None
    target = None

    if t_sessions is not None:
        target = choose_tmux_target(cfg, rustserver_path, l_name=l_name, prefer_session=prefer_session)
        if target:
            backend = "tmux"

    if not target and s_sessions is not None:
        target = choose_screen_target(cfg, rustserver_path)
        if target:
            backend = "screen"

    if not target:
        log("SMOOTH_TEST: FAIL: could not pick a console target session (tmux/screen)", fp)
        return 2

    log(f"SMOOTH_TEST: chosen backend: {backend} target: {target}", fp)

    cmd = build_smoothrestarter_cmd(cfg)
    log(f"SMOOTH_TEST: would send: {cmd}", fp)

    if send:
        log("SMOOTH_TEST: SENDING command (this may start a restart countdown!)", fp)
        if backend == "tmux":
            ok_send = tmux_send_line(target, cmd, fp=fp, dry_run=False, l_name=l_name)
        else:
            ok_send = screen_send_line(target, cmd, fp=fp, dry_run=False)

        if not ok_send:
            log(f"SMOOTH_TEST: FAIL: {backend} send failed", fp)
            return 2
        log("SMOOTH_TEST: OK: command sent", fp)
    else:
        log("SMOOTH_TEST: OK: wiring looks good (dry test)", fp)

    return 0

def health_report(cfg, server_dir, rustserver_path, fp=None):
    """
    Returns (state, evidence_lines)
    state in: RUNNING, DOWN, UNKNOWN
    """
    evidence = []

    running_votes = 0
    down_votes = 0

    # 1) Process+identity (strong)
    if cfg.get("check_process_identity", True):
        ok, msg = check_process_identity(cfg["identity"], fp)
        evidence.append(f"process_identity: {'PASS' if ok else 'FAIL'} -- {msg}")
        if ok: running_votes += 2  # weight it
        else: down_votes += 1

    # 2) TCP connect to RCON port (medium)
    if cfg.get("check_tcp_rcon", True):
        ok, msg = check_tcp(cfg["rcon_host"], int(cfg["rcon_port"]), float(cfg["tcp_timeout"]))
        evidence.append(f"tcp_rcon: {'PASS' if ok else 'FAIL'} -- {msg}")
        if ok:
            running_votes += 1
        else:
            down_votes += 1

    # 3) LGSM details (weak-ish, but informative)
    if cfg.get("check_lgsm_details", True):
        status, rc, out = check_lgsm_details(server_dir, rustserver_path, int(cfg["details_timeout"]))
        evidence.append(f"lgsm_details: status={status} rc={rc}")
        # IMPORTANT: ignore rc; trust parsed status if present
        if status == "STARTED":
            running_votes += 1
        elif status == "STOPPED":
            down_votes += 1

    if running_votes > 0:
        return ("RUNNING", evidence)
    if down_votes > 0:
        return ("DOWN", evidence)
    return ("UNKNOWN", evidence)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(PROJECT_DIR, "rust_watchdog.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--version", action="store_true", help="print version and exit")
    ap.add_argument("--test-rcon-say", metavar="MSG",
                help="send a global chat message via RCON (no plugins required) and exit")
    ap.add_argument("--test-smoothrestarter", action="store_true",
                    help="validate SmoothRestarter bridge wiring and print what would be sent; then exit")
    ap.add_argument("--test-smoothrestarter-send", action="store_true",
                    help="same as --test-smoothrestarter but actually sends the command via tmux or screen; then exit")
    args = ap.parse_args()

    if args.version:
        print(__version__)
        return

    cfg = load_cfg(args.config)
    global CFG_FOR_HINTS
    CFG_FOR_HINTS = cfg
    
    apply_recovery_toggles(cfg)

    server_dir = os.path.abspath(cfg["server_dir"])
    rustserver_path = os.path.join(server_dir, "rustserver")

    # Clean shutdown behavior under systemd (SIGTERM) and Ctrl-C (SIGINT)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    # Pre-flight checklist (also opens logfile if enabled)
    fp = preflight_or_die(cfg, server_dir, rustserver_path)

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
        msg = args.test_rcon_say.replace('"', '\\"')
        ok, resp = rcon_send(cfg, f'global.say "{msg}"', fp=fp)
        log(f"RCON_SAY: {'OK' if ok else 'FAIL'} -- {resp}", fp)
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

    log(f"Watchdog v{__version__} started (dry_run={cfg['dry_run']})", fp)
    log(f"server_dir={server_dir} identity={cfg['identity']}", fp)
    log(f"recovery_steps={cfg['recovery_steps']}", fp)

    # One-time SmoothRestarter info on startup (only if bridge is enabled)
    if parse_bool(cfg.get("enable_smoothrestarter_bridge"), False):
        ok, cfg_ok, sr_cfg, sr_plugin = smoothrestarter_available(server_dir, cfg)
        log(f"SMOOTH_BRIDGE: expected plugin path: {sr_plugin}", fp)
        log(f"SMOOTH_BRIDGE: expected config path: {sr_cfg}", fp)

        if not ok:
            log(f"SMOOTH_BRIDGE: SmoothRestarter not installed (plugin missing). Get it from: {SMOOTHRESTARTER_URL}", fp)
        elif not cfg_ok:
            log(f"SMOOTH_BRIDGE: NOTE: SmoothRestarter config missing (may be first run): {sr_cfg}", fp)

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

            state, evidence = health_report(cfg, server_dir, rustserver_path, fp)
            log(f"HEALTH: {state}", fp)
            for line in evidence:
                log(f"  {line}", fp)

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
                        ok, cfg_ok, sr_cfg, sr_plugin = smoothrestarter_available(server_dir, cfg)
                        if not ok:
                            log(f"SMOOTH_BRIDGE: enabled but SmoothRestarter plugin not found: {sr_plugin}", fp)
                        elif not cfg_ok:
                            log(f"SMOOTH_BRIDGE: NOTE: SmoothRestarter config missing (may be first run): {sr_cfg}", fp)

                    verdict = check_server_update_via_lgsm(cfg, server_dir, rustserver_path, fp)

                    if verdict is True:
                        log("UPDATE_WATCH: update available", fp)

                        if parse_bool(cfg.get("enable_smoothrestarter_bridge"), False):
                            cooldown = int(cfg.get("restart_request_cooldown_seconds", 3600))
                            if (now - last_restart_request) < cooldown:
                                left = int(cooldown - (now - last_restart_request))
                                log(f"SMOOTH_BRIDGE: restart request cooldown active ({left}s left) -- not requesting again", fp)
                            else:
                                ok = request_smooth_restart(cfg, server_dir, rustserver_path, fp)
                                if ok:
                                    last_restart_request = now
                                    log(
                                        f"SMOOTH_BRIDGE: requested SmoothRestarter restart "
                                        f"(delay={int(cfg.get('smoothrestarter_restart_delay_seconds', 300))}s)",
                                        fp
                                    )
                                else:
                                    log("SMOOTH_BRIDGE: failed to request SmoothRestarter restart", fp)
                        else:
                            log("UPDATE_WATCH: SmoothRestarter bridge disabled; no graceful restart requested", fp)

                    elif verdict is False:
                        log("UPDATE_WATCH: no update available", fp)
                    else:
                        log("UPDATE_WATCH: unknown (could not determine update availability)", fp)

            if state == "DOWN" and down_streak >= int(cfg["down_confirmations"]):
                log("CONFIRMED DOWN -> recovery sequence", fp)
                for step in cfg["recovery_steps"]:
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
        release_lock(cfg["lockfile"])
        if fp:
            fp.close()

if __name__ == "__main__":
    main()

