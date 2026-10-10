# music-organizer

Curate a well-organized, **deduplicated** copy of the music library under
`/mnt/largepool/bulk/MediaLibrary/Music`, built out of **reflink clones**
(`cp --reflink=always` semantics) so the copy costs inodes, not gigabytes.

Companion to [`media-organizer`](../media-organizer), which does the same for
video. Same house rules, same hard-won gotchas.

> **Sloppy-code warning.** Unreviewed, AI-written, environment-specific. Read
> it before you run it, and check the paths.

## What it does

Sources (strictly **read-only**):

```
/mnt/largepool/bulk/Music/CleanFLAC   lossless archives
/mnt/largepool/bulk/Music/VGM         sequenced music
/mnt/largepool/bulk/Music/CleanMP3    lossy
```

Destination: `/mnt/largepool/bulk/MediaLibrary/Music`.

```mermaid
flowchart TD
  A["Sources<br/>never written"] -->|"beet import -C -W<br/>copy off · write off"| B["staging.db<br/>tags + MB matches live HERE"]
  B -->|"beet musicorganize"| C["group by release-group<br/>(fallback: artist+album)"]
  C --> D["rank: lossless > lossy<br/>tracks > duration > JP > year"]
  D -->|"FICLONE shim"| E["Artist Name - Album Name (Year)/NN Title.ext"]
  E --> F["normalized tags written<br/>into the CLONE only"]
  B -.->|"shared"| G["mbcache.db<br/>every MB response"]
```

`mbcache.db` is the `mbcache` plugin: beets persists **nothing** from
MusicBrainz to disk, so without it any re-import (or a second source tree
holding the same release) re-queries everything. Every API call funnels through
one method, `MusicBrainzAPI._get_resource`, which the plugin wraps. Measured:
0.25 s live, 0.00 s on the cached repeat with an identical payload.

Rate limiting is beets' own and needs no tuning: musicbrainz.org is queried at
`per_second=1.0` (the documented maximum) and a 429 is retried with
`Retry(total=6, backoff_factor=0.5)`. We query as fast as the host allows and
never harder.

Two phases, deliberately:

1. **Index** — `beet import -C -W`: no copy, no write. MusicBrainz matches are
   stored in the *database*, so a source file's bytes (and therefore its
   checksum) never change. `incremental` makes re-runs cheap.
2. **Organize** — `beet musicorganize`: picks one winner per work, clones only
   that, and writes clean tags into the clone.

Cloning is a separate pass on purpose. Cloning first and deleting losers later
is expensive on ZFS for no benefit: freeing shared blocks churns the
block-reference table, and `rm` of a reflinked tree sits in `D` state for
minutes (documented in `media-organizer/README.md`).

## Layout

```
config.yaml                  the beets config (paths, import policy, matching)
beetsplug/musicorganize.py   the `musicorganize` command
beetsplug/mbcache.py         persists MusicBrainz responses to SQLite
shim/reflink.py              FICLONE reflink module (shadows the PyPI package)
install.sh                   idempotent deploy: venv, shim, units
run-full.sh                  index everything, then curate (tmux-friendly)
systemd/*.service, *.timer   daily index; manual organize
```

## Naming

```
<albumartist> - <album> (<year>)/
    <track> <title>.<ext>
```

beets zero-pads `$track` to two digits (`01`, `02`, …, `12`), so "number on
top" needs no template work. Verified against a real album on the pool.

## Why there is a `shim/`

beets' `reflink` option does `import_module("reflink")` — the PyPI package.
That package clones and *then* `copystat`s, which fails on
`aclmode=restricted` ZFS:

```
OSError: Could not copy permissions (errno EPERM)
```

So, without `shim/reflink.py`:

* `reflink: yes` → every clone aborts with a `FilesystemError`;
* `reflink: auto` → the clone works, the `copystat` fails, and beets
  **silently falls back to a byte copy** — full space, no warning.

`shim/reflink.py` is a bare `FICLONE` ioctl (`fcntl.ioctl(dst, 0x40049409,
src)`), stdlib only. Its semantics are exactly `cp --reflink=always`: shared
blocks, no metadata copying, and a real error if the clone fails.

It is put on `PYTHONPATH` so it shadows any installed `reflink` distribution.

## Install

```sh
./install.sh          # venv, shim, program files, systemd user units
./install.sh --dry-run
```

