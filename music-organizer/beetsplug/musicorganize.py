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


def _group_key(album) -> str:
    """Identity of the underlying *work*.

    MusicBrainz release-group id when we have one, otherwise a normalized
    artist+album pair. The fallback is a heuristic: it merges distinct
    pressings that share artist and album title, which is the requested
    "one release per work" behaviour but will occasionally be wrong.
    """
    rgid = (album.get("mb_releasegroupid") or "").strip()
    if rgid:
        return "rg:" + rgid
    artist = (album.albumartist or "").strip().casefold()
    title = (album.album or "").strip().casefold()
    return "name:" + artist + "\x00" + title


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

            query = " ".join(args) if args else None
            albums = list(lib.albums(query))

            groups = defaultdict(list)
            for album in albums:
                groups[_group_key(album)].append(album)

            organized = 0
            skipped = 0
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

                organized += 1 if moved else 0
                skipped += 0 if moved else 1

            ui.print_(
                f"musicorganize: {len(groups)} works, "
                f"{organized} organized, {skipped} skipped/unchanged"
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
                item[SRC_ATTR] = displayable_path(item.path)
            item[ROLE_ATTR] = "winner"
            item[GROUP_ATTR] = key

            # Clones and updates item.path; the source is only read.
            item.move(operation=MoveOperation.REFLINK, basedir=dest_bytes)

            if write_tags:
                item.try_write()

            item.store()
            moved = True
        return moved
