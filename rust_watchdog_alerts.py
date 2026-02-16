#!/usr/bin/env python3
# rust_watchdog_alerts.py -- external notifications for rust-linuxgsm-watchdog (stdlib-only)
import json
import os
import queue
import threading
import time
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

TELEGRAM_LIMIT = 4096

def _now() -> float:
    return time.time()

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()

def _split_telegram(msg: str) -> List[str]:
    if len(msg) <= TELEGRAM_LIMIT:
        return [msg]
    return [msg[i:i + TELEGRAM_LIMIT] for i in range(0, len(msg), TELEGRAM_LIMIT)]

@dataclass
class Alert:
    event: str
    level: str
    title: str
    text: str
    fields: Dict[str, Any]
    ts: float

class Backend:
    name = "backend"
    def send(self, alert: Alert, rendered: str) -> bool:
        raise NotImplementedError

class TelegramBackend(Backend):
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_ids: List[int],
        parse_mode: str = "HTML",
        disable_web_preview: bool = True,
        timeout_s: int = 8,
    ):
        self.token = token
        self.chat_ids = chat_ids
        self.parse_mode = parse_mode
        self.disable_web_preview = disable_web_preview
        self.timeout_s = timeout_s

    def send(self, alert: Alert, rendered: str) -> bool:
        ok_all = True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        chunks = _split_telegram(rendered)

        for chat_id in self.chat_ids:
            for chunk in chunks:
                payload = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": self.disable_web_preview,
                    "parse_mode": self.parse_mode,
                }
                data = json.dumps(payload).encode("utf-8")
                req = Request(url, data=data, headers={"Content-Type": "application/json"})
                try:
                    with urlopen(req, timeout=self.timeout_s) as resp:
                        _ = resp.read()
                except (HTTPError, URLError, TimeoutError, OSError):
                    ok_all = False
        return ok_all

