#!/usr/bin/env python3
"""
umod_plugins_check.py

Check local Oxide/uMod Rust plugins (*.cs) against uMod plugin JSON endpoints,
with optional fallback to ChaosCode manifest for plugins that do not match uMod.

Features:
- cache to disk (TTL)
- proper 429 handling (Retry-After / X-Retry-After)
- min interval throttling
- progress output
- optional ANSI colors
- optional ChaosCode fallback (default: on)

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

CHAOS_MANIFEST_JSON = "https://chaoscode.io/api/resource_manifest.json"

CACHE_DIR_DEFAULT = HERE.parent / "data" / "cache"
CACHE_FILE_DEFAULT = CACHE_DIR_DEFAULT / "umod_plugin_json_cache.json"
CACHE_TTL_SECONDS_DEFAULT = 12 * 3600

CHAOS_CACHE_TTL_SECONDS_DEFAULT = 45 * 60  # manifest updates ~31m; keep a little headroom

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
# ChaosCode manifest loader (cached)
# ------------------------------------------------------------
def load_chaos_manifest(
    cache: Dict[str, Any],
    cache_path: Path,
    *,
    ttl_s: int,
    timeout_s: int,
    debug_headers: bool,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns mapping: stem_lower -> resource dict
    Resource fields (typical):
      ResourceTitle, ResourceVersion, ResourceFile, ResourceURL, AuthorName
    """
    key = "chaos:manifest"
    now = int(time.time())

    ent = cache.get(key)
    if isinstance(ent, dict):
        ts = int(ent.get("ts", 0) or 0)
        if ts and (now - ts) <= int(ttl_s) and "data" in ent and isinstance(ent["data"], list):
            manifest_list = ent["data"]
            return _index_chaos_manifest(manifest_list)

    # Fetch fresh
    res = http_get_json(
        CHAOS_MANIFEST_JSON,
        timeout_s=int(timeout_s),
        min_interval_s=0.0,  # one call
        max_retries=3,
        debug_headers=bool(debug_headers),
    )
    manifest_list = res.data
    if not isinstance(manifest_list, list):
        raise RuntimeError("Chaos manifest: unexpected JSON shape (expected list)")

    cache[key] = {"ts": now, "data": manifest_list}
    save_cache(cache_path, cache)

    return _index_chaos_manifest(manifest_list)

