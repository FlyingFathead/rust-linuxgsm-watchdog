#!/usr/bin/env python3
# rust_watchdog_alerts.py -- external notifications for rust-linuxgsm-watchdog (stdlib-only)

import json
import os
import queue
import threading
import time
import hashlib
import socket
import html
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

TELEGRAM_LIMIT = 4096

DEFAULT_EMOJI_BY_EVENT = {
    "recovery_attempted": "⚠️",
    "confirmed_down": "☠️",
}

DEFAULT_EMOJI_BY_LEVEL = {
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "🧨",
    "CRITICAL": "☠️",
}

# ---------------------------------------
# HELPERS
# ---------------------------------------

def _parse_int_list(s: str) -> List[int]:
    s = (s or "").strip()
    if not s:
        return []
    # split on commas and whitespace
    parts = []
    for chunk in s.replace(",", " ").split():
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    out: List[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except Exception:
            pass
    return out

def _telegram_getme(token: str, timeout_s: int = 8) -> (bool, str):
    # Returns (ok, detail)
    url = f"https://api.telegram.org/bot{token}/getMe"
    req = Request(url, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        obj = json.loads(raw) if raw else {}
        if isinstance(obj, dict) and obj.get("ok") is True:
            # keep it short; don’t leak anything sensitive
            u = obj.get("result", {}).get("username", "")
            return True, f"ok (bot=@{u})" if u else "ok"
        return False, f"bad response: {raw[:200]}"
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _now() -> float:
    return time.time()


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


def _split_telegram(msg: str) -> List[str]:
    if len(msg) <= TELEGRAM_LIMIT:
        return [msg]
    return [msg[i:i + TELEGRAM_LIMIT] for i in range(0, len(msg), TELEGRAM_LIMIT)]


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown-host"


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
        self.last_error = ""

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
                except (HTTPError, URLError, TimeoutError, OSError) as e:
                    self.last_error = f"{type(e).__name__}: {e}"
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

    Config lives under cfg["alerts"].
    """

    def _emoji_for(self, alert: Alert) -> str:
        try:
            e = self.emoji_by_event.get(alert.event)
            if e:
                return str(e)
            return str(self.emoji_by_level.get(str(alert.level).upper(), ""))
        except Exception:
            return ""

    def _app_label_for(self, alert: Alert) -> str:
        app = str(alert.fields.get("app") or self.app_default).strip()
        ver = str(alert.fields.get("version") or self.version_default).strip()
        return f"{app} {ver}".strip()

    def _title_for(self, alert: Alert) -> str:
        # If caller didn’t provide a meaningful title, use event_titles mapping
        t = (alert.title or "").strip()
        if (not t) or (t == alert.event):
            mapped = self.event_titles.get(alert.event)
            if mapped:
                return str(mapped)
        return t or alert.event

    def _body_for(self, alert: Alert) -> str:
        body = (alert.text or "").strip()
        if not body:
            mapped = self.event_bodies.get(alert.event)
            if mapped:
                return str(mapped)
        return body

    def __init__(
        self,
        cfg: Dict[str, Any],
        state_path: str = "data/state/alerts_state.json",
        max_queue: int = 200,
        log_fn=None,  # callable(level:str, msg:str)
    ):
        self.root_cfg = cfg if isinstance(cfg, dict) else {}
        self.cfg = self.root_cfg.get("alerts", {}) if isinstance(self.root_cfg.get("alerts", {}), dict) else {}
        self.log_fn = log_fn

        self.enabled = bool(self.cfg.get("enabled", False))
        self.state_path = str(self.cfg.get("state_path", state_path))

        self.cooldown_default = int(self.cfg.get("cooldown_seconds_default", 900))
        self.cooldowns = self.cfg.get("cooldowns", {}) or {}
        self.dedupe_seconds = int(self.cfg.get("dedupe_seconds", 300))

        self.include_host = bool(self.cfg.get("include_host", True))
        self.include_identity = bool(self.cfg.get("include_identity", True))

        self.app_default = str(self.cfg.get("app", "rust-linuxgsm-watchdog"))
        self.version_default = str(self.cfg.get("version", "")).strip()

        cfg_e_event = self.cfg.get("emoji_by_event", {}) or {}
        cfg_e_level = self.cfg.get("emoji_by_level", {}) or {}

        self.emoji_by_event = {**DEFAULT_EMOJI_BY_EVENT, **cfg_e_event}
        self.emoji_by_level = {**DEFAULT_EMOJI_BY_LEVEL, **cfg_e_level}

        self.event_titles = self.cfg.get("event_titles", {}) or {}
        self.event_bodies = self.cfg.get("event_bodies", {}) or {}

        self._q: "queue.Queue[Alert]" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="alerts-worker", daemon=True)

        self._state_lock = threading.Lock()
        # schema:
        #   last_event[event] = ts
        #   last_key[event] = {"hash": str, "ts": float}
        #   suppressed[event] = int
        self._state: Dict[str, Any] = {"last_event": {}, "last_key": {}, "suppressed": {}}
        self._load_state()

        self.backends: List[Backend] = []
        if self.enabled:
            self._init_backends()
            if self.enabled:
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
                    obj = json.load(f)
                if isinstance(obj, dict):
                    self._state = obj
        except Exception:
            self._state = {"last_event": {}, "last_key": {}, "suppressed": {}}

        # harden shape
        for k in ("last_event", "last_key", "suppressed"):
            if k not in self._state or not isinstance(self._state.get(k), dict):
                self._state[k] = {}

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

            token_env = str(tcfg.get("token_env", "RUST_WD_TELEGRAM_TOKEN")).strip()
            chat_ids_env = str(tcfg.get("chat_ids_env", "RUST_WD_TELEGRAM_CHAT_IDS")).strip()

            token = (os.getenv(token_env, "") or "").strip()
            ids = _parse_int_list(os.getenv(chat_ids_env, ""))

            timeout_s = int(tcfg.get("timeout_s", 8))
            preflight = bool(tcfg.get("preflight_getme", False))

            if preflight and token:
                ok, detail = _telegram_getme(token, timeout_s=timeout_s)
                if ok:
                    self._log("INFO", f"telegram preflight getMe: {detail}")
                else:
                    self._log("WARN", f"telegram preflight getMe failed: {detail}")
                    token = ""  # disable backend if token bad

            if token and ids:
                self.backends.append(
                    TelegramBackend(
                        token=token,
                        chat_ids=ids,
                        parse_mode=str(tcfg.get("parse_mode", "HTML")),
                        disable_web_preview=bool(tcfg.get("disable_web_preview", True)),
                        timeout_s=timeout_s,
                    )
                )

        # Discord
        if "discord" in backends:
            dcfg = self.cfg.get("discord", {}) or {}
            webhook = os.getenv(str(dcfg.get("webhook_env", "RUST_WD_DISCORD_WEBHOOK")), "")
            if webhook:
                self.backends.append(
                    DiscordWebhookBackend(
                        webhook_url=webhook,
                        timeout_s=int(dcfg.get("timeout_s", 8)),
                    )
                )

        if not self.backends:
            self._log("WARN", "enabled, but no usable backends configured -- disabling alerts")
            self.enabled = False

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._stop.set()
        try:
            self._worker.join(timeout=2.0)
        except Exception:
            pass
        self._save_state()

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
        alert = Alert(
            event=str(event or "event"),
            level=str(level or "INFO"),
            title=str(title or event or "event"),
            text=str(text or ""),
            fields=fields or {},
            ts=_now(),
        )
        try:
            self._q.put_nowait(alert)
        except queue.Full:
            # drop spam; keep watchdog alive
            if str(level).upper() in ("ERROR", "CRITICAL"):
                self._log("WARN", f"queue full; dropped alert event={alert.event} level={alert.level}")

    def _cooldown_for(self, event: str) -> int:
        try:
            v = self.cooldowns.get(event)
            if v is None:
                return self.cooldown_default
            return int(v)
        except Exception:
            return self.cooldown_default

    def _render_html(self, alert: Alert) -> str:
        # Telegram HTML parse mode: escape everything we didn't author as markup.
        ts_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.ts))
        parts: List[str] = []

        # lvl = html.escape(alert.level)
        # title = html.escape(alert.title)

        emoji = self._emoji_for(alert)
        label = self._app_label_for(alert)
        title = self._title_for(alert)

        parts.append(f"{html.escape(emoji)} <b>{html.escape(label)}</b> -- <b>{html.escape(title)}</b>")
        parts.append(f"<code>{html.escape(ts_s)}</code>")

        if self.include_host:
            parts.append(f"<code>host={html.escape(_hostname())}</code>")

        if self.include_identity:
            ident = alert.fields.get("identity")
            if ident:
                parts.append(f"<code>identity={html.escape(str(ident))}</code>")

        body = self._body_for(alert)
        if body:
            parts.append(html.escape(body))

        # Add a few structured fields (keep it short)
        extras = []
        for k, v in (alert.fields or {}).items():
            if k in ("identity",):
                continue
            if v is None:
                continue
            s = str(v)
            if len(s) > 200:
                s = s[:200] + "..."
            extras.append(f"{k}={s}")
            if len(extras) >= 8:
                break
        if extras:
            parts.append("<code>" + html.escape(" | ".join(extras)) + "</code>")

        return "\n".join(parts)

    def _render_plain(self, alert: Alert) -> str:
        ts_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(alert.ts))
        emoji = self._emoji_for(alert)
        label = self._app_label_for(alert)
        title = self._title_for(alert)
        parts: List[str] = [f"{emoji} {label} -- {title}".strip(), ts_s]

        if self.include_host:
            parts.append(f"host={_hostname()}")

        if self.include_identity:
            ident = alert.fields.get("identity")
            if ident:
                parts.append(f"identity={ident}")

        body = self._body_for(alert)
        if body:
            parts.append(body)

        extras = []
        for k, v in (alert.fields or {}).items():
            if k in ("identity", "app", "version"):
                continue
            if v is None:
                continue
            s = str(v)
            if len(s) > 200:
                s = s[:200] + "..."
            extras.append(f"{k}={s}")
            if len(extras) >= 8:
                break
        if extras:
            parts.append(" | ".join(extras))

        return "\n".join(parts)

    def _render(self, alert: Alert) -> str:
        # If Telegram backend uses HTML, render HTML; Discord gets plain.
        # We render per-backend at send time, but dedupe should be stable -- use plain for dedupe key.
        return self._render_plain(alert)

    def _should_suppress(self, alert: Alert, rendered_key: str) -> bool:
        event = alert.event
        now = alert.ts
        cooldown_s = self._cooldown_for(event)

        with self._state_lock:
            last_event = self._state.get("last_event", {}).get(event)
            if isinstance(last_event, (int, float)) and cooldown_s > 0 and (now - float(last_event)) < cooldown_s:
                self._state["suppressed"][event] = int(self._state["suppressed"].get(event, 0)) + 1
                self._save_state()
                return True

            last_key_ent = self._state.get("last_key", {}).get(event)
            if isinstance(last_key_ent, dict):
                h = last_key_ent.get("hash")
                ts0 = last_key_ent.get("ts")
                if h == rendered_key and isinstance(ts0, (int, float)) and (now - float(ts0)) < self.dedupe_seconds:
                    self._state["suppressed"][event] = int(self._state["suppressed"].get(event, 0)) + 1
                    self._save_state()
                    return True

        return False

    def _mark_sent(self, alert: Alert, rendered_key: str) -> None:
        with self._state_lock:
            self._state["last_event"][alert.event] = float(alert.ts)
            self._state["last_key"][alert.event] = {"hash": rendered_key, "ts": float(alert.ts)}
            self._save_state()

    def _run(self) -> None:
        self._log("INFO", "worker started")
        while not self._stop.is_set():
            try:
                alert = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                # Dedupe key based on plain rendering (stable across backends)
                key_plain = _sha1(self._render(alert))

                if self._should_suppress(alert, key_plain):
                    continue

                ok_any = False
                for b in self.backends:
                    try:
                        if isinstance(b, TelegramBackend) and b.parse_mode.upper() == "HTML":
                            rendered = self._render_html(alert)
                        else:
                            rendered = self._render_plain(alert)
                        ok = b.send(alert, rendered)
                        ok_any = ok_any or bool(ok)
                    except Exception as e:
                        self._log("WARN", f"backend {getattr(b, 'name', '?')} error: {e}")

                if ok_any:
                    self._mark_sent(alert, key_plain)
                else:
                    # Don't mark sent if nothing worked -- allow retry next time.
                    self._log("WARN", f"no backends succeeded for event={alert.event}")

            except Exception as e:
                self._log("WARN", f"handler error: {e}")
            finally:
                try:
                    self._q.task_done()
                except Exception:
                    pass

        self._log("INFO", "worker stopping")
        self._save_state()
