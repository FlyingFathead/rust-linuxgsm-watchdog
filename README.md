# rust-linuxgsm-watchdog

A small, boring, stdlib-only watchdog for **[Rust (the game)](https://rust.facepunch.com/), i.e. for dedicated servers managed by LinuxGSM** to keep your server up, running and up to date in a more automated way than what [LinuxGSM](https://linuxgsm.com/) offers by default.

The watchdog polls server health, and if the server is *confirmed down*, it runs a recovery sequence:

1) `./rustserver update`  
2) `./rustserver mu` (Oxide update via LinuxGSM mods)  
3) `./rustserver restart`

This is specifically meant to complement i.e. uMod's [Smooth Restarter](https://umod.org/plugins/smooth-restarter) type workflows that *stop the server* but don’t actually handle updating + restarting.

---

## Why this exists

- Some restart schedulers only know how to bring the server down.
- LinuxGSM already knows how to update server + Oxide + restart -- but it won’t do it automatically when some other plugin drops the server.
- LinuxGSM uses **tmux** when starting the server. If you try to automate recovery from inside `screen` (or nested multiplexers), you’ll hit tmuxception and everything gets stupid.

So the watchdog is designed to run **outside** `screen`/`tmux` (ideally via `systemd`).

---

## What "health" means here

Health is decided by simple signals (no fragile log parsing):

- **Process identity check (strong):**
  - `pgrep -af RustDedicated` must show `+server.identity <identity>`
- **TCP connect check (medium):**
  - TCP connect to the configured RCON port (default `127.0.0.1:28016`)

If any RUNNING signal passes, the watchdog reports `RUNNING`.

If RUNNING signals fail repeatedly for `down_confirmations` checks, it becomes “confirmed down” and recovery starts.

Optional (disabled by default): `./rustserver details` parsing is available for debugging, but it can hang or be slow.

---

## Files

- `rust_watchdog.py` -- the watchdog
- `rust_watchdog.json` -- config (merged over defaults)

---

## Config

Example `rust_watchdog.json`:

```json
{
  "server_dir": "/home/rustserver",
  "identity": "rustserver",
  "pause_file": "/home/rustserver/.watchdog_pause",
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

* `pause_file`: if this file exists, the watchdog should pause (if you implemented/kept that feature).
* `dry_run`: logs what it *would* do, but never runs recovery steps.
* `down_confirmations`: prevents one bad poll from causing a recovery.
* `timeouts`: per-step hard limits so you don’t hang forever.

---

## Usage

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

Put this at:
```
/etc/systemd/system/rust-watchdog.service
```

_(And change as needed; i.e. your user, etc)_

```ini
[Unit]
Description=Rust LinuxGSM watchdog (update + oxide + restart)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rustserver
Group=rustserver
WorkingDirectory=/home/rustserver
ExecStart=/usr/bin/python3 /home/rustserver/rust_watchdog.py --config /home/rustserver/rust_watchdog.json
Restart=always
RestartSec=5
KillMode=process

# Hardening (optional, but usually safe)
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rust-watchdog.service
sudo systemctl status --no-pager -l rust-watchdog.service
journalctl -u rust-watchdog.service -f
```

### After editing the script or JSON

Yes -- restart the service:

```bash
sudo systemctl restart rust-watchdog.service
```

---

## Troubleshooting

### "tmuxception error"

You’re running recovery from inside `screen` or another nested multiplexer. Run the watchdog via `systemd` (or from a plain shell) instead.

### Lock file complaints

The watchdog uses a lock to prevent multiple instances.

If you see:

* `Lock exists at /tmp/rustserver_watchdog.lock`

Check if a watchdog is actually running:

```bash
pgrep -af rust_watchdog.py
```

If nothing is running and the lock is stale:

```bash
sudo rm -f /tmp/rustserver_watchdog.lock
sudo systemctl restart rust-watchdog.service
```

### Timeouts / hanging updates

Bump `timeouts.update` / `timeouts.mu` in JSON if SteamCMD is slow, or keep them strict if you prefer fail-fast and retry later.

---

### About

As usual, code by [FlyingFathead](https://github.com/FlyingFathead/) with ChaosWhisperer meddling with the steering wheel.

This repo's official URL: [https://github.com/FlyingFathead/rust-linuxgsm-watchdog](https://github.com/FlyingFathead/rust-linuxgsm-watchdog)

**If you like this repo, remember to give it a star. ;-) Thanks.**