def _index_chaos_manifest(manifest_list: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for it in manifest_list:
        if not isinstance(it, dict):
            continue
        rf = str(it.get("ResourceFile") or "").strip()
        if not rf:
            continue
        stem = rf.split(".", 1)[0].strip().lower()
        if not stem:
            continue
        # last-one-wins; fine for our use
        out[stem] = it
    return out

# ------------------------------------------------------------
# ANSI colors (toggle)
# ------------------------------------------------------------
ANSI = {
    "reset": "\x1b[0m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
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
    if s.startswith("ERROR") or s.startswith("UNKNOWN"):
        return f"{ANSI['red']}{s}{ANSI['reset']}"
    return s

# ------------------------------------------------------------
# Output table
# ------------------------------------------------------------
def print_table(rows: List[Dict[str, Any]], *, use_color: bool) -> None:
    cols = ["filename", "source", "local", "remote", "status", "remote_url"]
    widths = {c: len(c) for c in cols}

    def s(v: Any) -> str:
        return "-" if v is None else str(v)

    # compute widths without ANSI
    for r in rows:
        for c in cols:
            vv = s(r.get(c))
            if c == "remote_url" and len(vv) > 110:
                vv = vv[:107] + "..."
            widths[c] = min(max(widths[c], len(vv)), 120)

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))

    for r in rows:
        parts = []
        for c in cols:
            vv = s(r.get(c))
            if c == "status":
                vv = color_status(vv, use=use_color)
            if c == "remote_url":
                raw = s(r.get(c))
                if len(raw) > 110:
                    raw = raw[:107] + "..."
                vv = raw  # keep urls plain
            parts.append(vv)

        # pad all but status (ANSI would break padding)
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
    ap.add_argument("--cache-ttl", type=int, default=CACHE_TTL_SECONDS_DEFAULT, help="uMod cache TTL seconds")

    # Chaos toggle (default: on)
    try:
        boolopt = argparse.BooleanOptionalAction  # py3.9+
        ap.add_argument("--check-chaos", default=True, action=boolopt, help="also check ChaosCode (fallback for uMod-unknown)")
    except Exception:
        # fallback if somehow running on older python
        ap.add_argument("--check-chaos", action="store_true", default=True)
        ap.add_argument("--no-check-chaos", action="store_true", default=False)

    ap.add_argument("--chaos-cache-ttl", type=int, default=CHAOS_CACHE_TTL_SECONDS_DEFAULT, help="Chaos manifest cache TTL seconds")

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

    check_chaos = bool(getattr(args, "check_chaos", True))
    # older-python fallback parsing if both flags exist
    if hasattr(args, "no_check_chaos") and getattr(args, "no_check_chaos"):
        check_chaos = False

    chaos_index: Dict[str, Dict[str, Any]] = {}
    chaos_load_err: Optional[str] = None
    if check_chaos:
        try:
            chaos_index = load_chaos_manifest(
                cache,
                cache_path,
                ttl_s=int(args.chaos_cache_ttl),
                timeout_s=int(args.timeout),
                debug_headers=bool(args.debug_headers),
            )
        except Exception as e:
            chaos_load_err = str(e)
            chaos_index = {}

    hdr = f"Found {len(locals_)} plugins in {plugins_dir} -- checking uMod"
    if check_chaos:
        hdr += " (+ ChaosCode fallback)"
        if chaos_load_err:
            hdr += f" [Chaos manifest ERROR: {chaos_load_err}]"
    print(hdr + "...", file=sys.stderr)

    out_rows: List[Dict[str, Any]] = []
    any_outdated = False
    any_unknown = False

    for i, p in enumerate(locals_, start=1):
        filename = str(p.get("filename") or "")
        stem = stem_noext(filename)
        stem_l = stem.lower()
        local_ver = str(p.get("version") or "") or ""

        # ---------------- uMod lookup ----------------
        source = "umod"
        status = ""
        remote_ver = "-"
        remote_url = "-"

        key = f"umod:json:{stem}"
        now = int(time.time())
        cached = False
        data = None

        ent = cache.get(key)
        if isinstance(ent, dict):
            ts = int(ent.get("ts", 0) or 0)
            if ts and (now - ts) <= int(args.cache_ttl) and "data" in ent:
                data = ent["data"]
                cached = True

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

        # Fallback search.json only if asked
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
                    data = m
                else:
                    status = "UNKNOWN (no match)"
                    any_unknown = True
            except Exception as e:
                status = f"ERROR: {e}"
                any_unknown = True

        if data is not None and not status:
            remote_ver = str(data.get("latest_release_version") or "-")
            remote_url = str(data.get("url") or "-")

            if local_ver != "-" and remote_ver != "-" and local_ver and remote_ver:
                if local_ver == remote_ver:
                    status = "OK"
                else:
                    newer = version_is_newer(remote_ver, local_ver)
                    status = "OUTDATED" if (newer is True or newer is None) else "OK"
            else:
                status = "UNKNOWN (missing version)"
                any_unknown = True

        # If uMod failed/unknown and chaos enabled -> Chaos fallback
        if (not status or status.startswith("UNKNOWN")) and check_chaos and chaos_index and (not status.startswith("ERROR")):
            rr = chaos_index.get(stem_l)
            if isinstance(rr, dict):
                source = "chaos"
                remote_ver = str(rr.get("ResourceVersion") or "-")
                remote_url = str(rr.get("ResourceURL") or "-")

                if local_ver and remote_ver and local_ver != "-" and remote_ver != "-":
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
            source = "unknown"

        if status == "OUTDATED":
            any_outdated = True

        row = {
            "filename": filename,
            "source": source,
            "local": local_ver or "-",
            "remote": remote_ver or "-",
            "status": status,
            "remote_url": remote_url or "-",
        }

        if args.outdated_only and status != "OUTDATED":
            if show_progress:
                tag = "cached" if cached else "net"
                st = color_status(status, use=use_color)
                print(f"[{i}/{len(locals_)}] {filename} [{source}] -- {st} ({tag})", file=sys.stderr)
            continue

        out_rows.append(row)

        if show_progress:
            tag = "cached" if cached else "net"
            st = color_status(status, use=use_color)
            print(f"[{i}/{len(locals_)}] {filename} [{source}] -- {st} ({tag})", file=sys.stderr)

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