class DiscordWebhookBackend(Backend):
    name = "discord"

    def __init__(self, webhook_url: str, timeout_s: int = 8):
        self.webhook_url = webhook_url
        self.timeout_s = timeout_s

    def send(self, alert: Alert, rendered: str) -> bool:
        payload = {"content": rendered}
        data = json.dumps(payload).encode("utf-8")
        req = Request(self.webhook_url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                _ = resp.read()
            return True
        except (HTTPError, URLError, TimeoutError, OSError):
            return False

class AlertManager:
    """
    - Never throws from emit()
    - Queue + worker thread (network never blocks watchdog loop)
    - cooldown per event + dedupe by rendered message hash
    - persists state to disk
    """

    def __init__(
        self,
        cfg: Dict[str, Any],
        state_path: str = "data/state/alerts_state.json",
        max_queue: int = 200,
        log_fn=None,  # optional: function(level:str, msg:str)
    ):
        self.cfg = cfg.get("alerts", {}) if isinstance(cfg, dict) else {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.log_fn = log_fn
        self.state_path = state_path

        self.cooldown_default = int(self.cfg.get("cooldown_seconds_default", 900))
        self.cooldowns = self.cfg.get("cooldowns", {}) or {}
        self.dedupe_seconds = int(self.cfg.get("dedupe_seconds", 300))
        self.include_host = bool(self.cfg.get("include_host", True))
        self.include_identity = bool(self.cfg.get("include_identity", True))

        self._q: "queue.Queue[Alert]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="alerts-worker", daemon=True)

        self._state_lock = threading.Lock()
        self._state = {"last_event": {}, "last_key": {}, "suppressed": {}}
        self._load_state()

        self.backends: List[Backend] = []
        if self.enabled:
            self._init_backends()
            self._worker.start()

    def _log(self, level: str, msg: str) -> None:
        if self.log_fn:
            try:
                self.log_fn(level, msg)
            except Exception:
                pass

    def _load_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self._state = json.load(f)
        except Exception:
            self._state = {"last_event": {}, "last_key": {}, "suppressed": {}}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _init_backends(self) -> None:
        backends = self.cfg.get("backends", []) or []
        backends = [b.strip().lower() for b in backends if isinstance(b, str)]

        # Telegram
        if "telegram" in backends:
            tcfg = self.cfg.get("telegram", {}) or {}
            token = os.getenv(tcfg.get("token_env", "RUST_WD_TELEGRAM_TOKEN"), "")
            chat_ids = tcfg.get("chat_ids") or tcfg.get("chat_id")
            ids: List[int] = []
            if isinstance(chat_ids, list):
                ids = [int(x) for x in chat_ids]
            elif isinstance(chat_ids, (str, int)):
                # allow comma-separated string
                if isinstance(chat_ids, str) and "," in chat_ids:
                    ids = [int(x.strip()) for x in chat_ids.split(",") if x.strip()]
                else:
                    ids = [int(chat_ids)]
            if token and ids:
                self.backends.append(
                    TelegramBackend(
                        token=token,
                        chat_ids=ids,
                        parse_mode=tcfg.get("parse_mode", "HTML"),
                        disable_web_preview=bool(tcfg.get("disable_web_preview", True)),
                        timeout_s=int(tcfg.get("timeout_s", 8)),
                    )
                )

        # Discord
        if "discord" in backends:
            dcfg = self.cfg.get("discord", {}) or {}
            webhook = os.getenv(dcfg.get("webhook_env", "RUST_WD_DISCORD_WEBHOOK"), "")
            if webhook:
                self.backends.append(DiscordWebhookBackend(webhook_url=webhook, timeout_s=int(dcfg.get("timeout_s", 8))))

        if not self.backends:
            self._log("WARN", "ALERTS: enabled, but no usable backends configured -> alerts disabled")
            self.enabled = False

    def stop(self) -> None:
        self._stop.set()

    def emit(
        self,
        event: str,
        level: str,
        title: str,
        text: str,
        **fields: Any,
    ) -> None:
        if not self.enabled:
            return
        alert = Alert(event=event, level=level, title=title, text=text, fields=fields, ts=_now())
        try:
            self._q.put_nowait(alert)
        except queue.Full:
            # drop non-critical spam; keep watchdog alive
            if level in ("ERROR", "CRITICAL"):
                self._log("WARN", f"ALERTS: queue full; dropped alert event={event} level={level}")

    def _cooldown_for(self, event: str) -> int:
        v = self.cooldowns.get(event)
        if v is None:
            return self.cooldown_default
        try:
            return int(v)
        except Exception:
            return self.cooldown_default

    def _render(self, a: Alert) -> str:
        host = os.uname().nodename if self.include_host else ""
        identity = a.fields.get("identity", "") if self.include_identity else ""
        head = f"[RustWatchdog]{'[' + identity + ']' if identity else ''}{'[' + host + ']' if host else ''} {a.level}: {a.title}"
        lines = [head, a.text]

        # compact fields (no secrets)
        safe = {}
        for k, v in (a.fields or {}).items():
            if v is None:
                continue
            if "password" in k.lower() or "token" in k.lower():
                continue
            safe[k] = v
        if safe:
            lines.append("fields: " + " ".join(f"{k}={safe[k]}" for k in sorted(safe.keys())))
        return "\n".join(lines)

    def _should_send(self, a: Alert, rendered: str) -> bool:
        now = _now()
        key = f"{a.event}:{_sha1(rendered)}"

        with self._state_lock:
            last_evt = float(self._state.get("last_event", {}).get(a.event, 0) or 0)
            last_key = float(self._state.get("last_key", {}).get(key, 0) or 0)

            cd = self._cooldown_for(a.event)
            if (now - last_evt) < cd:
                self._state["suppressed"][a.event] = int(self._state.get("suppressed", {}).get(a.event, 0) or 0) + 1
                return False

            if (now - last_key) < self.dedupe_seconds:
                self._state["suppressed"][a.event] = int(self._state.get("suppressed", {}).get(a.event, 0) or 0) + 1
                return False

            # tentatively allow; timestamps updated on successful send
            return True

    def _mark_sent(self, a: Alert, rendered: str) -> None:
        now = _now()
        key = f"{a.event}:{_sha1(rendered)}"
        with self._state_lock:
            self._state.setdefault("last_event", {})[a.event] = now
            self._state.setdefault("last_key", {})[key] = now
            # reset suppression counter on real send
            if a.event in self._state.get("suppressed", {}):
                self._state["suppressed"][a.event] = 0
        self._save_state()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                a = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            rendered = self._render(a)
            if not self._should_send(a, rendered):
                continue

            ok_any = False
            for b in self.backends:
                try:
                    if b.send(a, rendered):
                        ok_any = True
                except Exception:
                    pass

            if ok_any:
                self._mark_sent(a, rendered)
