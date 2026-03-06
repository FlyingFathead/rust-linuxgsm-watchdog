#!/usr/bin/env python3
"""
oxide_plugins_inventory.py

Scan an Oxide/uMod plugins directory for *.cs files and extract:
- filename
- Info(name, author, version)
- Description(...)
- file mtime (local time)
- file size

Usage:
  python3 oxide_plugins_inventory.py /path/to/oxide/plugins
  python3 oxide_plugins_inventory.py /path/to/oxide/plugins --tsv
  python3 oxide_plugins_inventory.py /path/to/oxide/plugins --json
  python3 oxide_plugins_inventory.py /path/to/oxide/plugins --recursive
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Match C# normal string literal: " ... " with backslash escapes
CS_STR = r'"(?P<s>(?:\\.|[^"\\])*)"'
# Match C# verbatim string literal: @" ... " where quotes are doubled: ""
CS_VSTR = r'@\"(?P<vs>(?:\"\"|[^\"])*?)\"'

# Info("Name","Author","Version") -- Python re forbids duplicate named groups,
# so we give the 3 fields unique names.
INFO_RE = re.compile(
    r'\[\s*Info\s*\(\s*"(?P<name>(?:\\.|[^"\\])*)"\s*,\s*"(?P<author>(?:\\.|[^"\\])*)"\s*,\s*"(?P<version>(?:\\.|[^"\\])*)"\s*\)\s*\]',
    re.MULTILINE,
)

# Description can be normal or verbatim; may appear in same attribute list as Info
DESC_RE = re.compile(
    rf'\[\s*Description\s*\(\s*(?:{CS_VSTR}|{CS_STR})\s*\)\s*\]',
    re.MULTILINE | re.DOTALL,
)

# Also allow [Info(...), Description(...)] on same brackets; pull Description separately:
DESC_ANYWHERE_RE = re.compile(
    rf'Description\s*\(\s*(?:{CS_VSTR}|{CS_STR})\s*\)',
    re.MULTILINE | re.DOTALL,
)


def _local_dt(ts: float) -> str:
    # local timezone ISO-ish
    return _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _unescape_csharp_normal(s: str) -> str:
    """
    Unescape common C# backslash escapes from a normal string literal.
    This is close enough for plugin metadata.
    """
    try:
        # Python's unicode_escape is a decent approximation for \n, \t, \uXXXX, \xNN, \\
        return bytes(s, "utf-8").decode("unicode_escape")
    except Exception:
        return s


def _unescape_csharp_verbatim(vs: str) -> str:
    # In verbatim strings, "" represents a literal quote.
    return vs.replace('""', '"')


def _extract_description(text: str) -> Optional[str]:
    # Prefer bracketed [Description(...)] if present, else any Description(...) occurrence.
    m = DESC_RE.search(text)
    if not m:
        m = DESC_ANYWHERE_RE.search(text)
        if not m:
            return None

        # When using DESC_ANYWHERE_RE, group names differ; rebuild by checking which matched.
        # We'll re-run a tighter match on that substring:
        sub = m.group(0)
        m2 = re.search(rf'(?:{CS_VSTR}|{CS_STR})', sub, flags=re.DOTALL)
        if not m2:
            return None
        if m2.groupdict().get("vs") is not None:
            return _unescape_csharp_verbatim(m2.group("vs"))
        return _unescape_csharp_normal(m2.group("s"))

    # DESC_RE provides named groups vs/s but only one will be present
    gd = m.groupdict()
    if gd.get("vs") is not None:
        return _unescape_csharp_verbatim(gd["vs"])
    if gd.get("s") is not None:
        return _unescape_csharp_normal(gd["s"])
    return None


def _extract_info(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    m = INFO_RE.search(text)
    if not m:
        return None, None, None

    gd = m.groupdict()

    name = _unescape_csharp_normal(gd.get("name") or "").strip()
    author = _unescape_csharp_normal(gd.get("author") or "").strip()
    version = _unescape_csharp_normal(gd.get("version") or "").strip()

    return (name or None, author or None, version or None)


def scan_plugins(dir_path: Path, recursive: bool = False) -> List[Dict[str, Any]]:
    if not dir_path.exists() or not dir_path.is_dir():
        raise FileNotFoundError(f"Not a directory: {dir_path}")

    files = dir_path.rglob("*.cs") if recursive else dir_path.glob("*.cs")
    out: List[Dict[str, Any]] = []

    for p in sorted(files):
        try:
            st = p.stat()
        except OSError:
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""

        name, author, version = _extract_info(text)
        desc = _extract_description(text)

        out.append(
            {
                "file": str(p),
                "filename": p.name,
                "name": name,
                "author": author,
                "version": version,
                "description": desc,
                "mtime": _local_dt(st.st_mtime),
                "size_bytes": st.st_size,
            }
        )

    return out


def _print_table(rows: List[Dict[str, Any]]) -> None:
    # Simple fixed-width columns, truncate description
    cols = ["filename", "name", "author", "version", "mtime", "size_bytes", "description"]
    widths = {c: len(c) for c in cols}

    def fmt(v: Any) -> str:
        if v is None:
            return "-"
        s = str(v)
        return s

    # precompute widths
    for r in rows:
        for c in cols:
            s = fmt(r.get(c))
            if c == "description" and len(s) > 80:
                s = s[:77] + "..."
            widths[c] = min(max(widths[c], len(s)), 90 if c == "description" else 60)

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))

    for r in rows:
        parts = []
        for c in cols:
            s = fmt(r.get(c))
            if c == "description" and len(s) > 80:
                s = s[:77] + "..."
            parts.append(s.ljust(widths[c]))
        print("  ".join(parts))


def _print_tsv(rows: List[Dict[str, Any]]) -> None:
    cols = ["file", "filename", "name", "author", "version", "mtime", "size_bytes", "description"]
    print("\t".join(cols))
    for r in rows:
        def t(v: Any) -> str:
            if v is None:
                return ""
            s = str(v)
            return s.replace("\t", " ").replace("\n", "\\n")
        print("\t".join(t(r.get(c)) for c in cols))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", nargs="?", default=".", help="plugins directory (default: .)")
    ap.add_argument("--recursive", action="store_true", help="scan recursively")
    ap.add_argument("--json", dest="as_json", action="store_true", help="output JSON")
    ap.add_argument("--tsv", action="store_true", help="output TSV")
    args = ap.parse_args()

    rows = scan_plugins(Path(args.dir).expanduser(), recursive=args.recursive)

    if not rows:
        p = str(Path(args.dir).expanduser())
        print(f"No plugins found in directory: {p}")
        return 0

    if args.as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if args.tsv:
        _print_tsv(rows)
        return 0

    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())