Then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now music-organizer-index.timer
systemctl --user list-timers music-organizer-index.timer
```

`install.sh` installs into `~/music-organizer` (override with
`MUSIC_ORGANIZER_PREFIX`) and units into `~/.config/systemd/user`. It does not
enable or start anything — that is yours to do.

### First run by hand

```sh
export BEETSDIR=~/music-organizer PYTHONPATH=~/music-organizer/shim
B=~/music-organizer/.venv/bin/beet

# 1. index (read-only over the sources)
$B -c ~/music-organizer/config.yaml import -C -W \
   /mnt/largepool/bulk/Music/CleanFLAC /mnt/largepool/bulk/Music/VGM

# 2. curate — always dry-run first
$B -c ~/music-organizer/config.yaml musicorganize -n
$B -c ~/music-organizer/config.yaml musicorganize
```

## `beet musicorganize`

```
beet musicorganize [-n] [-f] [-d DEST] [QUERY...]

  -n, --dry-run   show what would be cloned, change nothing
  -f, --force     re-clone even if already organized
  -d, --dest      destination root (default: plugin config `dest`)
```

Grouping identity: `mb_releasegroupid` when MusicBrainz supplied one,
otherwise case-folded `albumartist` + `album`.

Winner ranking, highest wins:

| # | Criterion | Why |
|---|---|---|
| 1 | fraction of lossless tracks | lossless > lossy |
| 2 | number of tracks | covers "Japanese CDs have more tracks" |
| 3 | total duration | same, second order |
| 4 | `country == JP` | Japanese release > rest of the world |
| 5 | earliest year | deterministic-ish tie-break |

Re-running is safe: the original path is stashed in the flexible attribute
`mo_source`, and any item carrying it is skipped unless `--force`. Losers get
`mo_role=alternate` and `mo_group=<key>`, so the alternates are discoverable
without being cloned.

## Known limits

* **Grouping is a heuristic for 98% of the library.** Sampled 400 real FLACs:
  `MUSICBRAINZ_ALBUMID` in 4%, `MUSICBRAINZ_RELEASEGROUPID` in 2%,
  `RELEASECOUNTRY` in 2%. Autotag fills those in the DB where it can match;
  what it cannot match falls back to artist+album, which merges distinct
  pressings that share both. That is the requested "one release per work", but
  it will occasionally be wrong.
* **JP preference works on `country`, which is usually absent** for unmatched
  albums. Where it is absent, the track-count and duration rules decide — which
  is exactly what was specified, but it means "JP wins" is inferred from
  running order, not from a country tag.
* **Tag writing changes the clone's checksum.** Intended: the clone is the
  mutable object; the source is hash-verified as unchanged.

## Migrating the legacy lyrics

The old library (`Music/lyrics.db`) had lyrics and `lyrics_*` flexible
attributes on 741 items. The new staging library is built from the source
trees and inherits none of them, so:

```sh
python3 migrate_lyrics.py --dry-run   # report only
python3 migrate_lyrics.py             # write
```

It matches each legacy row to a staging item **by exact source path first**
(the staging DB records the absolute source path in `mo_source`, so the
relative tail is exact and survives autotag rewriting artist/album
spellings), falling back to normalized artist+album+title. Both the plain
`lyrics` column and the four `lyrics_*` flexattrs are carried.

Measured: 737 of 739 rows matched by path, 2 288 flexattrs written, 737 items
carry lyrics afterwards, and a re-run is a no-op. `--dry-run` needs no care:
it rolls back.

## Verifying it did not cost space, and did not touch a source

**`stat %b` cannot tell you whether a clone is sharing blocks.** The honest
measurement is a **dataset-scoped `used` delta**:

```sh
# on the host that owns the pool (the container has no `zfs`)
zfs get -Hp -o value used /mnt/largepool/bulk     # before / after -> delta ~0
```

That trap cost a real debugging session here, so it is worth stating plainly.
A just-created clone reports `st_blocks=1`:

```
immediate blocks: 1
t+30s   blocks: 48582      <-- after the next txg commits
source  blocks: 48582
```

ZFS reports **referenced**, not charged/shared, blocks once the transaction
group commits, so `stat`, `du` and `ls -s` converge on the full size whether or
not the data is shared. Observing `blocks == size/512` therefore proves nothing.

What *is* conclusive from inside the container is that the clone was made by
FICLONE and not by the copy fallback: `beets.util.reflink(..., fallback=False)`
raises on failure, so a returned clone is genuinely shared. The shim never
falls back.

Sources are verified untouched by hashing them before and after, or simply:

```sh
find /mnt/largepool/bulk/Music/{CleanFLAC,VGM,CleanMP3} -type f -mmin -60
```

which must be empty.
