# rust-linuxgsm-watchdog

A watchdog for **[Rust (the game)](https://rust.facepunch.com/), i.e. for dedicated servers managed by LinuxGSM** to keep your server up, running and up to date in a more automated way than what [LinuxGSM](https://linuxgsm.com/) offers by default.

This program is stdlib-only unless WebRCON features are used. Authenticated
wipe-timestamp discovery is enabled by default, and WebRCON tests plus the
SmoothRestarter bridge are optional; these features use `websocket-client`. It
polls server health and, if the server is *confirmed down*, runs a recovery
sequence, i.e.:

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

Optional (needed for authenticated wipe-timestamp discovery and other WebRCON
features such as `--test-rcon-say` and the SmoothRestarter bridge):
- `websocket-client` (install via `requirements.txt`, or `pip install websocket-client`) 

---

## Files

- `rust_watchdog.py` -- the watchdog
- `rust_watchdog_alerts.py` -- external alert backends and formatting
- `rust_watchdog.json` -- config (merged over defaults)
- `rust-watchdog.service` -- example systemd unit
- `tests/test_forced_wipe.py` -- forced-wipe schedule/state/lifecycle regression tests
- `tests/test_alert_footnotes.py` -- Telegram HTML/Markdown footnote regression tests
- `tests/test_config_edit.py` -- persistent config-editing and path-migration regression tests
- `tests/test_view_config.py` -- effective-config rendering and safe-exit regression tests
- `tests/test_plugin_tools.py` -- Oxide plugin-tool path-default regression tests

The release version is declared once as `__version__` near the top of
`rust_watchdog.py`. Every alert reads that runtime value and renders it in the
application label:

```text
🟢 rust-linuxgsm-watchdog (v0.4.6) -- started
```

If the value is missing or empty, the alert renderer uses `(N/A)` instead.

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

  "wipe_timestamp_rcon_enabled": true,
  "wipe_timestamp_filesystem_fallback_enabled": true,
  "wipe_timestamp_interval_seconds": 600,

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
* `wipe_timestamp_rcon_enabled`: read the current save creation time from
  authenticated WebRCON `serverinfo` and persist it as the last map-wipe time.
* `wipe_timestamp_filesystem_fallback_enabled`: if RCON cannot supply a valid
  timestamp, use the newest `.map` mtime under the derived LinuxGSM
  `serverfiles/server/<identity>/` directory. Active `.sav` mtimes are never
  used because ordinary autosaves keep changing them.
* `wipe_timestamp_interval_seconds`: how often to reconcile the primary or
  fallback value while the Rust server is healthy.

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

### View the effective configuration

Show the complete configuration the watchdog would use after merging built-in
defaults, resolving configured paths, merging alert defaults, and applying the
recovery-step toggles:

```bash
./rust_watchdog.py --config ./rust_watchdog.json --view-config
```

`--viewconfig` is accepted as a convenience alias. The command exits before
opening logs, acquiring the watchdog lock, loading alert state, starting alert
workers, sending notifications, checking systemd, probing RCON, or touching
runtime state.

Interactive terminals receive section colors and UTF-8 symbols when supported.
Piped or redirected output is plain text, and setting `NO_COLOR` disables ANSI
colors. Environment-variable values, including alert credentials, are not read
or printed.

### Persistent config editing

The shipped example keeps `rustserver` as the default Linux account. To migrate
an existing JSON config to another home account without hand-editing every
absolute path:

```bash
./rust_watchdog.py --config ./rust_watchdog.json \
  --change-home-user rustnewone

# Short alias:
./rust_watchdog.py --config ./rust_watchdog.json \
  --changeuser rustnewone
```

The command recursively inspects the raw JSON config and rewrites string values
whose complete absolute path starts with `/home/<user>`. It reports the number
of matches and prints each JSON key, old value, and new value. For the shipped
config, the normal matches are:

```text
$.server_dir
$.lockfile
$.logfile
$.pause_file
$.forced_wipe_state_file
```

It does not rewrite `identity`, relative paths, embedded message text, or
arbitrary strings that merely contain `/home/`. It also deliberately leaves
`rust-watchdog.service` alone and prints a warning to review that file's
`User=`, `Group=`, `WorkingDirectory=`, and `ExecStart=` values separately.

Config changes are written atomically. Before an actual change, the original is
copied next to it with a UTC timestamp, for example:

```text
rust_watchdog.json.bak.20260727T145512.123456Z
```

The preferred forced-wipe config interface persists the existing single action
value:

```bash
./rust_watchdog.py --config ./rust_watchdog.json \
  --set-forced-wipe-action off

./rust_watchdog.py --config ./rust_watchdog.json \
  --set-forced-wipe-action map-wipe

./rust_watchdog.py --config ./rust_watchdog.json \
  --set-forced-wipe-action full-wipe
```

Boolean-style convenience switches accept `on/off`, `true/false`, `yes/no`, or
`1/0`:

```bash
./rust_watchdog.py --config ./rust_watchdog.json \
  --full-wipe-wipeday on

./rust_watchdog.py --config ./rust_watchdog.json \
  --map-wipe-wipeday on
```

They still save only `forced_wipe_action`; separate contradictory booleans are
not added to the config. If both modes are enabled, `full-wipe` wins:

```text
WARN: both map-wipe and full-wipe were enabled for the forced-wipe day; full-wipe takes precedence. Effective forced_wipe_action="full-wipe".
```

To switch explicitly from full wipe to map wipe using the convenience form:

```bash
./rust_watchdog.py --config ./rust_watchdog.json \
  --full-wipe-wipeday off \
  --map-wipe-wipeday on
```

Home-path migration and forced-wipe changes can be combined in one invocation.
They are saved as one transaction, after which the command exits without
starting the watchdog:

```bash
./rust_watchdog.py --config ./rust_watchdog.json \
  --change-home-user rustnewone \
  --set-forced-wipe-action full-wipe
```

### Oxide/uMod plugin tools

Both plugin utilities now use the standard LinuxGSM Oxide plugin directory when
no directory argument is supplied:

```text
$HOME/serverfiles/oxide/plugins
```

Run them from the repository without repeating that path:

```bash
python3 tools/oxide_plugins_inventory.py
python3 tools/umod_plugins_check.py
```

Each script declares its own `DEFAULT_PLUGINS_DIR` near the imports. A positional
directory still overrides the default for nonstandard installations:

```bash
python3 tools/oxide_plugins_inventory.py /srv/rust/oxide/plugins
python3 tools/umod_plugins_check.py /srv/rust/oxide/plugins
```

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

## Optional automatic monthly forced wipe

Automatic deletion is **off by default**, including after an upgrade. Enable it
explicitly with either `map-wipe` (retain blueprints) or `full-wipe` (remove
blueprints):

```json
{
  "forced_wipe_action": "full-wipe",
  "forced_wipe_trigger": "new-build-after-schedule",

  "forced_wipe_early_release_tolerance_minutes": 15,
  "forced_wipe_action_window_minutes": 360,
  "forced_wipe_fallback_at_window_end": false,

  "forced_wipe_backup_before": true,
  "forced_wipe_backup_required": true,
  "forced_wipe_verify_update_current": true,
  "forced_wipe_state_file": "/home/rustserver/rust-linuxgsm-watchdog/data/state/forced_wipe.json",

  "forced_wipe_reminder_enabled": true,
  "forced_wipe_reminder_repeat_minutes": 30
}
```

The schedule is the first Thursday at 19:00 `Europe/London`. This is deliberately
not described as 19:00 GMT: during British Summer Time it is 18:00 UTC.

The calendar alone never triggers deletion. The watchdog parses LinuxGSM's
`Local build` and `Remote build` values and maintains a pre-release remote-build
fence. A different remote build first observed within the configured tolerance
before release, or after release, becomes the candidate. An earlier same-day
update becomes the fence instead of the wipe candidate.

The Rust player client and dedicated server are separate Steam apps with
separate build IDs. Near-simultaneous changes are useful evidence that a
coordinated Rust release landed, especially around the scheduled monthly
window, but they are not a forced-wipe flag: an ordinary coordinated hotfix can
also change both. Build correlation therefore must not independently authorize
deletion.

If the watchdog starts inside the release window without a previously persisted
fence, it refuses to arm an automatic wipe. That can miss an unattended wipe,
but it cannot reinterpret an old pending update as permission to delete data.

An optional calendar backstop can guarantee the configured wipe action even
when the monthly build is not identified:

```json
{
  "forced_wipe_fallback_at_window_end": true
}
```

The fallback arms at the end of `forced_wipe_action_window_minutes`. With the
default schedule and 360-minute window, that is 01:00 `Europe/London` after the
first-Thursday 19:00 release time. It uses `forced_wipe_action`, so it performs
either `map-wipe` or `full-wipe`; it cannot select a different wipe kind.

This does not claim that a particular build was the Facepunch monthly release.
It is explicitly a calendar fallback: if no wipe is recorded for that cycle by
the cutoff, it runs the normal backup/update/verify/mod-update/wipe/start
lifecycle anyway. The watchdog must have observed the cycle before the cutoff.
Consequently, enabling the option after an old monthly window has already
passed cannot cause an immediate retroactive wipe. A manual wipe recorded on
the scheduled Facepunch wipe day also completes the cycle and suppresses the
fallback.

Once armed, both restart paths use the same lifecycle:

```text
stop (a no-op if SmoothRestarter already stopped it)
backup
update
verify no update remains pending
mu
full-wipe or map-wipe
start
verify normal watchdog health
```

The state file makes this sequence crash-safe:

- `pending` is written before SmoothRestarter is asked to shut down.
- A pre-wipe marker is written before the destructive LinuxGSM command.
- `wipe_done` is written immediately after that command succeeds and before
  startup.
- If startup fails after `wipe_done`, recovery retries only `start`.
- If execution dies while the wipe command is in an ambiguous state, the
  watchdog refuses another automatic wipe and reports that manual inspection is
  required.
- `completed` is written only after the normal health checks report `RUNNING`.
- Later hotfixes in the same monthly cycle use the ordinary update/restart path
  and cannot cause another wipe.

When `forced_wipe_action` is `off`, the persistent reminder is on by default.
After the monthly schedule passes, the watchdog logs and sends a UTF-8 `⚠️`
warning every `forced_wipe_reminder_repeat_minutes` until that cycle has a
recorded wipe. The wording deliberately says **no completed wipe is recorded**:
Steam build numbers alone cannot prove that a wipe occurred. By default, the
watchdog also queries authenticated WebRCON `serverinfo.SaveCreatedTime`, which
does identify when the current map/save was created. If that timestamp belongs
to the active Facepunch cycle, the reminder and any pending duplicate automatic
wipe are suppressed.

The automatic path records the exact successful wipe-command time. After a
manual `full-wipe` or `map-wipe`, record either the current time or the actual
UTC time:

```bash
./rust_watchdog.py --config ./rust_watchdog.json \
  --mark-forced-wipe-done --forced-wipe-kind full-wipe

./rust_watchdog.py --config ./rust_watchdog.json \
  --mark-forced-wipe-done 2026-08-06T18:23:00Z \
  --forced-wipe-kind full-wipe
```

The state retains `last_wipe_at`, `last_wipe_source`, `last_wipe_kind`,
`last_restart_at`, and `last_restart_source` across monthly cycle rollover.
Both ledgers are stored in the configured `forced_wipe_state_file`. The restart
time is taken from the live `RustDedicated` process start time via Linux
`/proc`, then persisted so a later server-down alert can still show it.
The wipe time is primarily read from the nested JSON returned by authenticated
WebRCON `serverinfo.SaveCreatedTime`, at startup and then every
`wipe_timestamp_interval_seconds` while Rust is healthy. This requires the
optional `websocket-client` dependency and working RCON credentials. A
discovered historical/external wipe is recorded with
`last_wipe_source: rcon-save-created` and `last_wipe_kind: unknown`, because
SaveCreatedTime cannot distinguish `map-wipe` from `full-wipe`. Explicit
manual/automatic wipe-kind metadata is retained when it is already known.

If RCON is unavailable or its response has no valid `SaveCreatedTime`, the
default-on filesystem fallback reads the newest `.map` file mtime beneath the
LinuxGSM server identity directory derived from `server_dir` and `identity`.
LinuxGSM deletes and recreates `*.map` and `*.sav*` for both map and full wipes,
but the watchdog deliberately ignores `.sav` mtimes because ordinary saves keep
changing them. LinuxGSM full wipes additionally delete `*.db` except
`player.tokens.db`; database timestamps are not used because those files may be
created or modified well after the wipe as players and plugins become active.
A fallback observation is recorded with
`last_wipe_source: filesystem-map-mtime` and `last_wipe_kind: unknown`. RCON
always wins when both sources are available.

Status renders the UTC timestamps and long-form ages:

```text
last_wipe_at: 2026-08-06T18:23:00Z
last_wipe_age: 20 days, 3 hours, 12 minutes ago
last_wipe_source: manual
last_wipe_kind: full-wipe
last_restart_at: 2026-08-06T18:24:15Z
last_restart_age: 20 days, 3 hours, 10 minutes ago
last_restart_source: rust-process-start
```

Every normal watchdog alert receives a separate status footnote by default:

```text
Server last wiped: 2026-08-06 18:23:00 UTC (RCON)
(20 days, 3 hours, 12 minutes ago)

Server last restarted: 2026-08-06 18:24:15 UTC
(20 days, 3 hours, 10 minutes ago)
```

The wipe line identifies the source actually used: `(RCON)` for
`serverinfo.SaveCreatedTime`, `(map file mtime)` for the filesystem fallback,
`(manual record)` for `--mark-forced-wipe-done`, or
`(watchdog automatic wipe)` when the watchdog performed the wipe. Legacy
records without a stored source omit the source label.

The Telegram HTML renderer wraps each line in `<i>...</i>`, Telegram Markdown
uses `_..._`, and the existing Discord renderer uses Markdown italics. The
elapsed footnote is excluded from alert deduplication, so a changing minute
count does not defeat the existing dedupe policy.

If RCON and the `.map` fallback are both unavailable and no prior timestamp has
been persisted, the watchdog says so instead of guessing from a save-file
mtime or Steam build time:

```text
Server last wiped: unknown (no wipe timestamp recorded)

Server last restarted: unknown (no Rust process start timestamp recorded)
```

The footnote can be configured under `alerts`:

```json
{
  "alerts": {
    "status_footnote": {
      "enabled": true,
      "include_last_wipe": true,
      "include_last_restart": true,
      "unknown_wipe_text": "unknown (no wipe timestamp recorded)",
      "unknown_restart_text": "unknown (no Rust process start timestamp recorded)"
    }
  }
}
```

Inspect the current schedule and persisted fence without starting the watchdog:

```bash
./rust_watchdog.py --config ./rust_watchdog.json --forced-wipe-status
```

The automatic feature requires `enable_update_watch=true` and
`enable_server_update=true`. `dry_run=true` never writes forced-wipe state.

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

If a monthly forced-wipe candidate is armed, that recovery cycle instead runs
the persisted `backup -> update -> mu -> wipe -> start` sequence.

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
- v0.4.6
  **Fixed / Added:**
  - Added `--view-config` with the `--viewconfig` alias for a complete, human-readable effective configuration.
  - The view merges watchdog and alert defaults, resolves paths, applies recovery toggles, and exits before runtime side effects.
  - Added automatic ANSI colors and UTF-8 symbols for capable interactive terminals, with plain redirected output and `NO_COLOR` support.
  - Split wipe/restart alert ages onto their own lines and added a blank line between the two status blocks.
  - Changed both Oxide/uMod plugin tools to default to `$HOME/serverfiles/oxide/plugins`.
  - Kept an explicit `DEFAULT_PLUGINS_DIR` in each tool and retained positional path overrides.
  - Added config-view, alert-rendering, and plugin-tool regression coverage.
- v0.4.5
  **Fixed / Added:**
  - Added default-on reconciliation of the last map-wipe timestamp from authenticated WebRCON `serverinfo.SaveCreatedTime`.
  - Decodes Rust's outer WebRCON response and the JSON document nested in its `Message` field.
  - Queries the timestamp before the first alert and periodically while the server is healthy.
  - Added a default-on LinuxGSM `.map` mtime fallback when RCON is unavailable or invalid; RCON remains authoritative.
  - Alert footnotes identify the timestamp source as `(RCON)` or `(map file mtime)`; manual and watchdog-recorded wipes are labeled separately.
  - Deliberately ignores active `.sav` mtimes because autosaves continuously modify them.
  - Persists discovered timestamps with `last_wipe_source: rcon-save-created` and `last_wipe_kind: unknown`.
  - Retains known `map-wipe` or `full-wipe` metadata when RCON observes the same wipe shortly after an explicit manual or automatic record.
  - Suppresses forced-wipe reminders and pending duplicate automatic wipes when the current Facepunch cycle is already wiped.
  - Added validation and regression coverage for nested responses, malformed or future timestamps, persistence, metadata preservation, and cycle completion.
- v0.4.4
  **Fixed / Added:**
  - Declared `0.4.4` once in `rust_watchdog.py` and added `(v0.4.4)` to every normal alert header and the direct Telegram status-test header.
  - Added an `(N/A)` alert-header fallback when no version value is available.
  - Updated the MIT license copyright attribution and project home-page line.
  - Added the opt-in `forced_wipe_fallback_at_window_end` calendar backstop.
  - The fallback runs the configured `map-wipe` or `full-wipe` action when the Facepunch monthly action window ends without a recorded wipe.
  - Added a pre-cutoff observation guard so enabling the option after an old window cannot trigger a retroactive wipe.
  - A manual wipe recorded on the scheduled Facepunch wipe day now completes that cycle even when performed earlier than the build-candidate tolerance.
  - The fallback reuses the persisted one-wipe-per-cycle, pre-destructive marker, ambiguous-result, and start-only retry protections.
- v0.4.3
  **Fixed / Added:**
  - Added `--change-home-user USER` with the `--changeuser` alias to migrate all `/home/<user>`-prefixed JSON path values while leaving `identity` and the systemd unit untouched.
  - Added per-match reporting, Linux-account-name validation, atomic config replacement, and timestamped backups.
  - Added `--set-forced-wipe-action {off,map-wipe,full-wipe}` for persistent command-line configuration.
  - Added `--full-wipe-wipeday` and `--map-wipe-wipeday` boolean-style convenience switches, with explicit full-wipe precedence and warning output.
  - Added transactional combined edits and config migration/precedence regression tests.
- v0.4.2
  **Fixed / Added:**
  - Added a default-on italic status footnote to watchdog alerts with last wipe and last Rust server restart timestamps.
  - Added long-form elapsed time such as `5 days, 23 hours, 51 minutes ago`.
  - Added explicit unknown-timestamp fallbacks instead of inferring wipes from unreliable filesystem or Steam metadata.
  - Added persistent `last_restart_at` tracking from the actual `RustDedicated` process start time.
  - Added Telegram HTML/Markdown and existing Discord Markdown rendering, plus fallback, rollover, and dedupe regression tests.
- v0.4.1
  **Fixed / Added:**
  - Added a default-on persistent `⚠️` reminder when automatic forced wiping is off and the monthly schedule has passed without a recorded wipe.
  - Added configurable reminder repetition with restart-safe rate-limit state.
  - Added a persistent last-wipe ledger with UTC timestamp, elapsed age, source, and wipe kind.
  - Added `--mark-forced-wipe-done [UTC_TIMESTAMP]` for acknowledging externally performed wipes.
  - Documented why simultaneous player-client and dedicated-server build changes are corroborating release evidence, not a forced-wipe flag.
- v0.4.0
  **Fixed / Added:**
  - Added opt-in `map-wipe` / `full-wipe` handling for the monthly Rust release.
  - Added persisted Steam build fencing so earlier same-day updates cannot be mistaken for the monthly release build.
  - Added one-wipe-per-cycle state with `wipe_done` persisted before startup; startup failures now retry only `start`.
  - Added an ambiguous in-progress guard so a watchdog crash during the destructive command cannot cause a blind second wipe.
  - Unified SmoothRestarter and no-SmoothRestarter lifecycle ordering: backup, update, update verification, mod update, wipe, start, health verification.
  - Fixed the post-release schedule bug that made the configured `WIPE WINDOW` unreachable.
  - Added `--forced-wipe-status`, forced-wipe alerts, LinuxGSM build-ID parsing, DST/date tests, and lifecycle idempotency tests.
- v0.3.8
  **Fixed / Added:**
  - Fixed startup alert ordering so `watchdog_started` is emitted only after the watchdog successfully acquires its lock.
  - Prevented non-winning / aborted instances from sending misleading startup notifications.
  - Startup/restart notifications now come from the actual live watchdog instance instead of the pre-lock path.
  - Included extra startup alert context (`pid`, `started_at`, `dry_run`) in the emitted startup event.
- v0.3.7
  **Fixed / Added:**
  - Restored zero-cooldown defaults for restart/update-related alert events in the shipped config.
  - Prevented `restart_requested` and related update flow alerts from being unintentionally suppressed by the global default alert cooldown.
  - Kept the global alert cooldown available for noisier events while allowing immediate restart/update notifications by default.
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
  - The watchdog is now calculating a countdown to Facepunch's forced wipe day (by default, the first Thursday of every month at 19:00 Europe/London); pending restarts over updates are on hold by default that day until we're past the expected update time.
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
