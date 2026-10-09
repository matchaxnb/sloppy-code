# human note

this works for me, for my files, in my context.

i run the VLM part against qwen3-VL-8b-instruct, with success.

lets me have a clean library.

# robot stuff

# media-organizer

Identify a visual-media tree under `$MEDIA_ROOT/<sources>` and lay it out under
`$MEDIA_LIBRARY/Cinema` using
**`cp --reflink=always`** (ZFS block-cloning — metadata only, blocks shared).

**Sources are immutable.** The pipeline only reads them; the only writes are new
files inside `MediaLibrary`. That makes the library disposable: wiping it and
re-running costs no space and no data, and is the intended recovery from any
mis-placement.

## Why it runs where the pool is mounted

The pool is ZFS with block cloning (`zfs_bclone_enabled=1`). A reflink shares the
source's blocks: a 25 GB film costs one inode, not 25 GB. SMB/CIFS cannot express
reflink (the client copies bytes), so this runs on a host that has the dataset
mounted locally.

### Gotchas, all hit for real

- **Cloning needs one dataset.** `/tmp` and the container rootfs are a different,
  `idmapped` dataset which cannot clone (`Operation not supported` /
  `Invalid cross-device link`). Keep sources and destination on one dataset.
- **Never pass `cp -p`.** bulk is `aclmode=restricted`; preserving mode/ownership
  fails `EPERM` and makes `cp` exit non-zero even though the clone succeeded.
  ACLs are inherited instead.
- **Dirty source data fails `EAGAIN`.** `zfs_bclone_wait_dirty=0`, so a file still
  dirty in cache (`Resource temporarily unavailable`) cannot be cloned until
  flushed. The code `sync`s and retries once.
- **Deleting a reflinked file is slow on ZFS.** Freeing shared blocks updates the
  block-reference table per block; `rm` of a large tree can sit in `D` state
  (uninterruptible, `cv_timedwait_common`) for minutes with ~1 s of CPU. It is
  working, not hung. To recover quickly, `mv` the directory aside (instant) and
  delete later.

### Verifying a clone

A container usually has no `zfs` binary, so run the check on the host that owns
the pool.

ZFS space properties are **dataset-scoped**; `zfs get` works on filesystems,
volumes, snapshots and bookmarks, never on a file. `du`, `ls -s`, `stat %b` and
`filefrag` report the *charged/referenced* view and cannot show that blocks are
shared.

```sh
zfs get -Hp -o value used,logicalused $DATASET   # before
cp --reflink=always SRC DST
zfs get -Hp -o value used,logicalused $DATASET   # after  -> delta ~ 0
```

Per `zfsprops(7)`:

- **`used`** — space consumed by the dataset and descendants; *space shared by
  multiple snapshots is not accounted for*. A reflink within the dataset adds
  nothing.
- **`referenced`** — data accessible by the dataset, "which may or may not be
  shared". A clone initially references the same amount, so it does **not** drop.
- **`logicalreferenced`** — as `referenced`, ignoring `compression`/`copies`.
- **`du` / `ls -s`** — the userused-charged view, not a sharing measurement.

Measured here: 2 GB file created, settled, cloned → pool delta **0 bytes**, clone
`stat %b` = 1 vs source 4,193,208. With other pool writers active the `df` delta
is swamped (±74 GB observed) — only a dataset-level `used` delta is conclusive.
`zfs-reflink-monitor.sh` samples those properties to a TSV.

## Layout produced

Under the `MediaLibrary/Cinema/` skeleton:

```
Cinema/<Category>/<Decade>/<Author>/<Title (Year)>/
    <Title (Year)> [2160p UHD].mkv
    <Title (Year)>.ja.srt              # original + preferred subs
    <Title (Year)>.alternatives.txt    # only when other versions were rejected
Cinema/<Category>/.../Season NN/<Title> SNNENN [tag].mkv
Cinema/_BONUS_/<Title (Year)>/<bonus name>
```

- **Decade** = release year. **Author** = director (film) or studio (series/anime).
- **Subtitle preference**: `ja`+`fr` for anime, `ja`+`en` otherwise.
- **Names** sanitized for SMB/NTFS; the dataset is case-insensitive.

### Categories

| kind | original language | animated | category |
|---|---|---|---|
| tv | ja | yes | `AnimeSeries` |
| tv | ja | no | `Series` (Japanese live-action) |
| tv | other | any | `Series` |
| movie | ja | yes | `AnimeFilms` |
| movie | ja | no | `FeatureFilms` |
| movie | other | yes | `Animation` |
| movie | other | no | `FeatureFilms` |

