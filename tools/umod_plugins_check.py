#!/usr/bin/env python3
"""
umod_plugins_check.py

Check local Oxide/uMod Rust plugins (*.cs) against uMod's public JSON endpoints.

Strategy:
- Use your existing oxide_plugins_inventory.py as the local source of truth.
- For each local plugin, query uMod:
    https://umod.org/plugins/search.json?query=<q>&page=1&sort=title&sortdir=asc&filter=&categories[]=rust
  and pick the best match (prefer exact download_url basename == local filename).
- Cache responses on disk to avoid rate limits.

Refs:
- search.json endpoint is recommended by uMod admin: /plugins/search.json?... :contentReference[oaicite:2]{index=2}
- uMod also mentions /latest.json and warns about rate limits :contentReference[oaicite:3]{index=3}
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
# Import your inventory scanner (no duplicated parsing)
# ------------------------------------------------------------
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from oxide_plugins_inventory import scan_plugins  # type: ignore
except Exception as e:
    print(f"FATAL: cannot import scan_plugins from oxide_plugins_inventory.py: {e}", file=sys.stderr)
    raise SystemExit(2)

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
UMOD_SEARCH_BASE = "https://umod.org/plugins/search.json"
USER_AGENT = "rust-linuxgsm-watchdog/umod_plugins_check (stdlib urllib)"

CACHE_DIR_DEFAULT = HERE.parent / "data" / "cache"
CACHE_FILE_DEFAULT = CACHE_DIR_DEFAULT / "umod_search_cache.json"
CACHE_TTL_SECONDS_DEFAULT = 12 * 3600  # 12h

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def norm(s: str) -> str:
    s = (s or "").lower()
    return re.sub(r"[^a-z0-9]+", "", s)

def stem_noext(filename: str) -> str:
    return Path(filename).stem

def parse_version(v: str) -> Tuple[Tuple[int, ...], str]:
    """
    Best-effort version compare. Good enough for typical x.y.z(.n).
    Returns (nums, suffix). Compares nums lexicographically; suffix as tiebreaker.
    """
    v = (v or "").strip()
    if v.startswith(("v", "V")):
        v = v[1:].strip()

    m = re.match(r"^([0-9]+(?:\.[0-9]+)*)?(.*)$", v)
    if not m:
        return ((), v)

    nums_s = (m.group(1) or "").strip()
    suffix = (m.group(2) or "").strip()

    nums: Tuple[int, ...]
    if nums_s:
        nums = tuple(int(x) for x in nums_s.split(".") if x.isdigit() or re.match(r"^\d+$", x))
    else:
        nums = ()

    return (nums, suffix)

def version_is_newer(remote: str, local: str) -> Optional[bool]:
    """
    Returns True if remote > local, False if remote <= local, None if can't compare.
    """
    if not remote or not local:
        return None
    r_nums, r_suf = parse_version(remote)
    l_nums, l_suf = parse_version(local)
    if not r_nums or not l_nums:
        return None
    if r_nums != l_nums:
        return r_nums > l_nums
    # same numeric version: treat suffix presence as "different" but not strictly newer
    if r_suf == l_suf:
        return False
    # If remote has no suffix but local does, assume remote is "cleaner"/newer-ish
    if (not r_suf) and l_suf:
        return True
    return None

def http_get_json(url: str, *, timeout_s: int = 12) -> Any:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except HTTPError as e:
        # Handle rate limiting nicely
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "")
            raise RuntimeError(f"HTTP 429 rate-limited (Retry-After={retry_after})")
        raise RuntimeError(f"HTTPError {e.code}: {e.reason}")
    except URLError as e:
        raise RuntimeError(f"URLError: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"JSONDecodeError: {e}")

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

def umod_search_url(query: str, *, page: int = 1) -> str:
    # categories[]=rust matches what uMod staff recommend in examples (urlencoded in their post) :contentReference[oaicite:4]{index=4}
    params = [
        ("query", query),
        ("page", str(page)),
        ("sort", "title"),
        ("sortdir", "asc"),
        ("filter", ""),
        ("categories[]", "rust"),
    ]
    return f"{UMOD_SEARCH_BASE}?{urlencode(params)}"

def cached_search(query: str, cache: Dict[str, Any], cache_ttl_s: int, cache_path: Path) -> Dict[str, Any]:
    key = f"q:{query}"
    now = int(time.time())
    ent = cache.get(key)
    if isinstance(ent, dict):
        ts = int(ent.get("ts", 0) or 0)
        if ts and (now - ts) <= cache_ttl_s and "data" in ent:
            return ent["data"]

    # Not cached / stale
    url = umod_search_url(query, page=1)
    data = http_get_json(url)
    cache[key] = {"ts": now, "data": data}
    save_cache(cache_path, cache)
    return data

def best_match_for_plugin(
    local: Dict[str, Any],
    search_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    filename = str(local.get("filename") or "")
    file_stem = stem_noext(filename)
    local_name = str(local.get("name") or "")
    local_author = str(local.get("author") or "")

    items = search_data.get("data") if isinstance(search_data, dict) else None
    if not isinstance(items, list):
        return None

    best = None
    best_score = -1

    for it in items:
        if not isinstance(it, dict):
            continue

        score = 0
        dl = str(it.get("download_url") or "")
        dl_base = Path(dl).name if dl else ""
        title = str(it.get("title") or "")
        name = str(it.get("name") or "")
        author = str(it.get("author") or "")

        # Strongest: exact filename match from download_url
        if dl_base and dl_base.lower() == filename.lower():
            score += 100

        # Next: normalized comparisons
        if local_name and norm(title) == norm(local_name):
            score += 30
        if norm(name) == norm(file_stem):
            score += 25
        if norm(title) == norm(file_stem):
            score += 20

        # Author (weaker; authors can be multi/merged)
        if local_author and norm(author) == norm(local_author):
            score += 10

        # Tiny bonus if query found something that looks “Rust plugin-y”
        if "rust" in (str(it.get("category_tags") or "") + "," + str(it.get("tags_all") or "")).lower():
            score += 2

        if score > best_score:
            best_score = score
            best = it

    # Require some minimal confidence unless we got the filename match.
    if best and best_score >= 20:
        return best
    return best if (best and best_score >= 100) else None

# ------------------------------------------------------------
# Output
# ------------------------------------------------------------
def print_table(rows: List[Dict[str, Any]]) -> None:
    cols = ["filename", "local", "remote", "status", "remote_url", "remote_dl"]
    widths = {c: len(c) for c in cols}

    def s(v: Any) -> str:
        return "-" if v is None else str(v)

    for r in rows:
        for c in cols:
            vv = s(r.get(c))
            vv = (vv[:90] + "...") if (c in ("remote_url", "remote_dl") and len(vv) > 93) else vv
            widths[c] = min(max(widths[c], len(vv)), 100)

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        parts = []
        for c in cols:
            vv = s(r.get(c))
            vv = (vv[:90] + "...") if (c in ("remote_url", "remote_dl") and len(vv) > 93) else vv
            parts.append(vv.ljust(widths[c]))
        print("  ".join(parts))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plugins_dir", nargs="?", default=".", help="oxide/plugins directory (default: .)")
    ap.add_argument("--recursive", action="store_true", help="scan recursively for *.cs")
    ap.add_argument("--cache", default=str(CACHE_FILE_DEFAULT), help=f"cache file (default: {CACHE_FILE_DEFAULT})")
    ap.add_argument("--cache-ttl", type=int, default=CACHE_TTL_SECONDS_DEFAULT, help="cache TTL seconds")
    ap.add_argument("--sleep", type=float, default=0.25, help="sleep seconds between queries (rate limit friendliness)")
    ap.add_argument("--outdated-only", action="store_true", help="only show outdated plugins")
    ap.add_argument("--json", dest="as_json", action="store_true", help="output JSON")
    args = ap.parse_args()

    plugins_dir = Path(args.plugins_dir).expanduser()
    cache_path = Path(args.cache).expanduser()
    cache = load_cache(cache_path)

    locals_ = scan_plugins(plugins_dir, recursive=bool(args.recursive))
    if not locals_:
        print(f"No plugins found in directory: {plugins_dir}")
        return 0

    out_rows: List[Dict[str, Any]] = []
    errors = 0

    for p in locals_:
        filename = str(p.get("filename") or "")
        local_ver = str(p.get("version") or "") or ""
        local_name = str(p.get("name") or "") or ""

        # Query choice: filename stem tends to match uMod download URLs best
        q1 = stem_noext(filename)
        q2 = local_name.strip()

        match = None
        remote = None

        try:
            d1 = cached_search(q1, cache, int(args.cache_ttl), cache_path)
            match = best_match_for_plugin(p, d1)
            if not match and q2 and norm(q2) != norm(q1):
                time.sleep(float(args.sleep))
                d2 = cached_search(q2, cache, int(args.cache_ttl), cache_path)
                match = best_match_for_plugin(p, d2)
        except Exception as e:
            errors += 1
            out_rows.append({
                "filename": filename,
                "local": local_ver or "-",
                "remote": "-",
                "status": f"ERROR: {e}",
                "remote_url": "-",
                "remote_dl": "-",
            })
            time.sleep(float(args.sleep))
            continue

        if match:
            remote_ver = str(match.get("latest_release_version") or "") or ""
            remote_url = str(match.get("url") or "") or ""
            remote_dl = str(match.get("download_url") or "") or ""

            if local_ver and remote_ver:
                if local_ver == remote_ver:
                    status = "OK"
                else:
                    newer = version_is_newer(remote_ver, local_ver)
                    status = "OUTDATED" if (newer is True or newer is None) else "OK"
            else:
                status = "UNKNOWN"

            row = {
                "filename": filename,
                "local": local_ver or "-",
                "remote": remote_ver or "-",
                "status": status,
                "remote_url": remote_url or "-",
                "remote_dl": remote_dl or "-",
            }
        else:
            row = {
                "filename": filename,
                "local": local_ver or "-",
                "remote": "-",
                "status": "UNKNOWN (no match)",
                "remote_url": "-",
                "remote_dl": "-",
            }

        if args.outdated_only:
            if row["status"] != "OUTDATED":
                # keep also hard errors if you want; currently hide them in outdated-only mode
                time.sleep(float(args.sleep))
                continue

        out_rows.append(row)
        time.sleep(float(args.sleep))

    if args.as_json:
        print(json.dumps(out_rows, ensure_ascii=False, indent=2))
        return 0 if errors == 0 else 1

    print_table(out_rows)
    return 0 if errors == 0 else 1

if __name__ == "__main__":
    raise SystemExit(main())