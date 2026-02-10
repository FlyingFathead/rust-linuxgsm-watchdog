#!/usr/bin/env python3
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

DEFAULTS = {
    "server_dir": "/home/rustserver",
    "identity": "rustserver",

    "interval_seconds": 30,
    "cooldown_seconds": 120,

    "lockfile": "/tmp/rustserver_watchdog.lock",
    "logfile": "/home/rustserver/log/rust_watchdog.log",

    "pause_file": "",  # e.g. "/home/rustserver/.watchdog_pause" (empty = disabled)

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
        if ok: running_votes += 1

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
    ap.add_argument("--config", default="./rust_watchdog.json")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    server_dir = os.path.abspath(cfg["server_dir"])
    rustserver_path = os.path.join(server_dir, "rustserver")

    if not (os.path.isfile(rustserver_path) and os.access(rustserver_path, os.X_OK)):
        print(f"FATAL: not executable: {rustserver_path}", file=sys.stderr)
        sys.exit(2)

    fp = None
    if cfg.get("logfile"):
        os.makedirs(os.path.dirname(os.path.abspath(cfg["logfile"])), exist_ok=True)
        fp = open(cfg["logfile"], "a", encoding="utf-8")

    # Guard: don’t allow recovery from inside screen/tmux
    if inside_screen_or_tmux() and not cfg.get("dry_run", False):
        log("WARNING: running inside screen/tmux -> forcing dry_run=true (prevents tmuxception loops)", fp)
        cfg["dry_run"] = True

    if not acquire_lock(cfg["lockfile"], fp):
        sys.exit(1)

    log(f"Watchdog started (dry_run={cfg['dry_run']})", fp)
    log(f"server_dir={server_dir} identity={cfg['identity']}", fp)
    log(f"recovery_steps={cfg['recovery_steps']}", fp)

    down_streak = 0
    paused = False

    try:
        while True:
            pause_file = cfg.get("pause_file")

            if pause_file and os.path.exists(pause_file):
                if not paused:
                    log(f"PAUSED: {pause_file} exists -- skipping checks/recovery", fp)
                    paused = True
                if args.once:
                    break
                time.sleep(int(cfg["interval_seconds"]))
                continue
            else:
                if paused:
                    log(f"UNPAUSED: {pause_file} removed -- resuming", fp)
                    paused = False

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
                    step = step.strip().lower()
                    timeout = cfg["timeouts"].get(step, None)
                    try:
                        run_cmd([rustserver_path, step], server_dir, fp, timeout=timeout, dry_run=cfg["dry_run"])
                    except TimeoutError as e:
                        log(f"STEP TIMEOUT ({step}): {e}", fp)
                    except Exception as e:
                        log(f"STEP ERROR ({step}): {e}", fp)

                log(f"Cooldown {cfg['cooldown_seconds']}s after recovery attempt", fp)
                time.sleep(int(cfg["cooldown_seconds"]))
                down_streak = 0
            else:
                if args.once:
                    break
                time.sleep(int(cfg["interval_seconds"]))
                if args.once:
                    break
    finally:
        release_lock(cfg["lockfile"])
        if fp:
            fp.close()

if __name__ == "__main__":
    main()

