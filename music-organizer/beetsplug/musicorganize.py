"""beets plugin: `beet musicorganize` -- curate a deduplicated MusicLibrary.

Contract
--------
* **Sources are immutable.** This command only ever *reads* source files.
  Writes happen exclusively to reflink clones under the destination tree.
* **One release per work.** Albums are grouped by ``mb_releasegroupid`` when
  MusicBrainz supplied one, else by case-folded ``albumartist`` + ``album``.
  One winner per group is cloned; every other candidate is recorded as an
  alternate and left alone.
* **Winner ranking** (highest wins), exactly as specified:

  1. lossless over lossy       (fraction of lossless tracks in the album)
  2. more tracks
  3. longer total duration
  4. ``country == JP``
  5. earliest year

* **Clone = ``cp --reflink=always``.** Cloning goes through
  ``beets.util.reflink`` -> the ``reflink`` module shadowed on ``PYTHONPATH``
  (see ``shim/reflink.py``), which is a bare FICLONE ioctl. It never falls
  back to a byte copy, so a broken clone is a visible error, not silent space
  use.

Idempotency
-----------
Before cloning, the item's original path is stashed in the flexible attribute
``mo_source``. A re-run skips any item that already carries it, so the command
can be re-executed freely. ``--force`` re-does the work.
"""

from __future__ import annotations

import os
import unicodedata
from collections import defaultdict

from beets import ui
from beets.plugins import BeetsPlugin
from beets.util import MoveOperation, displayable_path, normpath

LOSSLESS_FORMATS = {
    "flac",
    "alac",
    "ape",
    "wavpack",
    "wv",
    "aiff",
    "aif",
    "wav",
    "dsf",
    "dff",
    "shn",
    "tta",
}

# Flexible attributes recording provenance and grouping.
SRC_ATTR = "mo_source"
ROLE_ATTR = "mo_role"
GROUP_ATTR = "mo_group"


def _is_lossless(item) -> bool:
    return (item.format or "").lower() in LOSSLESS_FORMATS


# Typographic punctuation that NFKC does *not* fold to ASCII. Apostrophes and
# quotes are the important ones: "B'z" and "B’z" are the same artist, but
# NFKC leaves U+2019 alone, so without this they group as two works.
_PUNCT_FOLD = str.maketrans(
    {
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u02bc": "'",  # modifier letter apostrophe
        "\u2032": "'",  # prime
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u2033": '"',  # double prime
    }
)


def _norm_text(s: str) -> str:
    """Case-fold and Unicode-normalize a field for grouping.

    NFKC maps full-width forms and compatibility variants onto their plain
    shapes, and `_PUNCT_FOLD` handles the typographic quotes NFKC leaves
    alone, so a release tagged two ways by different sources groups together
    instead of being cloned twice.
    """
    s = unicodedata.normalize("NFKC", s or "")
    return s.translate(_PUNCT_FOLD).strip().casefold()


def _keys(album) -> tuple[str, str]:
    """Both grouping identities for an album.

    Returns ``(rg_key, name_key)``. An album that autotag matched carries a
    release-group id; the same release from a source tree that was not matched
    does not. Grouping on the rg id alone would therefore keep the matched and
    unmatched copies of one work apart, so the two identities are unioned.
    """
    rgid = (album.get("mb_releasegroupid") or "").strip()
    name = "name:" + _norm_text(album.albumartist) + "\x00" + _norm_text(album.album)
    return ("rg:" + rgid) if rgid else name, name


