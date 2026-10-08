#!/usr/bin/env python3
"""Audit how the folder classifier reads the real source corpus.

Development tool, not part of the pipeline. It walks the sources, groups files by
folder, and prints what `siblings.classify_folder` decides for each — so the
reading can be reviewed in bulk instead of one folder at a time.

    ./classify_folders.py                    # summary + all unusual readings
    ./classify_folders.py --all              # every folder
    ./classify_folders.py --root FilmsCollec # one source root
    ./classify_folders.py --show <substr>    # token detail for matching folders

Reads a manifest produced on the source host:

    find <root> -type f -printf '%P\\t%s\\n' > manifest.tsv     # per root
  or a single combined file with "<root>\\t<relpath>\\t<size>" per line.
"""
from __future__ import annotations
import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import siblings as S  # noqa: E402
import media_ids as M  # noqa: E402

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts"}
DEFAULT_MANIFEST = os.environ.get("MEDIA_MANIFEST", "manifest.tsv")


def load_manifest(path: str):
    """Yield (root, relpath, size). Accepts 2- or 3-column rows."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                yield parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                yield "", parts[0], parts[1]


def folders_with_videos(rows, min_videos: int = 3):
    """{(root, relative dir): [filename, ...]} for folders holding enough videos."""
    out = collections.defaultdict(list)
    for root, rel, _size in rows:
        if os.path.splitext(rel)[1].lower() not in VIDEO_EXT:
            continue
        out[(root, os.path.dirname(rel))].append(os.path.basename(rel))
    return {k: sorted(v) for k, v in out.items() if len(v) >= min_videos}


def per_file_verdict(root: str, relpath: str):
    """What the *per-file* path decides for a file, as (kind, label).

    A file whose folder is not a family is not "unreadable" — it goes through
    per-file discrimination, which is the normal path for films. Reporting it as
    unreadable would understate the pipeline badly, so the reading is labelled
    from what that path actually concludes.
    """
    relparts = relpath.split("/")
    path = os.path.join(root or "", relpath)
    try:
        kind = "tv" if M.parse_name(path)["episode"] is not None else "movie"
    except Exception:
        kind, label = "movie", "per-file (film)"
        return kind, label
    par = M.parse_name(path)
    if kind == "tv":
        s_, e_ = par.get("season"), par.get("episode")
        return kind, f"per-file tv S{s_ if s_ is not None else '?'}E{e_}"
    return kind, "per-file film"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--root", default=None, help="only folders under this source root")
    ap.add_argument("--all", action="store_true", help="print every folder")
    ap.add_argument("--show", default=None,
                    help="print token detail for folders matching this substring")
    ap.add_argument("--min-videos", type=int, default=3)
    ap.add_argument("--json", default=None, help="write {folder: reading} here")
    args = ap.parse_args(argv)

    if not os.path.exists(args.manifest):
        raise SystemExit(
            f"no manifest at {args.manifest!r}. Build one on the source host:\n"
            f"  find <root> -type f -printf '%P\\t%s\\n' > {args.manifest}")
    rows = list(load_manifest(args.manifest))
    folders = folders_with_videos(rows, args.min_videos)
    if args.root:
        folders = {k: v for k, v in folders.items() if k[0] == args.root}

    readings = collections.Counter()
    detail = {}
    for (root, d), names in sorted(folders.items()):
        kind, payload = S.classify_folder(names)
        readings[kind] += 1
        if isinstance(payload, dict):
            det = {k: v for k, v in payload.items() if not str(k).startswith("__")}
        else:
            det = payload
        detail[f"{root}|{d}"] = {
            "files": len(names),
            "reading": kind if kind else "per-file",
            "detail": det,
        }

    print(f"folders (>= {args.min_videos} videos): {len(folders)}")
    for k in ("series", "anthology", None):
        label = k if k else "unreadable"
        print(f"  {label:<11} {readings[k]}")

    if args.show:
        needle = args.show.lower()
        for (root, d), names in sorted(folders.items()):
            if needle not in (root + "/" + d).lower():
                continue
            kind, payload = S.classify_folder(names)
            run = S.common_prefix_run(names, min_len=1)
            print(f"\n### [{kind}] {len(names)} files  {root}/{d}")
            print(f"    shared run : {run}")
            print(f"    remainders : {S.remainders_after(names, run)[:6]}")
            for n in names[:6]:
                print(f"      {n[:88]}")
            if len(names) > 6:
                print(f"      ... +{len(names) - 6}")

    elif args.all:
        for (root, d), names in sorted(folders.items()):
            kind, payload = S.classify_folder(names)
            n = len(payload) if isinstance(payload, dict) else payload
            print(f"  [{kind or '--':<9}] {len(names):>4} files  {str(n):<22} {root}/{d}")
    else:
        # unusual: unreadable, or an anthology (both worth a look)
        print("\nunreadable or anthology folders:")
        for (root, d), names in sorted(folders.items()):
            kind, payload = S.classify_folder(names)
            if kind in (None, "anthology"):
                n = len(payload) if isinstance(payload, dict) else payload
                print(f"  [{kind or '--':<9}] {len(names):>4} files  {str(n):<18} {root}/{d}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(detail, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
