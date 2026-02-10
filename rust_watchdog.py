#!/usr/bin/env python3

# =============================================================
# https://github.com/FlyingFathead/rust-linuxgsm-watchdog
# A restart & update watchdog for Rust game servers on LinuxGSM
# FlyingFathead / 2026 / https://github.com/FlyingFathead/
# =============================================================

import argparse
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

__version__ = "0.2.1"

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
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
    }
}

STATUS_RE = re.compile(r"^\s*Status:\s*(\S+)\s*$", re.IGNORECASE)

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
        return (True, f"matched process: {hits[0]}")
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
    args = ap.parse_args()

    if args.version:
        print(__version__)
        return

    cfg = load_cfg(args.config)
    server_dir = os.path.abspath(cfg["server_dir"])
    rustserver_path = os.path.join(server_dir, "rustserver")

    if not (cfg.get("check_process_identity") or cfg.get("check_tcp_rcon") or cfg.get("check_lgsm_details")):
        fatal("config: at least one health check must be enabled", fp=fp)

    # Clean shutdown behavior under systemd (SIGTERM) and Ctrl-C (SIGINT)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    # Pre-flight checklist (also opens logfile if enabled)
    fp = preflight_or_die(cfg, server_dir, rustserver_path)

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

    down_streak = 0
    paused = False

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

