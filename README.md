# rust-linuxgsm-watchdog

A watchdog for **[Rust (the game)](https://rust.facepunch.com/), i.e. for dedicated servers managed by LinuxGSM** to keep your server up, running and up to date in a more automated way than what [LinuxGSM](https://linuxgsm.com/) offers by default.

This program is stdlib-only by default. If you enable WebRCON features (tests / SmoothRestarter bridge), it uses `websocket-client`. It polls server health and, if the server is *confirmed down*, runs a recovery sequence, i.e.:

1) `./rustserver update`  
2) `./rustserver mu` (Oxide update via LinuxGSM mods)  
3) `./rustserver restart`

This is meant to complement workflows like uMod’s **[Smooth Restarter](https://umod.org/plugins/smooth-restarter)** that can *stop the server gracefully* but don’t handle **Steam-end server update + mod updates + restart** on their own.

---

## Why this exists

- Rust receives constant updates from [Facepunch](https://rust.facepunch.com/) -- so keeping the server current with minimal downtime matters.
- LinuxGSM already knows how to do the boring-but-correct sequence: **update server + update mods + restart**.
- But LinuxGSM does not automatically run that sequence when the server goes down due to external reasons (crashes, plugin actions, etc).
- Many “restart schedulers” can only stop the server. Coordinating **stop/update/mu/restart** reliably on LinuxGSM is a separate problem.
- LinuxGSM runs Rust inside **tmux**. If you try to run recovery from inside `screen`/`tmux`, you’ll get tmuxception and everything gets stupid.

So the watchdog is designed to run **outside** `screen`/`tmux` (ideally via `systemd`).

---

## What "health" means here

Health is decided by simple signals (no log parsing, no fragile regex soup):

- **Process identity check (strong):**
  - `pgrep -af RustDedicated` must show `+server.identity <identity>`
- **TCP connect check (medium):**
  - TCP connect to the configured RCON port (default `127.0.0.1:28016`) to verify the port is reachable (not full WebRCON auth)

If any RUNNING signal passes, the watchdog reports `RUNNING`.

If RUNNING signals fail repeatedly for `down_confirmations` checks, it becomes “confirmed down” and recovery starts.

Optional (disabled by default): `./rustserver details` parsing exists for debugging, but it can hang or be slow.

---

## Requirements / assumptions

- Python 3.9+ (uses `zoneinfo`; install `tzdata` on minimal hosts if your timezone DB is missing)
- A working LinuxGSM Rust install where `server_dir` contains an executable `./rustserver`

Optional (only needed for WebRCON features like `--test-rcon-say` and the SmoothRestarter bridge):
- `websocket-client` (install via `requirements.txt`, or `pip install websocket-client`) 

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

# stdlib-only mode (no WebRCON features) -- nothing to install

# OPTIONAL: enable WebRCON features (tests + SmoothRestarter bridge)
python3 -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/python -m pip install -r requirements.txt
```

**(Option B to install the websocket if the venv isn't working out for you):**

On Ubuntu/Debian tree Linux systems:

```bash
sudo apt update
sudo apt install -y python3-websocket || sudo apt install -y python3-websocket-client
```

On Fedora/RHEL:

```bash
sudo dnf install -y python3-websocket-client
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

### WebRCON test helpers

Send a chat broadcast via WebRCON:

```bash
./rust_watchdog.py --config ./rust_watchdog.json --test-rcon-say "hello from watchdog"
```

Send an arbitrary WebRCON command:

```bash
./rust_watchdog.py --config ./rust_watchdog.json --test-rcon-cmd "status"
```

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

If you see a lock complaint, it will mention your configured lockfile path, e.g.:

* `Lock exists at /home/rustserver/rust-linuxgsm-watchdog/data/lock/rust_watchdog.lock`

Check if it’s actually running:

```bash
pgrep -af rust_watchdog.py
```

If nothing is running and the lock is stale:

```bash
rm -f /home/rustserver/rust-linuxgsm-watchdog/data/lock/rust_watchdog.lock
sudo systemctl restart rust-watchdog.service
```

### Timeouts / hanging updates

Bump `timeouts.update` / `timeouts.mu` if SteamCMD is slow, or keep them strict if you prefer fail-fast + retry later.

---

## Optional: SmoothRestarter bridge (graceful restarts)

If you use uMod’s **[Smooth Restarter](https://umod.org/plugins/smooth-restarter)** for player-visible countdown/UI, the watchdog can act as a bridge **while the server is RUNNING**:

1. Watchdog periodically runs `./rustserver check-update` (or `./rustserver cu`) via LinuxGSM.
2. If an update is detected, watchdog **always broadcasts**:
   - `update_watch_announce_message` (default: "Update detected -- restart incoming.")
3. Then it chooses one of two paths:

### Path A -- SmoothRestarter countdown (preferred)

If SmoothRestarter bridging is enabled and usable, watchdog sends (via **Rust WebRCON**) the configured command:
- `smoothrestarter_console_cmd` (default: `srestart restart {delay}`)

Even when using SmoothRestarter’s own countdown/UI, watchdog also sends **one** informational line using:
- `update_watch_countdown_template` (example: "Time until server update and restart: {seconds} seconds.")

And it also sends the final fallback message once:
- `update_watch_final_message` (default: "Server is restarting, come back in a few minutes!")

SmoothRestarter then performs the graceful shutdown. Once the server is down, LinuxGSM restart/update happens on the next normal watchdog recovery cycle.

### Path B -- No SmoothRestarter (or bridge failed)

If SmoothRestarter is disabled OR the bridge fails at runtime, watchdog does a crude countdown itself:
- broadcasts `update_watch_countdown_template` every `update_watch_no_sr_tick_seconds`
- for `update_watch_no_sr_countdown_seconds` total
- then broadcasts `update_watch_final_message`
- then runs the immediate sequence:
  `./rustserver stop` -> `./rustserver update` -> `./rustserver mu` -> `./rustserver restart`

### What “SR check” means in this project

There are three different ideas people confuse:

- **Bridge enabled:** `enable_smoothrestarter_bridge=true` (note: bridge only triggers if `enable_update_watch=true`)
- **SmoothRestarter installed:** plugin file exists:
  `{server_dir}/serverfiles/oxide/plugins/SmoothRestarter.cs`
- **Bridge usable right now:** `websocket-client` is available and WebRCON autodetect works (find `+rcon.ip/+rcon.port/+rcon.password` from the RustDedicated cmdline for this identity), and the RCON send succeeds.

If “usable” fails, watchdog logs why and falls back to Path B.

Enable in `rust_watchdog.json`:

```json
{
  "enable_update_watch": true,
  "update_check_interval_seconds": 600,
  "update_check_timeout": 60,

  "enable_smoothrestarter_bridge": true,
  "smoothrestarter_restart_delay_seconds": 300,
  "smoothrestarter_console_cmd": "srestart restart {delay}",

  "update_watch_announce_message": "Update detected -- restart incoming.",
  "update_watch_countdown_template": "Time until server update and restart: {seconds} seconds.",
  "update_watch_final_message": "Server is restarting, come back in a few minutes!",

  "update_watch_no_sr_countdown_seconds": 30,
  "update_watch_no_sr_tick_seconds": 10,

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

Note: the bridge sends commands via Rust WebRCON (requires `websocket-client`).
Run the watchdog outside tmux/screen (systemd recommended) so recovery isn’t blocked by nested multiplexers.

---

### History
- v0.2.8 - Rudimentary checks on [Smooth Restarter](https://umod.org/plugins/smooth-restarter) integrity; more bug fixes
- v0.2.7 - Small bugfixes
- v0.2.6 - Implemented a standalone restart timer notification to the server when Smooth Restarter is not available and when we're watching for updates
  - The watchdog is now calculating a countdown to Facepunch's forced wipe day (by default, the first Thursday of every month at 19:00 GMT); pending restarts over updates are on hold by default that day until we're past the expected update time.
  - WIP: set wipe levels during forced wipe update-restarts.
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