def _group_albums(albums):
    """Group albums into works, unioning rg-id and artist+album identities."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Prefer an rg-key as the canonical root so groups read nicely.
            root, other = (ra, rb) if ra.startswith("rg:") else (rb, ra)
            parent[other] = root

    for album in albums:
        rg_key, name_key = _keys(album)
        find(rg_key)
        find(name_key)
        union(rg_key, name_key)

    groups: dict[str, list] = defaultdict(list)
    for album in albums:
        rg_key, name_key = _keys(album)
        groups[find(rg_key)].append(album)
    return groups


def _rank(album):
    """Sort key for choosing the best release in a group. Larger is better."""
    items = list(album.items())
    if not items:
        return (0.0, 0, 0.0, 0, 0, 0.0)
    lossless_frac = sum(1 for i in items if _is_lossless(i)) / len(items)
    n_tracks = len(items)
    duration = sum(float(i.length or 0.0) for i in items)
    jp = 1 if (album.get("country") or "").upper() == "JP" else 0
    year = int(album.year or 0)
    # Earliest year wins, so negate it; album id breaks exact ties.
    return (lossless_frac, n_tracks, duration, jp, -year, -(album.id or 0))


def _describe(album) -> str:
    items = list(album.items())
    n = len(items)
    n_lossless = sum(1 for i in items if _is_lossless(i))
    dur = sum(float(i.length or 0.0) for i in items)
    return (
        f"{album.albumartist} - {album.album} ({album.year or '?'}) "
        f"[{n} tracks, {n_lossless} lossless, {dur / 60.0:.1f} min, "
        f"country={album.get('country') or '?'}, "
        f"rg={album.get('mb_releasegroupid') or '-'}]"
    )


class MusicOrganizePlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "dest": "/mnt/largepool/bulk/MediaLibrary/Music",
                "dry_run": False,
                "force": False,
                "write_tags": True,
            }
        )

    def commands(self):
        cmd = ui.Subcommand(
            "musicorganize",
            help="reflink the best release of each work into the library",
            aliases=("mo",),
        )
        cmd.parser.add_option(
            "-n",
            "--dry-run",
            action="store_true",
            dest="dry_run",
            default=False,
            help="show what would be cloned, change nothing",
        )
        cmd.parser.add_option(
            "-f",
            "--force",
            action="store_true",
            dest="force",
            default=False,
            help="re-clone even if already organized",
        )
        cmd.parser.add_option(
            "-d",
            "--dest",
            action="store",
            dest="dest",
            default=None,
            help="destination root (default: plugin config `dest`)",
        )

        def func(lib, opts, args):
            self.config.set_args(opts)

            dest = opts.dest or self.config["dest"].as_str()
            dry_run = bool(opts.dry_run) or self.config["dry_run"].get(bool)
            force = bool(opts.force) or self.config["force"].get(bool)
            write_tags = self.config["write_tags"].get(bool)

            dest_bytes = normpath(dest)

            # Scope by *item* path when a query is given: an Album's `path`
            # is a computed destination directory, not its source location,
            # so a `path::` query against albums matches nothing. Items carry
            # the real source paths.
            #
            # Pass argv straight through as a sequence: `lib.*` parses a
            # sequence component-wise, whereas a joined string would go
            # through `shlex.split` and tear paths containing spaces apart.
            if args:
                album_ids = {
                    item.album_id for item in lib.items(args) if item.album_id
                }
                requested = [
                    a for a in (lib.get_album(i) for i in album_ids) if a
                ]
                # Scope to the requested albums, then pull in *every* album
                # that shares a work identity with any of them, so the group
                # is complete. Grouping is the one operation that must be
                # global: a per-album run alone sees one candidate and can
                # never collapse duplicates.
                keys = set()
                for a in requested:
                    keys.update(_keys(a))
                all_albums = list(lib.albums())
                albums = [
                    a
                    for a in all_albums
                    if not any(i.get(SRC_ATTR) for i in a.items())
                    or keys & set(_keys(a))
                ]
            else:
                albums = list(lib.albums())

            groups = _group_albums(albums)

            organized = 0
            skipped = 0
            pruned = 0
            for key, members in sorted(groups.items()):
                winner = max(members, key=_rank)
                losers = [a for a in members if a is not winner]

                if len(members) > 1:
                    ui.print_(
                        f"group {key} ({len(members)} releases), winner: "
                        f"{_describe(winner)}"
                    )
                    for loser in losers:
                        ui.print_(f"    alternate: {_describe(loser)}")

                moved = self._organize_album(
                    winner, dest, dry_run, force, write_tags, key
                )
                for loser in losers:
                    for item in loser.items():
                        item[ROLE_ATTR] = "alternate"
                        item[GROUP_ATTR] = key
                        item.store()
                    if not dry_run:
                        pruned += self._prune_alternate(
                            loser, dest_bytes, lib.directory
                        )

                organized += 1 if moved else 0
                skipped += 0 if moved else 1

            ui.print_(
                f"musicorganize: {len(groups)} works, "
                f"{organized} organized, {skipped} skipped/unchanged, "
                f"{pruned} stray alternate file(s) removed"
            )

        cmd.func = func
        return [cmd]

    def _organize_album(self, album, dest, dry_run, force, write_tags, key):
        """Clone + tag one album's tracks. Returns True if anything moved."""
        dest_bytes = normpath(dest)
        moved = False
        for item in album.items():
            if item.get(SRC_ATTR) and not force:
                self._log.debug("already organized, skipping {}", item.title)
                continue

            target = item.destination(basedir=dest_bytes)
            if dry_run:
                ui.print_(
                    f"  would reflink {displayable_path(item.path)}\n"
                    f"             -> {displayable_path(target)}"
                )
                moved = True
                continue

            if not item.get(SRC_ATTR):
                # Hex-encode: a filename with invalid UTF-8 yields surrogate
                # escapes (PEP 383), and sqlite refuses those. The value is
                # only used as an "already organized" marker, and hex is
                # lossless, unlike sanitising it.
                item[SRC_ATTR] = os.fsencode(item.path).hex()
            item[ROLE_ATTR] = "winner"
            item[GROUP_ATTR] = key

            # One unclonable track must not abort the rest of its album. Log
            # and carry on; a re-run retries whatever failed.
            try:
                # Clones and updates item.path; the source is only read.
                item.move(operation=MoveOperation.REFLINK, basedir=dest_bytes)

                if write_tags:
                    item.try_write()

                item.store()
            except Exception as exc:
                self._log.error(
                    "could not organize {}: {}",
                    displayable_path(item.path),
                    exc,
                )
                continue
            moved = True
        return moved

    def _prune_alternate(self, album, dest_bytes, libdir) -> int:
        """Remove a losing release's *destination* copies, if any.

        A loser is normally un-cloned. But a release can be organized as a
        winner in one run and lose to a better pressing later, leaving its old
        copies in the destination. Those must go, or the library holds more
        than one release per work.

        Item paths are stored *relative to the library directory*, so they
        must be resolved against it before any comparison -- a bare
        `realpath()` would resolve them against the process's cwd.

        The guard is absolute: only files that resolve *under the destination*
        are ever removed, so a source file cannot be touched even if the
        database says something odd.
        """
        removed = 0
        # `dest_bytes` and `libdir` are bytestrings (normpath's return), so
        # the separator must be encoded -- mixing them is a TypeError.
        root = os.path.realpath(dest_bytes) + os.fsencode(os.sep)
        libdir = os.fsencode(libdir)
        for item in album.items():
            if not item.get(SRC_ATTR):
                continue  # never cloned -> nothing in the destination
            path = item.path
            if not os.path.isabs(path):
                path = os.path.join(libdir, path)
            real = os.path.realpath(path)
            if not real.startswith(root):
                continue
            try:
                os.unlink(real)
                removed += 1
            except FileNotFoundError:
                # Already pruned by an earlier run; the row keeps its marker,
                # so this is expected on every subsequent pass.
                pass
            except OSError as exc:
                self._log.warning("could not remove {}: {}", real, exc)

            # A loser's album directory is left empty once its files go.
            parent = os.path.dirname(real)
            while parent.startswith(root) and parent != root.rstrip(os.sep):
                try:
                    os.rmdir(parent)
                except OSError:
                    break  # not empty, or already gone
                parent = os.path.dirname(parent)
        return removed
