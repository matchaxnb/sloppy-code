#!/usr/bin/env python3
"""Forced classifications: an override file for titles the heuristics get wrong.

The heuristics read filenames and folder shape, and they are good but not
perfect. Some sources are genuinely ambiguous — a film filed inside a series
folder, a numbered run that is really an anthology — and no amount of tuning
resolves those without breaking the general case. This module lets such
troublemakers be stated outright.

Format
------
One mapping from classification to the files that belong to it:

    # overrides.yaml
    Cowboy Bebop: The Movie (2001):
      - FilmsCollec/Anime/Cowboy Bebop [BDRip 1080p FRE+JAP]/cowboy.bebop.e02...mkv
      - CleanFilms/COWBOY BEBOP THE MOVIE ...BD/BDMV/STREAM/00000.m2ts

    AnimeSeries:
      - ...

A key is interpreted in this order:
  1. a known **category** (`FeatureFilms`, `AnimeSeries`, `_BONUS_`, …) — the
     file is filed under that category, bypassing `media_ids.category`;
  2. a **glob** containing `*?[` — every matching path is claimed;
  3. otherwise a **title**: the file is removed from whatever group it would
     have joined and grouped with the other files under the same key.

Matching keys are tried most-specific first, so an exact path beats a glob.
Paths are matched by suffix against both the absolute source path and the path
relative to its source root, so entries stay portable across hosts.

The loader is a deliberate subset of YAML — mappings to lists of scalars, with
comments and quoting. It is not a general YAML parser; anything it cannot read
raises, rather than being silently ignored.
"""
from __future__ import annotations
import os
import re

CATEGORY_KEYS = {"FeatureFilms", "Series", "AnimeFilms", "AnimeSeries",
                 "Animation", "ShortFilms", "Experimental", "_JUNK_", "_BONUS_"}


class OverrideError(ValueError):
    """Raised for an override file we cannot read. Never silently ignored."""


def _strip_comment(line: str) -> str:
    """Remove a trailing comment, honouring quotes."""
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text


def parse(text: str) -> dict:
    """Parse the override subset. Returns {key: [value, ...]}.

    Raises OverrideError on anything it does not understand — a misread override
    is worse than no override, because it would silently place files wrongly.
    """
    out: dict[str, list[str]] = {}
    current = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if body.startswith("- "):
            if current is None:
                raise OverrideError(f"line {lineno}: list item before any key")
            out[current].append(_scalar(body[2:]))
            continue
        if body.endswith(":"):
            key = _scalar(body[:-1])
            if not key:
                raise OverrideError(f"line {lineno}: empty key")
            current = key
            out.setdefault(key, [])
            continue
        if indent == 0 and ":" in body:
            # inline form: key: value
            key, _, val = body.partition(":")
            key, val = _scalar(key), _scalar(val)
            if not key:
                raise OverrideError(f"line {lineno}: empty key")
            current = None
            out.setdefault(key, []).append(val)
            continue
        raise OverrideError(f"line {lineno}: cannot parse {raw!r}")
    return out


def _glob_regex(pattern: str):
    """Compile a glob to a suffix-anchored regex.

    Written out rather than using `fnmatch.translate` because source paths are
    full of literal brackets ("[BDRip 1080p]", "[OZC]") and fnmatch would read
    them as character classes; escaping them by string replacement is fragile
    (the `[` escape contains a `]`, which the next replacement then mangles).
    Here everything but `*` and `?` is escaped verbatim.
    """
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts) + "$")     # anchor at the end: a suffix match


class Overrides:
    """Resolved overrides, queried per source path."""

    def __init__(self, mapping: dict | None = None):
        self.by_path: dict[str, str] = {}      # suffix -> key (exact, longest wins)
        self.by_glob: list[tuple[object, str]] = []
        self.mapping: dict[str, list[str]] = mapping or {}
        for key, entries in self.mapping.items():
            for e in entries:
                # Only * and ? mark a glob. A bare "[" is NOT treated as one:
                # source paths routinely contain bracketed release groups
                # ("[BDRip 1080p]", "[OZC]"), and reading those as character
                # classes turned every exact path into a pattern matching nothing.
                if any(c in e for c in "*?"):
                    self.by_glob.append((_glob_regex(e.replace(os.sep, "/")), key))
                else:
                    self.by_path[e.strip(os.sep)] = key
        # longest suffix first, so a specific path beats a shorter one
        self._ordered = sorted(self.by_path, key=len, reverse=True)

    def __bool__(self) -> bool:
        return bool(self.mapping)

    def __len__(self) -> int:
        return sum(len(v) for v in self.mapping.values())

    def lookup(self, path: str, root: str | None = None) -> str | None:
        """The override key for a source path, or None."""
        p = path.replace(os.sep, "/")
        candidates = [p]
        if root:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            candidates.append(rel)
        for suffix in self._ordered:
            for cand in candidates:
                if cand == suffix or cand.endswith("/" + suffix):
                    return self.by_path[suffix]
        for rx, key in self.by_glob:
            for cand in candidates:
                if rx.search(cand):
                    return key
        return None

    def category_for(self, path: str, root: str | None = None) -> str | None:
        """The forced category for a path, if its key names a category."""
        key = self.lookup(path, root)
        if key and key in CATEGORY_KEYS:
            return key
        return None

    def title_for(self, path: str, root: str | None = None) -> str | None:
        """The forced group title for a path, if its key names a title."""
        key = self.lookup(path, root)
        if key and key not in CATEGORY_KEYS:
            return key
        return None


def load(path: str) -> Overrides:
    """Load overrides from a file. A missing file yields empty overrides.

    An *unreadable or malformed* file raises: silently proceeding would place the
    very files the user took the trouble to pin.
    """
    if not path or not os.path.exists(path):
        return Overrides()
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return Overrides(parse(text))


if __name__ == "__main__":
    import sys
    ov = load(sys.argv[1] if len(sys.argv) > 1 else "")
    print(f"{len(ov)} entr(ies) across {len(ov.mapping)} key(s)")
    for k, v in ov.mapping.items():
        print(f"  {k}: {len(v)}")
        for e in v:
            print(f"      {e}")
