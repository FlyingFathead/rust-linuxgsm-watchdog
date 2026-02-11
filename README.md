# rust-linuxgsm-watchdog

A small, boring, stdlib-only watchdog for **[Rust (the game)](https://rust.facepunch.com/), i.e. for dedicated servers managed by LinuxGSM** to keep your server up, running and up to date in a more automated way than what [LinuxGSM](https://linuxgsm.com/) offers by default.

It polls server health and, if the server is *confirmed down*, runs a recovery sequence, i.e.:

1) `./rustserver update`  
2) `./rustserver mu` (Oxide update via LinuxGSM mods)  
3) `./rustserver restart`

This is meant to complement workflows like uMod’s **[Smooth Restarter](https://umod.org/plugins/smooth-restarter)** that can *stop the server gracefully* but don’t handle **update + mods + restart** on their own.

---

## Why this exists

- Some restart schedulers only know how to bring the server down.
- LinuxGSM already knows how to update server + mods + restart -- but it won’t automatically do it when some other plugin drops the server.
- LinuxGSM runs Rust inside **tmux**. If you try to automate recovery from inside `screen` (or nested multiplexers), you’ll get tmuxception and everything gets stupid.

So the watchdog is designed to run **outside** `screen`/`tmux` (ideally via `systemd`).

---

## What "health" means here

Health is decided by simple signals (no log parsing, no fragile regex soup):

- **Process identity check (strong):**
  - `pgrep -af RustDedicated` must show `+server.identity <identity>`
- **TCP connect check (medium):**
  - TCP connect to the configured RCON port (default `127.0.0.1:28016`)

If any RUNNING signal passes, the watchdog reports `RUNNING`.

If RUNNING signals fail repeatedly for `down_confirmations` checks, it becomes “confirmed down” and recovery starts.

Optional (disabled by default): `./rustserver details` parsing exists for debugging, but it can hang or be slow.

---

## Requirements / assumptions

- Python 3.10+
- A working LinuxGSM Rust install where `server_dir` contains an executable `./rustserver`
- For the SmoothRestarter bridge: `tmux` must exist in PATH (LinuxGSM uses it anyway)

---

## Files

- `rust_watchdog.py` -- the watchdog
- `rust_watchdog.json` -- config (merged over defaults)
- `rust-watchdog.service` -- example systemd unit

---

## Config

Example `rust_watchdog.json`:

```json
{
  "server_dir": "/home/rustserver",
  "identity": "rustserver",

  "pause_file": "/home/rustserver/rust-linuxgsm-watchdog/.watchdog_pause",
  "dry_run": false,

  "interval_seconds": 10,
  "cooldown_seconds": 120,
  "down_confirmations": 2,

  "check_process_identity": true,

  "check_tcp_rcon": true,
  "rcon_host": "127.0.0.1",
  "rcon_port": 28016,
  "tcp_timeout": 2.0,

  "check_lgsm_details": false,
  "details_timeout": 20,

  "recovery_steps": ["update", "mu", "restart"],
  "timeouts": { "update": 1800, "mu": 900, "restart": 600 }
}
```

Notes:

* `enable_server_update`: if false, skip the `update` step even if it’s listed in `recovery_steps`.
* `enable_mods_update`: if false, skip the `mu` step even if it’s listed in `recovery_steps`.
* `pause_file`: if this file exists, the watchdog pauses (no checks, no recovery).
* `dry_run`: logs what it *would* do, but never runs recovery steps.
* `down_confirmations`: prevents one bad poll from causing a recovery.
* `timeouts`: per-step hard limits so SteamCMD slowness doesn’t hang the watchdog forever.

---

## Usage

First, clone the repo i.e. with:

```bash
cd &&
git clone https://github.com/FlyingFathead/rust-linuxgsm-watchdog &&
cd rust-linuxgsm-watchdog
```

### One-shot (manual test)

Run one loop iteration and exit:

```bash
./rust_watchdog.py --config ./rust_watchdog.json --once
```

### Long-running

```bash
./rust_watchdog.py --config ./rust_watchdog.json
```

Do **not** run it inside `screen`/`tmux` if you want it to actually recover (LinuxGSM will tmuxception).

---

## systemd setup (recommended)

Copy the unit file (**make sure to edit your necessary changes first!**):

```bash
sudo cp ./rust-watchdog.service /etc/systemd/system/rust-watchdog.service
sudo systemctl daemon-reload
sudo systemctl enable --now rust-watchdog.service
```

Check logs:

```bash
sudo systemctl status --no-pager -l rust-watchdog.service
journalctl -u rust-watchdog.service -f
```

### After editing the script or JSON

Restart the service:

```bash
sudo systemctl restart rust-watchdog.service
```

---

## Troubleshooting

### "tmuxception"

You’re running recovery from inside `screen` or another multiplexer. Run the watchdog via `systemd` (or a plain shell) instead.

### Lock file complaints

The watchdog uses a lock to prevent multiple instances.

If you see:

* `Lock exists at /tmp/rustserver_watchdog.lock`

Check if it’s actually running:

```bash
pgrep -af rust_watchdog.py
```

If nothing is running and the lock is stale:

```bash
sudo rm -f /tmp/rustserver_watchdog.lock
sudo systemctl restart rust-watchdog.service
```

### Timeouts / hanging updates

Bump `timeouts.update` / `timeouts.mu` if SteamCMD is slow, or keep them strict if you prefer fail-fast + retry later.

---

## Optional: SmoothRestarter bridge (graceful restarts)

If you use uMod’s **[Smooth Restarter](https://umod.org/plugins/smooth-restarter)** for player-visible countdown/UI, the watchdog can act as a bridge:

* While `RUNNING`, watchdog periodically runs `./rustserver check-update` (LinuxGSM).
* If an update is detected, watchdog sends a console command to SmoothRestarter via tmux:
  `srestart restart <delay>`
* SmoothRestarter performs the graceful shutdown.
* Once the server is `DOWN`, watchdog runs the normal recovery sequence:
  `update` -> `mu` -> `restart`

Enable in `rust_watchdog.json`:

```json
{
  "enable_update_watch": true,
  "update_check_interval_seconds": 600,
  "update_check_timeout": 60,

  "enable_smoothrestarter_bridge": true,
  "smoothrestarter_restart_delay_seconds": 300,
  "smoothrestarter_console_cmd": "srestart restart {delay}",

  "restart_request_cooldown_seconds": 3600
}
```

### SmoothRestarter file locations (defaults + overrides)

By default, under a standard LinuxGSM layout, watchdog expects:

* `{server_dir}/serverfiles/oxide/plugins/SmoothRestarter.cs`
* `{server_dir}/serverfiles/oxide/config/SmoothRestarter.json`

The watchdog treats the **plugin file** as the “installed” signal.
The config file may be missing on first run and that’s OK (it will log a note).

If your layout is custom, override paths in `rust_watchdog.json`:

```json
{
  "smoothrestarter_config_path": "",
  "smoothrestarter_plugin_path": ""
}
```

* Leave them empty to use defaults.
* If you set a relative path, it’s resolved relative to `server_dir`.
* `~` and `$VARS` are expanded.

When `enable_smoothrestarter_bridge=true`, the watchdog logs the expected SmoothRestarter paths on startup and prints the download URL if the plugin isn’t installed:
[https://umod.org/plugins/smooth-restarter](https://umod.org/plugins/smooth-restarter)

Note: the bridge sends the command via tmux (LinuxGSM runs the server in tmux),
so run the watchdog outside tmux/screen (systemd recommended).

---

### History
- v0.2.5 - Switched completely to RCON to interact with bridged Oxide plugins like Smooth Restarter
- v0.2.4 - [Smooth Restarter](https://umod.org/plugins/smooth-restarter) bridge test (`--test-smoothrestarter` and `--test-smoothrestarter-send`)
- v0.2.3 - initial support for bridging with [Smooth Restarter](https://umod.org/plugins/smooth-restarter)
- v0.2.2 - server & plugin updates on restart can now be toggled
- v0.2.1 - pre-flight checks, interruptible sleep, stop-aware recovery, stop escalation in run_cmd
- v0.2.0 - stop flag + SIGTERM/SIGINT handler, TCP FAIL counts as DOWN (no “UNKNOWN forever”)
- v0.1.0 - initial release

---

### About

As usual, code by [FlyingFathead](https://github.com/FlyingFathead/) with ChaosWhisperer meddling with the steering wheel.

This repo's official URL: [https://github.com/FlyingFathead/rust-linuxgsm-watchdog](https://github.com/FlyingFathead/rust-linuxgsm-watchdog)

**If you like this repo, remember to give it a star. ;-) Thanks.**