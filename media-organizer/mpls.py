#!/usr/bin/env python3
"""Minimal BDMV playlist (.mpls) reader.

A Blu-ray title is a *playlist* that concatenates STREAM/*.m2ts segments, so the
largest single segment is not necessarily the whole feature — seamless branching
interleaves repeated references to the shared part plus the differing scenes.

Rather than depend on the exact variable record layout, scan the file for the
unambiguous marker: a five-digit clip name immediately followed by "M2TS"
(e.g. "00295M2TS"). Every play item carries exactly one, in play order, at a
fixed stride. That yields the referenced segments and their reference counts.

From that, branching is directly visible:
  * one segment referenced (any number of items)  -> single-file feature
  * several distinct segments, each referenced    -> assembled from parts
"""
from __future__ import annotations
import os, re
from dataclasses import dataclass, field

_CLIP = re.compile(rb"(\d{5})M2TS")


@dataclass
class Playlist:
    name: str
    segments: list = field(default_factory=list)   # clip names, in play order
    referenced_size: int = 0                        # sum of distinct segment sizes
    largest_segment: int = 0                        # biggest referenced segment

    @property
    def n_items(self) -> int:
        return len(self.segments)

    @property
    def distinct(self) -> list:
        seen = []
        for s in self.segments:
            if s not in seen:
                seen.append(s)
        return seen

    @property
    def multi_segment(self) -> bool:
        """Several distinct clips, none of which dominates -> needs a remux."""
        if len(self.distinct) < 2 or not self.referenced_size:
            return False
        return self.largest_segment < 0.9 * self.referenced_size


def parse_mpls(path: str, stream_dir: str | None = None) -> Playlist | None:
    try:
        with open(path, "rb") as fh:
            b = fh.read()
    except OSError:
        return None
    if len(b) < 20 or not b.startswith(b"MPLS"):
        return None
    pl = Playlist(name=os.path.basename(path))
    pl.segments = [m.group(1).decode("ascii", "ignore") for m in _CLIP.finditer(b)]
    if not pl.segments:
        return None

    if stream_dir:
        sizes = {}
        for seg in pl.distinct:
            for ext in (".m2ts", ".M2TS"):
                fp = os.path.join(stream_dir, seg + ext)
                if os.path.exists(fp):
                    try:
                        sizes[seg] = os.path.getsize(fp)
                    except OSError:
                        pass
                    break
        pl.referenced_size = sum(sizes.values())
        pl.largest_segment = max(sizes.values()) if sizes else 0
    return pl


def disc_playlists(bdmv_dir: str) -> list[Playlist]:
    """Readable playlists of a BDMV dir, biggest referenced size first."""
    pdir = os.path.join(bdmv_dir, "PLAYLIST")
    sdir = os.path.join(bdmv_dir, "STREAM")
    try:
        names = [n for n in os.listdir(pdir) if n.lower().endswith(".mpls")]
    except OSError:
        return []
    out = []
    for fn in names:
        p = parse_mpls(os.path.join(pdir, fn), sdir)
        if p is not None and p.segments:
            out.append(p)
    out.sort(key=lambda p: (p.referenced_size, p.n_items), reverse=True)
    return out


def main_playlist(bdmv_dir: str) -> Playlist | None:
    pls = disc_playlists(bdmv_dir)
    return pls[0] if pls else None


if __name__ == "__main__":
    import sys
    for d in sys.argv[1:]:
        print("==", d)
        for p in disc_playlists(os.path.join(d, "BDMV"))[:5]:
            print("  %-12s items=%-5d distinct=%-3d ref=%.2fGB largest=%.2fGB %s" % (
                p.name, p.n_items, len(p.distinct), p.referenced_size / 2**30,
                p.largest_segment / 2**30, "MULTI" if p.multi_segment else "single"))
