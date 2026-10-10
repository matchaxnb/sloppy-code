#!/usr/bin/env python3
"""Carry lyrics from the legacy library into the staging library.

The old library (`Music/lyrics.db`) tagged its items with lyrics and the
`lyrics_*` flexible attributes. The new staging library is built from the
source trees and never inherited them. This copies them across.

Matching, in order:

1. **Exact source path.** `lyrics.db` stores paths *relative to the source
   root* the item came from (`CleanFLAC/…`), and the staging library records
   the absolute source path hex-encoded in `mo_source`. Decoding that hex and
   matching the relative tail is exact -- and unlike matching on metadata, it
   is unaffected by autotag having rewritten artist/album spellings.
2. **Metadata key.** Normalized `albumartist` + `album` + `title`, for rows
   whose source file is gone or whose `mo_source` is missing.

Only rows that actually carry lyrics (or a `lyrics_*` attribute) are migrated;
the rest would be a lot of writes for nothing.

Idempotent: re-running overwrites with the same values. `--dry-run` reports
what would change without writing.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

LEGACY = "/mnt/largepool/bulk/Music/lyrics.db"
STAGING = "/mnt/largepool/bulk/MediaLibrary/.music-organizer/staging.db"
SOURCE_ROOTS = [
    "/mnt/largepool/bulk/Music/CleanFLAC",
    "/mnt/largepool/bulk/Music/CleanMP3",
    "/mnt/largepool/bulk/Music/VGM",
]

# Flexible attributes carried across (column `lyrics` is carried separately).
FLEX_KEYS = ("lyrics_url", "lyrics_language", "lyrics_instrumental", "lyrics_backend")


def _norm(s: str | bytes | None) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "surrogateescape")
    return " ".join(s.strip().casefold().split())


def _meta_key(row) -> tuple[str, str, str]:
    return (_norm(row["albumartist"]), _norm(row["album"]), _norm(row["title"]))


def load_legacy(conn) -> list[dict]:
    """Legacy rows that actually have something to migrate."""
    conn.row_factory = sqlite3.Row
    out: list[dict] = []
    flex: dict[int, dict] = {}
    for r in conn.execute(
        "select id, path, albumartist, album, title, lyrics from items"
    ):
        out.append(dict(r))
    for r in conn.execute(
        "select entity_id, key, value from item_attributes where key in "
        "(%s)" % ",".join("?" * len(FLEX_KEYS)),
        FLEX_KEYS,
    ):
        flex.setdefault(r["entity_id"], {})[r["key"]] = r["value"]
    for row in out:
        row["flex"] = flex.get(row["id"], {})
    return [
        r
        for r in out
        if (r["lyrics"] or "").strip() or r["flex"]
    ]


def build_index(staging) -> tuple[dict[str, int], dict[tuple, int]]:
    """Index staging items by (a) source-path tail and (b) metadata key."""
    by_path: dict[str, int] = {}
    by_meta: dict[tuple, int] = {}
    staging.row_factory = sqlite3.Row
    for r in staging.execute(
        "select id, albumartist, album, title from items"
    ):
        by_meta.setdefault(_meta_key(r), r["id"])
    # mo_source is normally a hex of the absolute source path, but items
    # organized by the very first build stored a plain path instead. Accept
    # both: a valid hex string decodes, anything else is treated as a path.
    def _decode(value: str) -> str | None:
        try:
            return bytes.fromhex(value).decode("utf-8", "surrogateescape")
        except ValueError:
            return value  # legacy plain-path form

    for r in staging.execute(
        "select entity_id, value from item_attributes where key = 'mo_source'"
    ):
        abs_path = _decode(r["value"])
        if not abs_path:
            continue
        for root in SOURCE_ROOTS:
            if abs_path.startswith(root + os.sep):
                tail = os.path.relpath(abs_path, root)
                by_path.setdefault(tail, r["entity_id"])
                break
    return by_path, by_meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legacy", default=LEGACY)
    ap.add_argument("--staging", default=STAGING)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.legacy):
        print(f"legacy library not found: {args.legacy}", file=sys.stderr)
        return 2
    if not os.path.exists(args.staging):
        print(f"staging library not found: {args.staging}", file=sys.stderr)
        return 2

    legacy = sqlite3.connect(args.legacy)
    staging = sqlite3.connect(args.staging, timeout=30.0)

    rows = load_legacy(legacy)
    print(f"legacy rows carrying lyrics: {len(rows)}")
    by_path, by_meta = build_index(staging)
    print(f"staging index: {len(by_path)} by source path, {len(by_meta)} by metadata")

    matched_path = matched_meta = unmatched = 0
    lyrics_written = flex_written = 0

    with staging:
        for row in rows:
            rel = row["path"].decode("utf-8", "surrogateescape")
            item_id = by_path.get(rel)
            if item_id is not None:
                matched_path += 1
            else:
                item_id = by_meta.get(_meta_key(row))
                if item_id is not None:
                    matched_meta += 1
            if item_id is None:
                unmatched += 1
                print(f"  unmatched: {rel}")
                continue

            lyrics = row["lyrics"]
            if lyrics and (lyrics or "").strip():
                # The `lyrics` column is a plain field on the item.
                staging.execute(
                    "update items set lyrics = ? where id = ?",
                    (lyrics, item_id),
                )
                lyrics_written += 1
            for key, value in row["flex"].items():
                staging.execute(
                    "insert or replace into item_attributes "
                    "(entity_id, key, value) values (?, ?, ?)",
                    (item_id, key, value),
                )
                flex_written += 1

    if args.dry_run:
        print("dry run: rolling back")
        staging.rollback()
    reported = (
        f"matched by path {matched_path}, by metadata {matched_meta}, "
        f"unmatched {unmatched}; lyrics {lyrics_written}, flexattrs {flex_written}"
    )
    print(reported)

    # Verify on disk: how many staging items now carry lyrics?
    n = staging.execute(
        "select count(*) from items where lyrics is not null and lyrics != ''"
    ).fetchone()[0]
    print(f"staging items with lyrics now: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
