#!/usr/bin/env python3
"""
umod_plugins_check.py

Check local Oxide/uMod Rust plugins (*.cs) against uMod plugin JSON endpoints.

Prefer direct per-plugin JSON:
  https://umod.org/plugins/<TitleCaseName>.json

This avoids hammering /plugins/search.json per plugin (which rate-limits fast and is legacy).

Features:
- cache to disk (TTL)
- proper 429 handling (Retry-After / X-Retry-After)
- min interval throttling
- progress output
- optional ANSI colors

Exit codes:
- 0: all OK
- 1: at least one OUTDATED
- 2: at least one UNKNOWN or ERROR
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ------------------------------------------------------------
# Import your inventory scanner (local source of truth)
# ------------------------------------------------------------
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from oxide_plugins_inventory import scan_plugins  # type: ignore
except Exception as e:
    print(f"FATAL: cannot import scan_plugins from oxide_plugins_inventory.py: {e}", file=sys.stderr)
    raise SystemExit(2)

USER_AGENT = "rust-linuxgsm-watchdog/umod_plugins_check (stdlib urllib)"

UMOD_PLUGIN_JSON = "https://umod.org/plugins/{name}.json"
UMOD_SEARCH_JSON = "https://umod.org/plugins/search.json"

CACHE_DIR_DEFAULT = HERE.parent / "data" / "cache"
CACHE_FILE_DEFAULT = CACHE_DIR_DEFAULT / "umod_plugin_json_cache.json"
CACHE_TTL_SECONDS_DEFAULT = 12 * 3600

# ------------------------------------------------------------
# Version compare (best-effort)
# ------------------------------------------------------------
def parse_version(v: str) -> Tuple[Tuple[int, ...], str]:
    v = (v or "").strip()
    if v.startswith(("v", "V")):
        v = v[1:].strip()
    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)?(.*)$", v)
    if not m:
        return ((), v)
    nums_s = (m.group(1) or "").strip()
    suffix = (m.group(2) or "").strip()
    if nums_s:
        nums = tuple(int(x) for x in nums_s.split(".") if re.match(r"^\d+$", x))
    else:
        nums = ()
    return (nums, suffix)

def version_is_newer(remote: str, local: str) -> Optional[bool]:
    if not remote or not local:
        return None
    r_nums, r_suf = parse_version(remote)
    l_nums, l_suf = parse_version(local)
    if not r_nums or not l_nums:
        return None
    if r_nums != l_nums:
        return r_nums > l_nums
    if r_suf == l_suf:
        return False
    if (not r_suf) and l_suf:
        return True
    return None

# ------------------------------------------------------------
# Cache
# ------------------------------------------------------------
def ensure_cache_path(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

def load_cache(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
    except Exception:
        pass
    return {}

def save_cache(path: Path, obj: Dict[str, Any]) -> None:
    ensure_cache_path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

# ------------------------------------------------------------
# HTTP with 429 handling + rate-limit headers
# ------------------------------------------------------------
@dataclass
class HttpResult:
    data: Any
    headers: Dict[str, str]

def _headers_dict(h) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for k, v in h.items():
            out[str(k)] = str(v)
    except Exception:
        pass
    return out

def _retry_after_seconds(headers: Dict[str, str]) -> Optional[int]:
    for k in ("Retry-After", "X-Retry-After"):
        v = headers.get(k)
        if v:
            try:
                return int(float(v))
            except Exception:
                pass
    return None

def http_get_json(
    url: str,
    *,
    timeout_s: int,
    min_interval_s: float,
    max_retries: int,
    debug_headers: bool,
) -> HttpResult:
    # naive min-interval limiter (per-process)
    now = time.monotonic()
    last = getattr(http_get_json, "_last_call", 0.0)
    dt = now - last
    if dt < min_interval_s:
        time.sleep(min_interval_s - dt)

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

    attempt = 0
    while True:
        attempt += 1
        setattr(http_get_json, "_last_call", time.monotonic())
        try:
            with urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read()
                hdrs = _headers_dict(resp.headers)
            if debug_headers:
                rl = {k: hdrs.get(k, "") for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After", "X-Retry-After")}
                print(f"DEBUG headers: {rl}", file=sys.stderr)
            obj = json.loads(raw.decode("utf-8", errors="replace") or "{}")

            # If server says remaining=0 and provides retry-after, we can be polite before next call
            try:
                rem = int(hdrs.get("X-RateLimit-Remaining", "999999"))
            except Exception:
                rem = 999999
            ra = _retry_after_seconds(hdrs)
            if rem == 0 and ra:
                time.sleep(max(0, ra))

            return HttpResult(data=obj, headers=hdrs)

        except HTTPError as e:
            hdrs = _headers_dict(e.headers)
            if e.code == 429:
                ra = _retry_after_seconds(hdrs)
                if ra is None:
                    ra = 30
                if attempt <= max_retries:
                    # obey Retry-After and retry
                    time.sleep(max(0, ra))
                    continue
                raise RuntimeError(f"HTTP 429 rate-limited (Retry-After={ra}) after {max_retries} retries")
            if e.code == 404:
                raise FileNotFoundError("404 not found")
            raise RuntimeError(f"HTTPError {e.code}: {e.reason}")

        except URLError as e:
            if attempt <= max_retries:
                time.sleep(1.0)
                continue
            raise RuntimeError(f"URLError: {e}")

        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSONDecodeError: {e}")

# ------------------------------------------------------------
# Matching / URLs
# ------------------------------------------------------------
def stem_noext(filename: str) -> str:
    return Path(filename).stem

def umod_direct_json_url(stem: str) -> str:
    # Works for many plugins: Vanish.json, AdminNoLoot.json etc.
    return UMOD_PLUGIN_JSON.format(name=stem)

def umod_search_url(query: str) -> str:
    params = [
        ("query", query),
        ("page", "1"),
        ("sort", "title"),
        ("sortdir", "asc"),
        ("filter", ""),
        ("categories[]", "rust"),
    ]
    return f"{UMOD_SEARCH_JSON}?{urlencode(params)}"

def best_match_from_search(local_filename: str, search_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = search_data.get("data") if isinstance(search_data, dict) else None
    if not isinstance(items, list):
        return None

    fn = local_filename.lower()
    stem = stem_noext(local_filename).lower()

    best = None
    best_score = -1

    for it in items:
        if not isinstance(it, dict):
            continue
        score = 0
        dl = str(it.get("download_url") or "")
        dl_base = Path(dl).name.lower() if dl else ""
        title = str(it.get("title") or "").lower()
        name = str(it.get("name") or "").lower()

        if dl_base == fn:
            score += 100
        if title.replace(" ", "") == stem.replace(" ", ""):
            score += 30
        if name.replace(" ", "") == stem.replace(" ", ""):
            score += 25

        if score > best_score:
            best_score = score
            best = it

    return best if (best and best_score >= 25) else None

# ------------------------------------------------------------
# ANSI colors (toggle)
# ------------------------------------------------------------
ANSI = {
    "reset": "\x1b[0m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
    "cyan": "\x1b[36m",
    "dim": "\x1b[2m",
}

def want_color(mode: str) -> bool:
    mode = (mode or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    return sys.stdout.isatty()

def color_status(s: str, *, use: bool) -> str:
    if not use:
        return s
    if s == "OK":
        return f"{ANSI['green']}{s}{ANSI['reset']}"
    if s == "OUTDATED":
        return f"{ANSI['yellow']}{s}{ANSI['reset']}"
    # UNKNOWN / ERROR
    if s.startswith("ERROR"):
        return f"{ANSI['red']}{s}{ANSI['reset']}"
    if s.startswith("UNKNOWN"):
        return f"{ANSI['red']}{s}{ANSI['reset']}"
    return s

# ------------------------------------------------------------
# Output table
# ------------------------------------------------------------
def print_table(rows: List[Dict[str, Any]], *, use_color: bool) -> None:
    cols = ["filename", "local", "remote", "status", "remote_url", "remote_dl"]
    widths = {c: len(c) for c in cols}

    def s(v: Any) -> str:
        return "-" if v is None else str(v)

    # compute widths without ANSI
    for r in rows:
        for c in cols:
            vv = s(r.get(c))
            if c in ("remote_url", "remote_dl") and len(vv) > 93:
                vv = vv[:90] + "..."
            widths[c] = min(max(widths[c], len(vv)), 110)

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))

    for r in rows:
        parts = []
        for c in cols:
            vv = s(r.get(c))
            if c == "status":
                vv = color_status(vv, use=use_color)
            if c in ("remote_url", "remote_dl"):
                raw = s(r.get(c))
                if len(raw) > 93:
                    raw = raw[:90] + "..."
                vv = raw if not use_color else raw  # keep urls plain
            parts.append(vv.ljust(widths[c]) if c != "status" else vv)
        # status column may contain ANSI; don't ljust it
        # rebuild line with fixed spacing except status:
        line = []
        for i, c in enumerate(cols):
            if c == "status":
                line.append(parts[i])
            else:
                line.append(parts[i].ljust(widths[c]))
        print("  ".join(line))

# ------------------------------------------------------------
# Main logic
# ------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plugins_dir", nargs="?", default=".", help="oxide/plugins directory (default: .)")
    ap.add_argument("--recursive", action="store_true", help="scan recursively for *.cs")
    ap.add_argument("--cache", default=str(CACHE_FILE_DEFAULT), help=f"cache file (default: {CACHE_FILE_DEFAULT})")
    ap.add_argument("--cache-ttl", type=int, default=CACHE_TTL_SECONDS_DEFAULT, help="cache TTL seconds")
    ap.add_argument("--timeout", type=int, default=12, help="HTTP timeout seconds")
    ap.add_argument("--min-interval", type=float, default=0.6, help="minimum seconds between HTTP requests")
    ap.add_argument("--max-retries", type=int, default=6, help="max retries for transient errors / 429")
    ap.add_argument("--outdated-only", action="store_true", help="only show outdated plugins")
    ap.add_argument("--fallback-search", action="store_true", help="if direct .json 404s, try search.json (slower; may rate-limit)")
    ap.add_argument("--progress", action="store_true", help="show progress lines (default: on if TTY)")
    ap.add_argument("--no-progress", action="store_true", help="disable progress lines")
    ap.add_argument("--color", default="auto", choices=["auto", "always", "never"], help="ANSI colors for status")
    ap.add_argument("--debug-headers", action="store_true", help="print rate-limit headers to stderr")
    ap.add_argument("--json", dest="as_json", action="store_true", help="output JSON")
    args = ap.parse_args()

    plugins_dir = Path(args.plugins_dir).expanduser()
    cache_path = Path(args.cache).expanduser()
    cache = load_cache(cache_path)

    locals_ = scan_plugins(plugins_dir, recursive=bool(args.recursive))
    if not locals_:
        print(f"No plugins found in directory: {plugins_dir}")
        return 0

    show_progress = args.progress or (sys.stderr.isatty() and not args.no_progress)
    use_color = want_color(args.color)

    print(f"Found {len(locals_)} plugins in {plugins_dir} -- now checking uMod...", file=sys.stderr)

    out_rows: List[Dict[str, Any]] = []
    any_outdated = False
    any_unknown = False

    for i, p in enumerate(locals_, start=1):
        filename = str(p.get("filename") or "")
        stem = stem_noext(filename)
        local_ver = str(p.get("version") or "") or ""

        # cache key: direct-json by stem
        key = f"json:{stem}"
        now = int(time.time())
        cached = False
        data = None

        ent = cache.get(key)
        if isinstance(ent, dict):
            ts = int(ent.get("ts", 0) or 0)
            if ts and (now - ts) <= int(args.cache_ttl) and "data" in ent:
                data = ent["data"]
                cached = True

        status = ""
        remote_ver = "-"
        remote_url = "-"
        remote_dl = "-"

        if data is None:
            url = umod_direct_json_url(stem)
            try:
                res = http_get_json(
                    url,
                    timeout_s=int(args.timeout),
                    min_interval_s=float(args.min_interval),
                    max_retries=int(args.max_retries),
                    debug_headers=bool(args.debug_headers),
                )
                data = res.data
                cache[key] = {"ts": now, "data": data}
                save_cache(cache_path, cache)
            except FileNotFoundError:
                data = None
            except Exception as e:
                status = f"ERROR: {e}"
                any_unknown = True

        # Fallback to search.json only if asked
        if data is None and args.fallback_search and not status:
            try:
                sres = http_get_json(
                    umod_search_url(stem),
                    timeout_s=int(args.timeout),
                    min_interval_s=float(args.min_interval),
                    max_retries=int(args.max_retries),
                    debug_headers=bool(args.debug_headers),
                )
                m = best_match_from_search(filename, sres.data if isinstance(sres.data, dict) else {})
                if m:
                    remote_ver = str(m.get("latest_release_version") or "-")
                    remote_url = str(m.get("url") or "-")
                    remote_dl = str(m.get("download_url") or "-")
                    data = m  # enough fields for comparison
                else:
                    status = "UNKNOWN (no match)"
                    any_unknown = True
            except Exception as e:
                status = f"ERROR: {e}"
                any_unknown = True

        if data is not None and not status:
            # direct plugin json has these fields (same as Vanish.json)
            remote_ver = str(data.get("latest_release_version") or "-")
            remote_url = str(data.get("url") or "-")
            remote_dl = str(data.get("download_url") or "-")

            if local_ver != "-" and remote_ver != "-" and local_ver and remote_ver:
                if local_ver == remote_ver:
                    status = "OK"
                else:
                    newer = version_is_newer(remote_ver, local_ver)
                    status = "OUTDATED" if (newer is True or newer is None) else "OK"
            else:
                status = "UNKNOWN (missing version)"
                any_unknown = True

        if not status:
            status = "UNKNOWN (no match)"
            any_unknown = True

        if status == "OUTDATED":
            any_outdated = True

        row = {
            "filename": filename,
            "local": local_ver or "-",
            "remote": remote_ver or "-",
            "status": status,
            "remote_url": remote_url or "-",
            "remote_dl": remote_dl or "-",
        }

        if args.outdated_only and status != "OUTDATED":
            if show_progress:
                tag = "cached" if cached else "net"
                st = color_status(status, use=use_color)
                print(f"[{i}/{len(locals_)}] {filename} -- {st} ({tag})", file=sys.stderr)
            continue

        out_rows.append(row)

        if show_progress:
            tag = "cached" if cached else "net"
            st = color_status(status, use=use_color)
            print(f"[{i}/{len(locals_)}] {filename} -- {st} ({tag})", file=sys.stderr)

    if args.as_json:
        print(json.dumps(out_rows, ensure_ascii=False, indent=2))
    else:
        print_table(out_rows, use_color=use_color)

    if any_unknown:
        return 2
    if any_outdated:
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())