plus `ShortFilms` (runtime < 40 min **and** a standalone work — an instalment of a
multi-part title is not a short) and `_JUNK_` (media from bad sources, e.g.
`FilmsCollec/Junk`, that is still worth keeping). Source-dir hints
`Anime|Shorts|Experimental|Junk` are honoured before metadata.

## Selection: which version wins

### One file per presentation

A group is not reduced to a single file. Versions are grouped into
**presentations**, and **one file is kept per presentation**:

> **presentation = (aspect class, audio-language set)**

- **Aspect class** (`aspect_class`): `4:3` vs `16:9`, read from the probed
  width/height. A widescreen release of a 4:3 show is usually a pan-and-scan
  *crop* that loses picture, so it is a different presentation, not a better one.
  (The Buffy case: the 16:9 "popcorn" set and the open-matte 4:3 box set are both
  wanted.)
- **Audio languages** (`audio_languages`): a dub-only release and one carrying the
  original audio are different presentations; both are kept.

Grouping is by **compatibility, not exact equality** — a file whose languages
could not be probed must not split off a phantom family from an otherwise
identical file (same aspect + equal languages *or either side unknown* = one
presentation). Empty dimensions collapse to `16:9`, so a never-probed file does
not mint a phantom 4:3 family either.

Within a presentation, `quality_score()` decides the winner — so a 4K beats a
1080p and only the 4K is kept (same presentation, best resolution). Across
presentations both survive. When a group has more than one presentation, each
name takes its family label as an edition so Jellyfin does not mint a `__dup`:

```
S01E06 - The Pack [DVD] [4:3].mkv
S01E06 - The Pack [1080p Web] [16:9].mkv
```

The quality tag is built **per file** (the tag in brackets describes *that* file,
not the group's primary).

### The score (`quality_score()`)

Returns a tuple, most significant first:

1. **original-language audio present** (or multi-lingual including it)
2. **4:3 presentation, TV only** (`_is_43`, kind `tv`) — protects a 4:3 show from
   losing to a widescreen *crop* on resolution. Films are natively widescreen and
   are never downgraded for being so.
3. **resolution** (`8k` … `480p`)
4. **source authenticity** — `remux` > `blu-ray` encode > **raw disc (`m2ts`)**
   > `web-dl`/iTunes > `webrip` > `hdtv`/`dvdrip`
5. HDR
6. bit depth
7. bitrate

Axis 1 dominates deliberately: a dub-only release can never outrank an
original-audio copy *within a presentation*. Axis 3 encodes your "remux better
than raw blu-ray" rule — a disc stream is authentic but is not a finished library
file, so an encode beats it.

Every rejected version is recorded in the store's **`alternative` table** (ranked,
with source path, score and size). It is **not** written beside the file: a stray
`.txt` is not library content and confuses Jellyfin. Render on demand instead.


## Grouping

Identity is `(kind, tmdb_id, season, episode)` when TMDB matched, else
`(kind, normalized title, year)`. Cross-root merges happen on that key, so the
same title in `Films` and `Movies` becomes one group and one winner.

Folder layout is a **hint, never authority** (sources are irregular):

- **Ancestor cascade** — nearest structural folder outward. Disc dirs (`BDMV`,
  `STREAM`, `PLAYLIST`, `AUXDATA`, `CERTIFICATE`), `Saison 3`, `Disc 2`,
  `Intégrale`, `Scans`, `collection`, `Subs`, `Extras` are recognised as
  structural and skipped rather than mistaken for titles.
- **Loose files** (all of `CleanFilms`) fall back to the filename.
- **Folder reading (series vs anthology)** — a folder's files are tokenised and
  compared as *sequences*; the longest shared leading run is the family, and what
  follows it decides:

  | remainder after the shared run | reading | example |
  |---|---|---|
  | a number | **series** — one work's instalments | `cowboy.bebop.e02…`, `Show - 01 (…)` |
  | a word | **anthology** — distinct works | `Tex Avery - Garden Gopher.mp4` |

  Tokenising makes this uniform across separators, so dotted scene names and
  spaced ones need no separate rules. It also covers the awkward forms:
  `e02`, `Ep10v2`, `3x01`, `S01E03`, and concatenated `S0401` (season *and*
  episode, so `S0401`/`S0501` never collide). A folder with no shared prefix but
  distinct leading numbers (`001 Un Yaourt…`) reads as a series too. The reading
  is a heuristic; its failure modes are bounded, which is what the override file
  below is for.

  Order of precedence in a single file: **override → anthology → Blu-ray disc →
  extras → series/episode → filename**. Discs and extras therefore still route to
  `_BONUS_` even inside a numbered folder.
