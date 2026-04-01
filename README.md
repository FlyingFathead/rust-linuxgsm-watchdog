# rust-linuxgsm-watchdog

A watchdog for **[Rust (the game)](https://rust.facepunch.com/), i.e. for dedicated servers managed by LinuxGSM** to keep your server up, running and up to date in a more automated way than what [LinuxGSM](https://linuxgsm.com/) offers by default.

This program is stdlib-only by default. If you enable WebRCON features (tests / SmoothRestarter bridge), it uses `websocket-client`. It polls server health and, if the server is *confirmed down*, runs a recovery sequence, i.e.:

1) `./rustserver update`  
2) `./rustserver mu` (Oxide update via LinuxGSM mods)  
3) `./rustserver restart`

This is meant to complement workflows like uMod’s **[Smooth Restarter](https://umod.org/plugins/smooth-restarter)** that can *stop the server gracefully* but don’t handle **Steam-end server update + mod updates + restart** on their own.

The Rust Watchdog currently supports server status/restart/update alerts via the [Telegram Bot API](https://core.telegram.org/bots).

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

## Config / Usage

Note: you do NOT need to copy the full example config.
The watchdog loads built-in defaults from the Python code and then merges the file passed with `--config` on top.

So a minimal custom config is valid, as long as it includes the keys you actually want to override.

Important: alert-related defaults are partly defined in `rust_watchdog_alerts.py`, so if you enable alerts with a minimal config, you should at least set:

```json
{
  "alerts": {
    "enabled": true,
    "backends": ["telegram"]
  }
}
```

Here's another example `rust_watchdog.json`:

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

## Telegram alerts setup

The watchdog can send alert messages via **Telegram Bot API** (outbound HTTPS).

### 1) Create a Telegram bot (get a token)

1. In Telegram, open **@BotFather**
2. Run:

   * `/newbot`
   * pick a name + username
3. BotFather will give you a token that looks like:

   * `123456789:AA...`

Keep that token secret.

### 2) Get your `chat_id` (private chat or group)

#### Option A: Private chat (simplest)

1. Open your new bot in Telegram and press **Start** (or send any message).
2. On the server, run:

```bash
export RUST_WD_TELEGRAM_TOKEN="123456789:AA..."
curl -s "https://api.telegram.org/bot${RUST_WD_TELEGRAM_TOKEN}/getUpdates" | jq
```

Look for something like:

* `.result[].message.chat.id`

You can also extract the latest chat id quickly:

```bash
curl -s "https://api.telegram.org/bot${RUST_WD_TELEGRAM_TOKEN}/getUpdates" \
  | jq '.result[-1].message.chat.id'
```

#### Option B: Group chat

1. Add the bot to your group.
2. In the group, send a command so the bot definitely "sees" it (privacy mode will not block commands):

   * `/start`
3. Then run the same `getUpdates` command above and read the group `chat.id` (usually a **negative** number).

### 3) Quick "does Telegram even work from this server" test

```bash
export RUST_WD_TELEGRAM_TOKEN="123456789:AA..."
export RUST_WD_TELEGRAM_CHAT_IDS="123456789"   # or "-1001234567890" for a group

curl -sS -X POST "https://api.telegram.org/bot${RUST_WD_TELEGRAM_TOKEN}/sendMessage" \
  -d "chat_id=123456789" \
  --data-urlencode "text=rust-linuxgsm-watchdog: test message" \
  | jq
```

For a group test, replace `123456789` in `-d "chat_id=123456789"` with your negative group chat id.

### 4) Store secrets safely (recommended)

Do not hardcode the token in a public config. Use an env file readable only by root:

```bash
sudo install -m 600 /dev/null /etc/default/rust-watchdog
sudo nano /etc/default/rust-watchdog
```

Put:

```bash
RUST_WD_TELEGRAM_TOKEN="123456789:AA..."
RUST_WD_TELEGRAM_CHAT_IDS="-1001234567890"
```

If you want multiple Telegram destinations, separate them with commas or spaces, for example:

```bash
RUST_WD_TELEGRAM_CHAT_IDS="-1001234567890,123456789"
```

Then in your `rust-watchdog.service`, add:

```ini
EnvironmentFile=/etc/default/rust-watchdog
```

Reload + restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart rust-watchdog.service
```

### 5) Configure the watchdog

The current config uses **env var names**, not raw secrets in JSON.

Example:

```json
{
  "alerts": {
    "enabled": true,
    "backends": ["telegram"],
    "telegram": {
      "token_env": "RUST_WD_TELEGRAM_TOKEN",
      "chat_ids_env": "RUST_WD_TELEGRAM_CHAT_IDS",
      "parse_mode": "HTML",
      "disable_web_preview": true,
      "timeout_s": 8,
      "preflight_getme": true
    }
  }
}
```

Notes:

* `token_env` = the name of the environment variable holding the bot token
* `chat_ids_env` = the name of the environment variable holding one or more Telegram chat IDs
* chat IDs may be separated by commas and/or whitespace
* this is **not** `$VARS` expansion inside config values -- the watchdog reads the env var names from config and then resolves them with `os.getenv()`

### 6) Verify alerts end-to-end

Run a one-shot cycle (or whatever minimal run you prefer) and watch logs:

```bash
./rust_watchdog.py --config ./rust_watchdog.json --once
# or:
journalctl -u rust-watchdog.service -f
```

If Telegram is misconfigured, you should see a clear error (bad token/chat ids, blocked outbound HTTPS, etc.).

---

### History
- v0.3.6
  **Fixed / Added:**
  - Improved `--test-telegram-status` with better systemd `EnvironmentFile` diagnostics and clearer manual-test failure reporting.
  - Added safe Rust server PID / start-time / uptime reporting to Telegram status output without exposing full cmdlines.
  - Added duplicate-aware RustDedicated process selection for status reporting.
  - Fixed Telegram status helper config placement by using the watchdog’s top-level config keys for systemd fallback options.
- v0.3.5
  **Fixed / Added:**
  - Added robust config JSON parse diagnostics for invalid / empty / whitespace-only config files.
  - Startup errors now show line, column, nearby context, and first-byte hex preview to expose pasted shell junk / corrupted config content quickly.
  - Config loader now reads with UTF-8 BOM tolerance (`utf-8-sig`).
- v0.3.4
  **Fixed / Added:**
  - Improved Telegram alert/event semantics and cleaned up alert naming (`server_down` instead of stale `confirmed_down`; normalized `WARNING` level naming).
  - Added richer alert coverage for watchdog lifecycle and update-watch flow:
    - `startup_ok`
    - `update_available`
    - `update_held`
    - `restart_requested`
  - Confirmed-down alerts now include the primary detected failure cause when available (for example process missing / identity mismatch / RCON endpoint problems), so restart/recovery reasons are visible in Telegram instead of just "server went down".
  - Update-triggered restart requests now include a reason/path in alerts (for example SmoothRestarter vs watchdog fallback), making restart behavior less opaque.
  - Added deep-merge config loading for nested config sections instead of the old mostly-shallow merge behavior.
  - Cleaned up alert config structure and docs:
    - human-readable event titles instead of emoji-only titles
    - normalized `emoji_by_level` keys
    - Telegram env var names/documentation aligned to `RUST_WD_TELEGRAM_TOKEN` and `RUST_WD_TELEGRAM_CHAT_IDS`
- v0.3.3
  **Fixed / Added:**
  - Prevent multiple watchdog instances from running at once (fixes “double processes” / duplicate recovery behavior).
  - Added alerts support with Telegram backend (dedupe + cooldown; configurable titles/bodies/emoji).
  - Discord and other API alert backends: sketched out in the code / WIP.
- v0.3.0
  **Fixed:**
  - SmoothRestarter runtime-loaded checks no longer misread unrelated WebRCON frames (serverinfo/chat/keepalive).
  - Reduced flakiness in RCON-based chat announcements and SR "ceremony" tests.
  - WebRCON receive logic now ignores non-matching frames until deadline; failures are surfaced as a timeout error instead of returning random frames.
- v0.2.9 - More detailed Smooth Restarter Oxide/Carbon checkup
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