- **Forced classifications** — `overrides.yaml` pins the cases the heuristics
  cannot win (see below).
- **Blu-ray discs** — one unit per disc, titled from the disc folder. The main
  title is the largest `STREAM/*.m2ts`, cross-checked against the playlist via
  **`bdinfo-rs`** (or the bundled `mpls.py` fallback). Everything else on the
  disc goes to `_BONUS_`. A disc whose feature spans several large segments
  (seamless branching) cannot be one file: the largest is placed and a
  `NEEDS_REMUX.txt` marker is written beside it.

### Naming anthology titles (title card → VLM)

`anthology_names.py` names the titles of an anthology (a disc of shorts, a
compilation) — a separate rename-phase worker, run after makemkv:

```sh
./.venv/bin/python anthology_names.py MediaLibrary/Remuxes/<Disc> --apply \
    --domain cartoon-classic --keyframes --grid 3x3 [--window 24]
```

**Frame sampling (`--keyframes`, preferred).** Decode **only I-frames**
(`-skip_frame nokey`) and keep roughly one per 2 s. A Blu-ray's keyframes are ~1 s
apart (GOP ≈ 24 at 24 fps) and far denser at cuts; a title card dwells 3–5 s, so a
2 s cadence cannot miss one while dropping the cut clusters that would otherwise
fill montage slots with near-identical frames. It is also far cheaper than the
`fps` filter. **Do not use `-vsync`** — it was removed in ffmpeg n9; decoding to
images uses `-fps_mode passthrough`. Without `--keyframes` the older `fps`-filter
sampling (`sample_frames`) is used.

**Grid density: 3×3, not 4×4.** Each tile is 640×480 and `xstack` does not rescale,
so a 4×4 montage is 2560×1920 — which the vision model downsizes until each tile
is unreadable. Measured on Tex Avery vol. 1: **4×4 → 7/19 titles, 3×3 → 18/19,
2×2 → fine too.** Denser is not better; past ~2 MP the tiles blur faster than the
extra context helps.

**Safe-zone crop (`SAFE_ZONE = 0.88`).** Title cards and credits sit inside the
title-safe area, so a centred 88% crop discards the frame border and gives each
tile more usable resolution (tiles are padded back to 4:3, so the grid stays even).

**What works (measured):**

- **A title card in the opening**, read by the VLM from a **montage grid** — one
  image, so the whole card sequence (studio → series banner → character card →
  title) is visible at once.
- **Window size dominates.** Sampling beyond ~18–24 s pulls *in-film signage*
  ("MALIBU SALOON") which the model mistakes for the title; `--window 24` with
  keyframes is the sweet spot.
- **Domain prompt matters.** `--domain cartoon-classic` seeds a reject list —
  characters, series banners, studios, credit/certificate lines — because those
  read at confidence 1.0 and would otherwise win.
- **Content beats confidence.** `pick_title` collects every card; character card
  and title card both read 1.0, so the choice is by *content*, not score.
- **The umbrella rule (generic):** a card naming a recurring character, franchise
  or sub-series (Droopy, Tom and Jerry, …) is the **SERIES**, not the title; the
  individual work has its own title card. This is what turns a bare `DROOPY` into
  `WAGS to RICHES` / `DAREDEVIL, DROOPY`. It is phrased generically on purpose —
  no per-studio name lists to overfit.
- **The credited-card rule:** when the grid's pick is missing or a bare character
  name, `_title_from_credited_card` reads frames individually and, on a card
  showing `Directed by`, hands *the text above the credit* back to the model.
- **Chapter count** flags a "play-all" (many chapters) for splitting rather than
  naming it after its first short. Renaming is `os.rename` — metadata, cheap.

**Measured results (Tex Avery Screwball Classics, keyframes + 3×3 + cartoon-classic):**

- **vol. 1** (`00003mpls_t*.mkv`, 19 shorts): **19/19** named.
- **vol. 2** (`00024mpls_t*.mkv`, 22 items): **21/22** named. The one miss is a
  52-minute censored-material documentary compilation with **no title card** —
  correctly left in `review-titles.txt` rather than guessed.
- Earlier D1–D4 set: ~62/64.

**Limitations (measured, not hidden):** a title shown **in media res** is missed by
the first-window pass; a work with **no title card** at all (the documentary) is
unnameable by this method. The model occasionally reorders a two-word title
(`Homesteader Droopy` → "Droopy, Homesteader") or reads a decoy; unresolved answers
go to the review file, never a wrong guess. Domain classification is **advisory
only** — it selects the title prompt, never gates the read.

### Raw DVD and ISO — remux, not reflink

A raw DVD (`VIDEO_TS/*.VOB`) and an ISO image cannot be represented by a reflink:
the feature is split across VOBs (often cut at the 1 GiB VOB boundary), and an
ISO is an opaque image — a reflinked copy is not consumable. These are therefore
**skipped** and an instead-run list of `makemkvcon` commands is written to
`MediaLibrary/to-remux.list`, one per disc, each extracting into its own folder
`MediaLibrary/Remuxes/<DiscTitle>/`.

`makemkvcon mkv` **remuxes** — streams are copied verbatim into an MKV, no
re-encode — one MKV per title, which is exactly the split a reflink cannot do.
The worker does **not** consume `Remuxes/` on its own; after running the commands,
integrate the **main title per disc** (largest file, with duration as a guard —
menus can report inane lengths) into the library in a separate pass.

### Forced classifications
Some sources are genuinely ambiguous — a film filed inside a series folder, a
numbered run that is really an anthology. Those are stated outright rather than
tuned for, in `overrides.yaml` (default `$XDG_CONFIG_HOME/media-organizer/`,
override with `--overrides` or `$MEDIA_OVERRIDES`):

```yaml
# Cowboy Bebop: the film sits alongside the 26-episode series and is named
# alike, so the heuristics merge them. Pin the film; force the episodes.
Cowboy Bebop: The Movie (2001):
  - FilmsCollec/Anime/Cowboy Bebop [BDRip 1080p FRE+JAP]/cowboy.bebop.e02.multi.1080p.bluray.x264-kazetv.mkv
  - CleanFilms/COWBOY BEBOP THE MOVIE - KNOCKIN ON HEAVENS DOOR BD/BDMV/STREAM/00000.m2ts

AnimeSeries:
  - FilmsCollec/Anime/Cowboy Bebop [BDRip 1080p FRE+JAP]/cowboy.bebop.*.mkv
```

A key is read as, in order: a known **category** (`FeatureFilms`, `AnimeSeries`,
`_BONUS_`, …) — the file is filed there, bypassing the category heuristics; a
**glob** containing `*` or `?`; otherwise a **title** — the file leaves whatever
group it would have joined and groups with the other files under that key.
Entries may be absolute paths or paths relative to a source root, so they stay
portable. `[` is not a glob metacharacter (source paths are full of `[BDRip]`).

The loader is a small subset of YAML (mappings to lists, comments, quoting) —
deliberately, to avoid a dependency. Anything it cannot parse **raises**: a
misread override would silently misplace the very files it was written to pin.

### Collisions

A destination name colliding with an existing file means an earlier run left a
stale copy. The library is disposable, so: if the size matches the source it is
already correct (skip); otherwise the stale file is **replaced**, keeping the
canonical name. If two ops in *one* plan want the same path (two groups that
should have merged), both are **skipped and logged** — never allowed to fight.

*(Previously this minted `<name>__dup.<ext>`, which left the stale file looking
canonical. Removed; `__dup`/`__copy` only exists in the drag-drop UI, where
keeping both copies is what a human actually wants.)*

## Files

| file | role |
|---|---|
| `media_ids.py` | parsing (guessit/anitopy), scoring, routing, categories, TMDB client, token-bucket request queue |
| `orchestrator.py` | walk → identify → group → probe → score → route → apply |
| `siblings.py` | folder reading: token-sequence family detection, series vs anthology |
| `overrides.py` | forced classifications (`overrides.yaml`) and their loader |
| `mpls.py` | minimal BDMV playlist reader (fallback when `bdinfo-rs` is absent) |
| `store.py` | SQLite: TMDB cache, probe cache, placement registry, fetch log |
| `filemanager.py` | two-pane drag-and-drop web UI; drops become reflinks |
| `run-apply.sh` | the streaming apply, for tmux |
| `run-fm.sh` | starts the UI on `:8099` |
| `zfs-reflink-monitor.sh` | dataset-space sampler — run where `zfs` exists |

### External tools

| tool | needed for | notes |
|---|---|---|
| `cp` (coreutils ≥ 9.0) | reflink | `--reflink=always`; 9.12 verified |
| `file` (file 5.x) | MIME classification of unknown extensions | already on Arch |
| `bdinfo-rs` | authoritative Blu-ray playlist scan | optional; falls back to `mpls.py` |
| `pymediainfo` | container/stream probing | bundles libmediainfo, no system package |
| `zfs` | verifying a clone | only on the host that owns the pool |

## Concurrency and resource posture

| stage | workers | bound by |
|---|---|---|
| identify | 1 queue, shared token bucket | TMDB rate (`TMDB_RATE`, default 20/s) |
| probe | `--probe-workers` (default 3) | disk read + CPU |
| copy | `--copy-workers` (default 2) | pool metadata ops |

The pool is shared, so defaults are deliberately modest; raise them only when
nothing else needs the pool. `--stream` keeps **memory bounded by the largest
group**: each group's videos are probed immediately before it is planned and the
feature dicts are dropped with the group. (Probing every video up front — the
earlier behaviour — retained a MediaInfo feature tree per video for the whole run
and was a genuine memory hog.)

Probing also short-circuits: a file under `MIN_PROBE_BYTES` (20 MB) is described
from its size without opening the container, since a trailer or menu clip cannot
be the presentation.

## Rate limiting (TMDB)

`TokenBucket` caps the global rate (default 20/s, burst 10) and is shared by all
workers, so raising worker count never raises the rate. On **429** the bucket is
drained and held (`penalise`) using the server's `Retry-After`, else 10 s — so a
throttling response slows *every* worker, not just the one that saw it. 5xx holds
2 s. Retries are exponential with jitter, up to 4 attempts. TMDB's documented
limit is ~40 req/s per key.

## Resumability

Everything expensive is persisted, so re-runs are cheap and idempotent:

- **TMDB** — `lookup`/`media` tables; a warm run makes **0** API calls.
- **Probe** — `probe` table keyed by `(path, size, mtime)`; re-runs skip MediaInfo
  entirely.
- **Copy** — `placement` maps the immutable source path → destination, so
  finished work is skipped and a `stat` size check is the fallback.

SQLite is single-writer here: writes funnel through one thread in batched
transactions, readers use thread-local connections under WAL. Worker threads
never write directly.

## Usage

```sh
cd ~/media-organizer
set -a; . ~/.config/media-organizer/tmdb.env; set +a

# held in tmux, detached
tmux new-session -d -s morg "~/media-organizer/run-apply.sh > /tmp/apply.log 2>&1"
tmux new-session -d -s fm   "~/media-organizer/run-fm.sh   > /tmp/fm.log 2>&1"

# dry run to a plan file, changes nothing
.venv/bin/python orchestrator.py --json /tmp/plan.json
```

`--root` is repeatable (defaults to the seven source dirs). `--dest` must end in
`MediaLibrary`. `--stream` writes progressively. `--rename` moves an
already-placed clone when its destination legitimately changes.

## Safety guards

- destination outside `MediaLibrary` → refused
- destination inside a source root → refused
- `src == dst` → refused
- stale file at the target → replaced only when its size differs from the source
- two ops wanting one path → skipped and logged
- `--reflink=always` only: a filesystem that cannot clone fails loudly, never
  silently full-copies
- the UI's **move** unlinks only sources already inside `MediaLibrary`

## State / verification (2026-10-07)

- **reflink**: 25 GB clone in ~1–2 s; pool `used` delta 0 on a controlled test.
- **sources**: byte-identical before/after (md5), throughout.
- **resume**: second identical run 0.3 s vs 15.6 s, zero `__dup`, registry intact.
- **real corpus**: ~2,300 videos across 7 roots; directors resolved correctly
  (silent-era, noir and contemporary directors and studios alike);
  7 Blu-ray discs handled; scores verified across the five axes.

## Open items

- Finish a clean full pass with the current code (the library was wiped
  deliberately; `MediaLibrary/stale-<epoch>/` holds the previous attempt and can
  be deleted).
- Optional lossless remux (ffmpeg `-c copy`) for `NEEDS_REMUX.txt` discs;
  reflink cannot filter tracks inside a file.
- `metadata.json` / `poster.jpg` provenance sidecars — designed, not written.
- Nothing is committed to git yet.
