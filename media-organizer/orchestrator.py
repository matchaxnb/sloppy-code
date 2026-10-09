#!/usr/bin/env python3
"""media-organizer orchestrator.

Anchors grouping on the source DIRECTORY structure (which is already per-title)
rather than per-file name guessing. For each source root:

    <root>/<Title (Year)>/...                  -> movie group (folder = title)
    <root>/<Show (Year)>/Season NN/...         -> per-episode tv groups
    <root>/<Title (Year)>/<Extra|Bonus|...>/   -> bonus

One best version is chosen per group (original-language audio first, then
quality). Subtitles/extras inherit the group's metadata.

Dry run by default; `--apply` reflinks (`cp --reflink=always`) under MediaLibrary.
Sources are never modified.
"""
from __future__ import annotations
import argparse, contextlib, json, os, subprocess, sys, time, queue, threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import media_ids as M
import siblings
import config as C
import overrides as OV

# Source roots and the library location come from the environment (see config.py):
# this codebase names no machine, mount point or pool.

SEASON_RE = re.compile(r"^(?i:season|s)[ ._-]*(\d{1,2})$") if False else None

import re
SEASON_RE = re.compile(r"^(?i:(?:season|saison|s)[ ._-]*)(\d{1,2})$")
COUR_RE = re.compile(r"^(?i:(?:cour|part|tome|t)[ ._-]*)(\d{1,2})$")

# Bonuses are detected unreliably (a short, an interview and a making-of are the
# same shape to the heuristics) and Jellyfin files them badly, so they are left
# OUT of the staged library until that is sorted. The ops are still computed —
# they can be inspected — they are simply not applied.
STAGE_BONUSES = False
TV_RE = M.TV_RE
# structural folders carry no title identity -> always skipped in the ancestor cascade
STRUCTURAL_RE = re.compile(
    r"^(?i:(?:season|saison|s|disc|disk|cd|dvd|part|cour|tome|t|vol|volume)[ ._-]*\d{1,2}"
    r"|integrale|intégrale|integral|collection|scans|raw|bonus|extras?|featurettes?|"
    r"special features|subs|subtitles|menus?|ncop|nced|samples?|bdmv|stream|auxdata|"
    r"bdjo|jar|playlist|clipinf|certificate)$")
# A season/part container that carries a QUALIFIER after the number ("Saison 8 -
# Animée", "Saison 1 à 4", "Season 03 - NTSC DVD"). STRUCTURAL_RE only accepts a
# bare "Season NN"; without this a range folder ("Saison 1 à 4") is read as a
# title ("à 4" -> a 2014 sitcom) and its files are filed under the wrong show.
STRUCTURAL_PREFIX_RE = re.compile(
    r"^(?i:(?:season|saison)s?|disc|disk|cd|dvd|part|cour|tome|vol|volume)"
    r"[ ._-]*\d{1,2}(?:[\s._-]|$)")

# A folder that carries the SHOW and the SEASON together ("BABYLON 5 - SEASON 03
# - NTSC DVD...", "Buffy The Vampire Slayer S03 1080p..."). Such a folder is a
# season pack, not a title: treating it as one gave every season its own show
# ("Babylon 5 S03"), so a single series fragmented into one entry per season.
_SEASON_IN_FOLDER_RE = re.compile(
    r"(?i)^(?P<show>.+?)[\s._-]+(?:season|saison|s)[\s._-]*(?P<num>\d{1,2})(?:[\s._-]|$)")


def season_folder(folder: str) -> tuple[str | None, int | None]:
    """(show title, season) when the folder embeds a season, else (None, None).

    Guards against eating a real title: the show part must be non-empty, and the
    number must be a plausible season (1-40). "Season 03" alone is handled by
    SEASON_RE as a pure season folder, so this only fires on the combined form.
    """
    m = _SEASON_IN_FOLDER_RE.match(folder.strip())
    if not m:
        return None, None
    show = m.group("show").strip(" .-_")
    try:
        num = int(m.group("num"))
    except (TypeError, ValueError):
        return None, None
    if not show or not (1 <= num <= 40):
        return None, None
    # A dotted scene name ("Buffy.the.Vampire.Slayer.s04.1999") keeps dots in the
    # show part; normalise separators so the query is a title, not a filename.
    show = re.sub(r"[._]+", " ", show).strip()
    return show, num


def is_structural(folder: str) -> bool:
    f = folder.strip()
    return bool(STRUCTURAL_RE.match(f) or STRUCTURAL_PREFIX_RE.match(f))

# folders that are containers/categories, never titles -> never send to TMDB
NON_TITLE = {"extras", "extra", "bonus", "bonuses", "featurettes", "special features",
             "samples", "sample", "shorts", "junk", "anime", "animearepack", "repack",
             "films", "cleanfilms", "movies", "series", "cleanseries",
             "filmscollec", "seriescollec", "subs", "subtitles", "menu", "menus",
             "ncop", "nced", "__macosx",
             # generic inner containers that are not titles: a bare "Episodes"
             # folder is otherwise sent to TMDB and resolves to the sitcom
             # "Episodes" (2011), hijacking the real show from an outer folder.
             "episode", "episodes", "ep", "eps", "videos", "video", "media",
             "cd1", "cd2", "cd3", "cd4", "dvd1", "dvd2", "dvd3", "dvd4",
             "disc1", "disc2", "disc3", "disc4"}


def is_title_folder(folder: str) -> bool:
    return folder.strip().lower() not in NON_TITLE


# ------------------------------------------------------------------ inventory
DISC_STRUCT_RE = re.compile(r"^#\s*disc structure:\s*([a-z]+)")


def read_disc_sidecars(dirpath: str) -> tuple[str, dict]:
    """(structure, {name_or_stem -> release_line}) from a disc's worker output.

    `anthology_names.py` writes `series-index.tsv` (<release line>\\t<file>\\t<title>)
    and `disc-structure.txt` beside the videos. Reading them here lets the planner
    act on what the vision stage found — in particular group a collection's shorts
    under their release line, which the file names alone cannot say. Names are
    keyed by both the file name and its stem, because the index is written with
    the names as they were when the worker ran (before its own rename).
    """
    struct, lines = "", {}
    sp = os.path.join(dirpath, "series-index.tsv")
    if os.path.isfile(sp):
        try:
            with open(sp, encoding="utf-8") as fh:
                for ln in fh:
                    if not ln.strip() or ln.startswith("#"):
                        continue
                    parts = ln.rstrip("\n").split("\t")
                    if len(parts) >= 2 and parts[0].strip():
                        line = parts[0].strip()
                        title = parts[2].strip() if len(parts) >= 3 else ""
                        for key in (parts[1].strip(), os.path.splitext(parts[1].strip())[0], title):
                            if key:
                                lines[key] = (line, title)
        except OSError:
            pass
    dp = os.path.join(dirpath, "disc-structure.txt")
    if os.path.isfile(dp):
        try:
            with open(dp, encoding="utf-8") as fh:
                m = DISC_STRUCT_RE.match(fh.readline())
                if m:
                    struct = m.group(1).lower()
        except OSError:
            pass
    return struct, lines


def walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in M.JUNK_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def classify(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    # DVD navigation artifacts (.IFO/.BUP) are not media: `file` reports them as
    # video/x-ifo, so the mime fallback below would otherwise admit them and they
    # get placed as if they were content. The real content is the .VOB.
    if ext in (".ifo", ".bup"):
        return "other"
    # Audio is NEVER library content: a .flac/.mp3 beside a film is a leaked
    # bonus track, and the mime fallback below would call it video. Blacklist by
    # extension before the fallback, which exists for container video only.
    if ext in M.AUDIO_EXT:
        return "other"
    # Archives, ROMs and disc images are never content and must not be probed:
    # the mime fallback opens every unknown file, and a source root can hold tens
    # of thousands of these parts (multi-part RAR `.r00..`, TOSEC ROM sets). A
    # cheap extension test replaces a full read on the shared pool.
    if ext in M.NONMEDIA_EXT or M._RAR_PART_RE.search(path):
        return "other"
    if ext in M.VIDEO_EXT:
        return "video"
    if ext in M.SUB_EXT:
        return "subtitle"
    if ext in M.AUX_EXT:
        return "aux"
    # unknown extension: ask the actual container type (cheap, mislabels happen)
    mt = mime_type(path)
    if mt.startswith("video/") or mt in ("application/x-matroska", "application/vnd.rn-realmedia"):
        return "video"
    # NB: audio is deliberately NOT admitted here. A file whose real type is
    # audio and whose name we do not recognise is not library content.
    if mt in ("application/x-subrip", "text/x-ssa", "text/x-ass", "application/x-ass"):
        return "subtitle"
    return "other"


_MIME_CACHE: dict = {}


def mime_type(path: str) -> str:
    """Container type via file(1); cached. Empty string when undeterminable."""
    key = None
    try:
        st = os.stat(path)
        key = (path, st.st_size, int(st.st_mtime))
        if key in _MIME_CACHE:
            return _MIME_CACHE[key]
        out = subprocess.run(["file", "-b", "--mime-type", "--", path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        # stat or file(1) failed (missing file, timeout): no cache key to use
        out = ""
    if key is not None:
        _MIME_CACHE[key] = out
    return out


def folder_title(folder: str) -> tuple[str, int | None]:
    """Title + year from a title folder name."""
    g = M.parse_with_guessit(folder)
    title = g.get("title")
    year = M._int_year(g.get("year"))
    if not title:
        m = M.PAREN_YEAR.search(folder)
        year = M._int_year(m.group(1)) if m else None
        title = M.PAREN_YEAR.sub("", folder).strip(" .-_")
    return title or folder, year


def subtitle_lang(path: str):
    base = os.path.splitext(os.path.basename(path))[0]
    parts = base.split(".")
    if len(parts) >= 2 and re.fullmatch(r"[a-z]{2,3}", parts[-1].lower()):
        return parts[-1].lower()
    return None


MIN_PROBE_BYTES = 20 * 1024 * 1024   # below this, do not open the container


# ------------------------------------------------------------------ probe
def probe(path: str, store=None) -> dict:
    """Container/stream features. Cheap for files that cannot be a feature.

    A MediaInfo parse reads the whole container header set; for a folder of
    trailers, menus and JPEG-sized clips that is pure I/O. Files far too small
    to be a presentation are described from their size alone, without opening
    them — the caller can then ignore them on duration grounds.
    """
    from pymediainfo import MediaInfo
    if store is not None:
        try:
            s = os.stat(path)
            cached = store.get_probe(path, s.st_size, int(s.st_mtime))
            if cached is not None:
                # Entries probed before the TV 4:3 axis carry no width; re-probe
                # those once so the aspect is known. Non-video (too_small) entries
                # are kept — they have no aspect to find.
                if "width" in cached or cached.get("too_small"):
                    return cached
        except OSError:
            pass
    f = {"resolution": None, "audio_langs": [], "sub_langs": [],
         "source": None, "hdr": False, "bit_depth": None, "bitrate": None, "duration": None,
         "width": None, "height": None}
    try:
        size = os.path.getsize(path)
    except OSError:
        size = None
    if size is not None and size < MIN_PROBE_BYTES:
        # too small to be the presentation; record size and skip the parse
        f["too_small"] = True
        f["size"] = size
        if store is not None:
            try:
                st = os.stat(path)
                store.put_probe(path, st.st_size, int(st.st_mtime), f)
            except OSError:
                pass
        return f
    try:
        mi = MediaInfo.parse(path)
    except Exception:
        return f
    for t in mi.tracks:
        if t.track_type == "General":
            try:
                f["duration"] = float(t.duration) if t.duration is not None else None
            except (TypeError, ValueError):
                f["duration"] = None
            if t.overall_bit_rate:
                f["bitrate"] = int(t.overall_bit_rate)
        elif t.track_type == "Video":
            if t.height:
                f["resolution"] = f"{t.height}p"
                f["height"] = t.height
            if t.width:
                f["width"] = t.width
            if t.bit_depth:
                f["bit_depth"] = t.bit_depth
            if t.hdr_format:
                f["hdr"] = True
        elif t.track_type == "Audio":
            f["audio_langs"].append((t.language or "und").lower()[:2])
        elif t.track_type == "Text":
            f["sub_langs"].append((t.language or "und").lower()[:2])
    if store is not None:
        try:
            s = os.stat(path)
            store.put_probe(path, s.st_size, int(s.st_mtime), f)
        except OSError:
            pass
    return f


_RES_LABEL = {"2160p": "2160p UHD", "1080p": "1080p", "720p": "720p", "480p": "480p"}


def source_tag(feat: dict, path: str) -> str:
    p = M.parse_name(path)
    bits = []
    if p["resolution"]:
        bits.append(_RES_LABEL.get(p["resolution"], p["resolution"]))
    # guessit returns a LIST when a field is ambiguous ("TC.WEB-DL" -> both
    # 'Telecine' and 'Web'). str(list) leaked a Python repr into the tag
    # ("[1080p ['Telecine', 'Web']]"), so normalise to a joined string.
    src = M.parse_with_guessit(path).get("source") or ""
    if isinstance(src, (list, tuple)):
        src = " ".join(str(x) for x in src if x)
    src = str(src).replace("Ultra HD Blu-ray", "UHD").replace("Blu-ray", "BluRay")
    if src:
        bits.append(src)
    if feat.get("hdr"):
        bits.append("HDR")
    return " ".join(bits).strip()


# ------------------------------------------------------------------ plan build
def _eff_from(meta, title, year):
    return {"title": (meta or {}).get("title") or title,
            "year": M._int_year((meta or {}).get("year")) or M._int_year(year),
            "author": (meta or {}).get("author"),
            "original_language": (meta or {}).get("original_language")}


def resolve_identity(path, relparts, tmdb, cache, kind, root):
    """Resolve a single file to (effective_meta, tmdb_meta|None, season, episode).

    Folder layout is a hint, never authority: sources are irregular dark (loose
    files at a collection root, season packs, nested categories). We cascade from
    the nearest plausible title folder outward, then fall back to the filename.
    """
    par = M.parse_name(path)
    guess = M.parse_with_guessit(path)

    season = None
    season_show = None            # a combined "<Show> S03 ..." folder's show name
    for comp in reversed(relparts[:-1]):
        m = SEASON_RE.match(comp)
        if m:
            season = int(m.group(1)); break
        if comp.strip().lower() in ("specials", "special", "sp"):
            season = 0; break
        show, num = season_folder(comp)
        if show:
            # a season PACK folder: take the season AND the show name it carries,
            # so the series resolves to one show rather than one per season
            season, season_show = num, show
            break
    # The parsed episode is already authoritative — parse_name covers S01E01,
    # 1x02, "_Ep10v2_" and "- 13 -" forms. Gating it on TV_RE here discarded the
    # episode for every non-SxxExx naming, which merged a whole series into one
    # group and had its episodes ranked as alternatives to each other.
    episode = par["episode"]
    if season is None and par["season"] is not None and episode is not None:
        season = par["season"]

    # ancestor title folders: nearest first, prefer one carrying a year.
    # A combined season-pack folder ("Buffy The Vampire Slayer S03 1080p...") is
    # NOT a title: it supplies the season (above) and its embedded SHOW NAME. If
    # it were left in, each season would resolve to its own show.
    dirs = [d for d in reversed(relparts[:-1])
            if is_title_folder(d) and not is_structural(d) and season_folder(d)[0] is None]
    dirs.sort(key=lambda d: 0 if folder_title(d)[1] else 1)
    if season_show:
        # the season pack named the show; look it up ahead of the outer folders
        dirs.insert(0, season_show)
    for d in dirs:
        t, y = folder_title(d)
        key = (kind, t.lower(), y)
        if key not in cache and tmdb is not None:
            cache[key] = tmdb.identify(t, y, kind)
        meta = cache.get(key)
        if meta and meta.get("id"):
            return _eff_from(meta, t, y), meta, season, episode

    # loose file: identify from its own name
    t = guess.get("title") or par["stem"]
    y = par["year"]
    meta = None
    if tmdb is not None and is_title_folder(str(t)):
        key = (kind, str(t).lower(), y)
        if key not in cache:
            cache[key] = tmdb.identify(t, y, kind)
        meta = cache.get(key)
    return _eff_from(meta if (meta and meta.get("id")) else None, t, y), (meta if (meta and meta.get("id")) else None), season, episode


def parse_kind(path, relparts, root):
    """'tv' or 'movie' for a file.

    Episode evidence beats everything: a file that carries an episode number is
    an episode, whatever directory it sits in. The collections are irregular —
    anime series live under `FilmsCollec`, broadcast series under a plain
    directory name — so the root's name is the weakest signal and is only a
    fallback.
    """
    par = M.parse_name(path)
    if par["episode"] is not None:
        return "tv"
    # explicit season folder ("Season 01", "S2", "Specials") is episode evidence too
    if any(SEASON_RE.match(c) or c.strip().lower() in ("specials",) for c in relparts[:-1]):
        return "tv"
    # weakest signal last: the root's name
    if "series" in os.path.basename(root).lower():
        return "tv"
    return "movie"


def is_extra_file(path, relparts):
    if any(x.lower() in M.EXTRA_DIRS for x in relparts[:-1]):
        return True
    return M.parse_name(path)["is_extra"]


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def bd_root(relparts) -> str | None:
    """If this path sits in a Blu-ray AVCHD structure, return the disc root
    relative dir (the parent of BDMV); else None."""
    for i, comp in enumerate(relparts[:-1]):
        if comp.lower() == "bdmv":
            return os.sep.join(relparts[:i]) or "."
    return None


def dvd_root(relparts) -> str | None:
    """If this path belongs to a raw DVD (or an ISO image), return the disc root
    relative dir; else None. Raw DVD and ISO cannot be consumed by a reflink —
    the feature spans VOBs, and an ISO is an image — so they are skipped and
    reported to `to-remux.list` (makemkvcon commands) instead of being copied."""
    parts = list(relparts)
    tail = parts[-1].lower() if parts else ""
    # an ISO image: the disc is the containing directory
    if tail.endswith(".iso"):
        return os.sep.join(parts[:-1]) or "."
    # anything inside a VIDEO_TS tree (a .vob, or the .ifo/.bup we skip): the disc
    # is the parent of VIDEO_TS, so every file on the disc maps to one entry
    for i, comp in enumerate(parts[:-1]):
        if comp.lower() == "video_ts":
            return os.sep.join(parts[:i]) or "."
    # a standalone .vob not inside a VIDEO_TS structure
    if tail.endswith(".vob"):
        return os.sep.join(parts[:-1]) or "."
    return None


def bd_main_playlist(bdmv: str) -> tuple[str, int, int] | None:
    """(playlist, length_seconds, est_bytes) of the main title, via bdinfo-rs.

    bdinfo-rs resolves the disc's playlists authoritatively (branching included).
    Falls back to our own parser when the tool is unavailable.
    """
    if not bdmv:
        return None
    try:
        out = subprocess.run(["bdinfo-rs", bdmv, "-l", "--no-banner"],
                             capture_output=True, text=True, timeout=180).stdout
        best = None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[2].upper().endswith(".MPLS"):
                try:
                    hh, mm, ss = parts[3].split(":")
                    secs = int(hh) * 3600 + int(mm) * 60 + int(ss)
                    nbytes = int(parts[4].replace(",", ""))
                except ValueError:
                    continue
                # the main title is the playlist with the largest estimated size
                if best is None or nbytes > best[2]:
                    best = (parts[2], secs, nbytes)
        if best:
            return best
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    try:
        import mpls
        mp = mpls.main_playlist(bdmv)
        if mp is not None:
            return (mp.name, -1, str(mp.referenced_size))
    except Exception:
        pass
    return None


def plan_disc(items, eff, kind, season, episode, feats, place_feature=True):
    """Plan one Blu-ray disc: main STREAM segment as the feature, everything
    else (other segments) as extras. Disc segments never take part in normal
    quality selection — a raw .m2ts must not outrank an encoded file of the
    same title."""
    videos = [p for p, k, _r in items if k == "video"]
    subs = [p for p, k, _r in items if k == "subtitle"]
    ops, decision = [], None
    if not videos:
        return ops, decision
    # a raw STREAM segment is authentic but not a finished library file
    for p in videos:
        feats.setdefault(p, {})
        feats[p]["raw_disc"] = True

    main = max(videos, key=lambda p: _size(p))
    bdmv = None
    for p, _k, _r in items:
        parts = p.split(os.sep)
        for i, c in enumerate(parts):
            if c.lower() == "bdmv":
                bdmv = os.sep.join(parts[:i + 1])
                break
        if bdmv:
            break
    info = bd_main_playlist(bdmv)
    if info is not None:
        pl_name, pl_secs, pl_bytes = info
        try:
            same = abs(int(pl_bytes) - _size(main)) <= max(1_000_000, 0.01 * _size(main))
        except (TypeError, ValueError):
            same = False
        eff["_bd_playlist"] = pl_name
        eff["_bd_seconds"] = pl_secs
        if not same:
            eff["_bd_needs_remux"] = (
                f"main playlist {pl_name} ({pl_secs}s, {pl_bytes} B) does not match the "
                f"largest segment ({_size(main)} B) — likely multiple parts")
    else:
        big = sorted((_size(p) for p in videos), reverse=True)
        if len(big) > 1 and big[1] >= 0.4 * big[0]:
            eff["_bd_needs_remux"] = f"{len([s for s in big if s >= 0.4 * big[0]])} large segments"

    # feature: the largest segment; a raw m2ts keeps its extension
    v = M.parse_name(main)
    tag = M._dedup_tokens(source_tag(feats.get(main, {}), main))
    title = M.sanitize(eff["title"])
    year = eff["year"]
    cat = M.category(kind, eff, None, feats.get(main, {}).get("duration"))
    eff["_category"] = cat
    name = f"{title}{f' ({year})' if year else ''}"
    if tag:
        name += f" [{tag}]"
    dest_dir = os.path.join(M.CINEMA_BASE, cat, M.media_dir(title, year, eff.get("author"), kind))
    if place_feature:
        ops.append({"src": main, "dest_dir": dest_dir, "name": M.sanitize(name) + v["ext"], "kind": "movie"})
        decision = {"group": f"{title}|disc", "versions": 1, "kept": main, "dropped": [], "orig_audio_ok": True}
        if eff.get("_bd_needs_remux"):
            ops.append({"kind": "marker", "dest_dir": dest_dir, "name": "NEEDS_REMUX.txt", "src": main,
                        "text": f"Blu-ray needing a remux: {eff['_bd_needs_remux']}.\n"
                                f"Placed the largest segment only ({os.path.basename(main)}).\n"
                                f"Remux the main .mpls playlist for the complete feature.\n"})
    else:
        # an encoded file of this title already wins; the disc segment joins the
        # extras rather than sitting beside it in the library
        eff["_bd_superseded"] = True

    # other segments and disc subtitles -> _BONUS_
    y = eff["year"]
    bonus_dir = os.path.join(M.CINEMA_BASE, M.BONUS_ROOT,
                             M.sanitize(f"{title}{f' ({y})' if y else ''}"))
    for p in videos:
        if p == main and place_feature:
            continue
        ops.append({"src": p, "dest_dir": bonus_dir,
                    "name": M.sanitize(os.path.splitext(os.path.basename(p))[0]) + M.parse_name(p)["ext"],
                    "kind": "bonus"})
    for p in subs:
        ops.append({"src": p, "dest_dir": bonus_dir,
                    "name": M.sanitize(os.path.splitext(os.path.basename(p))[0]) + M.parse_name(p)["ext"],
                    "kind": "bonus"})
    return ops, decision


def plan_group(items, eff, meta, kind, season, episode, feats, ovr=None, tmdb=None):
    """items: list of (path, kind, relparts). feats: {video_path: features}.
    Chooses one best video, routes subs. Pure given feats."""
    # Disc segments are planned separately and never compete with encoded files.
    disc_items = [it for it in items if bd_root(it[2]) is not None]
    lib_items = [it for it in items if bd_root(it[2]) is None]
    if disc_items:
        lib_videos = [p for p, k, _r in lib_items if k == "video"]
        ops, decision = plan_disc(disc_items, eff, kind, season, episode, feats,
                                  place_feature=not lib_videos)
        if lib_items:
            o2, d2 = plan_group(lib_items, eff, meta, kind, season, episode, feats, ovr=ovr, tmdb=tmdb)
            ops += o2
            decision = d2 or decision
        return ops, decision

    videos = [p for p, k, _r in items if k == "video"]
    subs = [p for p, k, _r in items if k == "subtitle"]

    # Drop clips too short to be the feature (logos, menus, trailers) whenever a
    # longer candidate exists. Under two minutes is never the presentation.
    if len(videos) > 1:
        def _dur(p):
            try:
                return float(feats.get(p, {}).get("duration") or 0)
            except (TypeError, ValueError):
                return 0.0
        longs = [p for p in videos if _dur(p) >= 120_000]
        if longs:
            videos = longs

    ops = []
    decision = None
    chosen = None
    keepers = []
    if videos:
        for p in videos:
            feats.setdefault(p, {})
            feats[p]["tag"] = source_tag(feats[p], p)
        orig = eff["original_language"]
        ok = [p for p in videos if M.has_original_audio(feats[p], orig)]
        # NOT `ok or videos`: filtering to original-audio first would discard a
        # dub-only release entirely. Instead every version is grouped by
        # presentation and the score (whose first axis is original-audio) picks
        # within a family, so a dub-only release survives as its own language
        # family while an original-audio encode wins its own family.
        pool = videos

        def _score(p):
            return M.quality_score(feats[p], p, orig, kind)

        # Keep one file PER PRESENTATION, not one file per group. A presentation
        # is (aspect class, audio-language set): a 4:3 open-matte and a 16:9
        # pan-and-scan of the same show are different works to the viewer, and so
        # are a dub-only and an original-audio release — each must survive. Within
        # one presentation the versions compete purely on quality, where
        # resolution leads, so a 4K beats a 1080p and only the 4K is kept.
        #
        # Grouping is by COMPATIBILITY, not exact equality: a file whose audio
        # languages could not be probed (empty set) must not be split off from an
        # otherwise identical file — that would mint a phantom family and keep a
        # redundant copy. Same aspect + (equal languages OR either side unknown)
        # means one presentation.
        families = []                      # list of (aspect, langs, [paths])
        for p in pool:
            asp = M.aspect_class(feats[p])
            lng = M.audio_languages(feats[p])
            for fam in families:
                if fam[0] != asp:
                    continue
                if not fam[1] or not lng or fam[1] == lng:
                    fam[2].append(p)
                    if not fam[1]:
                        fam[1] = lng        # adopt a known language set
                    break
            else:
                families.append([asp, lng, [p]])
        keepers = [max(fam[2], key=_score) for fam in families]
        chosen = max(keepers, key=_score)          # the "primary" for decision/messages
        dropped = [p for p in videos if p not in keepers]
        decision = {"group": f"{eff['title']}|S{season}|E{episode}|{kind}",
                    "versions": len(videos), "kept": chosen,
                    "kept_all": keepers, "presentations": len(families),
                    "dropped": dropped, "orig_audio_ok": bool(ok)}
        # record every rejected version (one per presentation family, plus any
        # inter-family loser that still lost on quality within its own family):
        # the ranking goes into the store's `alternative` table, NOT written
        # beside the file (a stray .txt is not library content).
        eff["_alternatives"] = [
            {"src": p, "score": _score(p), "size": _size(p),
             "reason": "lower-ranked version"}
            for p in sorted(dropped, key=_score, reverse=True)
        ]

    is_anime = eff["original_language"] == "ja"
    dur = feats.get(chosen, {}).get("duration") if chosen else None
    hint = os.sep.join(items[0][2]) if items else ""
    eff["_category"] = M.category(kind, eff, meta, dur, hint, n_videos=len(videos))
    # A forced category beats the heuristic: the point of the override file is
    # that the caller knows something the filename cannot say.
    if ovr:
        root_hint = (items[0][3] if items and len(items[0]) > 3 else None)
        forced_cat = ovr.category_for(chosen or (items[0][0] if items else ""), root_hint)
        if forced_cat:
            eff["_category"] = forced_cat
            eff["_forced_category"] = True
    # Jellyfin's episode name needs the episode TITLE, which lives at a separate
    # endpoint from the series identification. Resolved once per group and used
    # by the video AND its sidecar subtitles, so their stems agree.
    feat = {"tag": feats[chosen]["tag"] if chosen else ""}
    if kind == "tv" and isinstance(episode, int) and meta and meta.get("id") and tmdb is not None:
        try:
            ep_name, _ep_year = tmdb.episode_title(int(meta["id"]), season or 1, episode)
            if ep_name:
                feat["episode_title"] = ep_name
        except Exception:
            pass
    # Emit one op per kept presentation. With a single family this is exactly the
    # old behaviour. When a group carries two, the names must not collide (Jellyfin
    # mints a __dup otherwise), so every presentation after the first takes its
    # family label as an edition tag: "Title (Year) [16:9]" / "... [4:3]" and, when
    # the aspect class is shared but the audio differs, the language instead.
    fam_labels = {}
    if len(keepers) > 1:
        aspects = [M.aspect_class(feats[p]) for p in keepers]
        for p in keepers:
            fk = M.presentation_key(feats[p])
            if aspects.count(M.aspect_class(feats[p])) > 1:
                langs = sorted(fk[1])
                fam_labels[p] = "/".join(langs).upper() if langs else M.aspect_class(feats[p])
            else:
                fam_labels[p] = M.aspect_class(feats[p])
    alts = eff.get("_alternatives") or []
    for p in keepers:
        # Per-file feature dict: the quality tag must describe THIS file, not the
        # group's primary — otherwise the 16:9 encode is labelled with the DVD's
        # tag ([DVD]) and Jellyfin sees two identical tags.
        f = {"tag": feats[p]["tag"] if p in feats else ""}
        if feat.get("episode_title"):
            f["episode_title"] = feat["episode_title"]
        label = fam_labels.get(p)
        r = M.route(p, eff, f, videos, from_filename=False, season=season, episode=episode)
        if label:
            # apply the family label as an edition so names differ across families
            base, ext = os.path.splitext(r["name"])
            r = dict(r); r["name"] = f"{base} [{label}]{ext}"
        # Alternatives are NOT written beside the file: the rejection ranking is
        # provenance, not library content, and a stray .txt confuses Jellyfin.
        # It is kept on the op so the store records it, and can be rendered later.
        ops.append({"src": p, "dest_dir": r["dest_dir"], "name": r["name"],
                    "kind": r["kind"], "meta": eff, "alternatives": alts})

    for p in subs:
        lang = subtitle_lang(p)
        if not M.subtitle_keep(lang, is_anime):
            continue
        r = M.route(p, eff, {"language": lang, "episode_title": feat.get("episode_title")},
                    videos, from_filename=False, season=season, episode=episode)
        ops.append({"src": p, "dest_dir": r["dest_dir"], "name": r["name"], "kind": "subtitle"})

    return ops, decision


# ------------------------------------------------------------------ grouping
#
# Groups were keyed by an ad-hoc nested tuple: ((kind, "id", tmdb_id), season,
# episode). It worked, but the same key was built in four places with different
# arities — a literal ("movie", "id", id) in the disc branch, `kind` elsewhere —
# so a mistake would silently split or merge groups rather than raise. These
# dataclasses make the identity say what it means, and make the two reference
# kinds (TMDB id, or title+year) explicit and mutually exclusive.
from dataclasses import dataclass, field as _dc_field


@dataclass(frozen=True)
class MediaRef:
    """What a group claims to be: a TMDB id, or a title (+year) guess.

    Exactly one of `tmdb_id` / `title` is meaningful; `key()` picks whichever is
    set, and the `kind` is part of the identity so a film and a series of the
    same name never merge.
    """
    kind: str                      # "movie" | "tv"
    tmdb_id: int | None = None
    title: str | None = None
    year: int | None = None
    forced: bool = False           # pinned by an override file

    def key(self) -> tuple:
        if self.tmdb_id is not None:
            return (self.kind, "id", self.tmdb_id)
        if self.forced:
            return (self.kind, "ovr", self.title or "")
        return (self.kind, "t", self.title or "", self.year)


@dataclass(frozen=True)
class Instalment:
    """Which instalment of the referenced work: season, episode.

    For an anthology (a flat folder of distinct works) there is no instalment,
    so both are None.
    """
    season: int | None = None
    episode: int | None = None


@dataclass
class Group:
    """One logical media item: its identity, and the files that belong to it."""
    ref: MediaRef
    instalment: Instalment
    kind: str
    eff: dict
    meta: dict | None = None
    items: list = _dc_field(default_factory=list)

    @property
    def key(self) -> tuple:
        return (self.ref.key(), self.instalment.season, self.instalment.episode)


def collect_units(roots, tmdb, cache, verbose=True, max_groups=None, ovr=None):
    """Walk the roots and return (groups, bonuses, files_seen).

    A group holds one logical media item's files; bonuses are routed separately.

    max_groups stops the walk as soon as that many groups have been collected.
    The walk is where identity resolution happens, so a limit applied later
    would still pay for every lookup in the corpus; stopping here is what makes
    a small trial actually small.
    """
    groups: dict[tuple, Group] = {}
    bonuses: list = []
    files_seen = 0
    ids_done = 0
    sibling_meta_cache_index: dict = {}
    # files pinned to a title by an override share one group key, whatever else
    # the heuristics decide about them; they also sidestep the sibling/episode
    # readings, which is the point — a film sitting in a series folder must not
    # be absorbed into the series.
    pinned: dict[str, list] = {}
    pinned_meta: dict[str, dict] = {}
    # raw DVD video discs: a reflink of a title-set cannot be right (the feature
    # is split across VOBs), so they are not copied — the disc root is recorded
    # so a makemkv command can be written to `to-remux.list`.
    discs_seen: dict[str, str] = {}     # disc abs dir -> rel dir for the title

    def add_item(ref: MediaRef, inst: Instalment, eff: dict, meta: dict | None,
                 kind: str, item: tuple):
        """Accumulate a file under (identity, instalment).

        One place that knows how a group is keyed, so the four call sites below
        cannot drift in arity or in which kind they pass.
        """
        g = groups.get((ref.key(), inst.season, inst.episode))
        if g is None:
            g = Group(ref=ref, instalment=inst, kind=kind, eff=eff, meta=meta)
            groups[(ref.key(), inst.season, inst.episode)] = g
        g.items.append(item)
        return g

    for root in roots:
        if max_groups is not None and len(groups) >= max_groups:
            break
        if not os.path.isdir(root):
            if verbose:
                print(f"  ! skip missing root {root}", file=sys.stderr)
            continue
        # precompute, per folder: is it an anthology (many distinct works) or a
        # numbered run of episodes (one work)? A shared *token sequence* is the
        # evidence, and what follows it decides — see siblings.classify_folder.
        sibling_dirs: dict[str, str] = {}
        episode_dirs: dict[str, dict[str, tuple]] = {}
        disc_lines: dict[str, dict] = {}      # dir_rel -> {name/stem -> release line}
        disc_struct: dict[str, str] = {}      # dir_rel -> anthology|series|collection|single
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in M.JUNK_DIRS]
            vids = [f for f in filenames if os.path.splitext(f)[1].lower() in
                    (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts")]
            reading, payload = siblings.classify_folder(vids)
            rel = os.path.relpath(dirpath, root)
            key = rel if rel != "." else ""
            if reading == "anthology":
                sibling_dirs[key] = payload
            elif reading == "series":
                episode_dirs[key] = payload
            # the vision stage's disc verdict, if the worker has run here
            st, lines = read_disc_sidecars(dirpath)
            if st:
                disc_struct[key] = st
            if lines:
                disc_lines[key] = lines
        for p in walk(root):
            if max_groups is not None and len(groups) >= max_groups:
                break
            relparts = os.path.relpath(p, root).split(os.sep)
            # raw DVD / ISO: cannot be reflinked; record for the remux list and skip
            dvdr = dvd_root(relparts)
            if dvdr is not None:
                discs_seen.setdefault(os.path.normpath(os.path.join(root, dvdr)), root)
                continue
            k = classify(p)
            if k == "other":
                continue
            files_seen += 1
            if verbose and files_seen % 250 == 0:
                print(f"  … {files_seen} files, {ids_done} titles identified", flush=True)
            forced = ovr.title_for(p, root) if ovr else None
            if forced is not None:
                if k == "video":
                    if forced not in pinned_meta:
                        # The key may carry a year ("Who Killed Who (1943)").
                        # Pass it as the year FILTER, not just in the string: a
                        # generic title otherwise matches a same-named recent
                        # film on similarity alone ("The Cuckoo Clock" -> the
                        # 2024 short) and the key's year is ignored.
                        fy = M._int_year(re.search(r"\((\d{4})\)", forced).group(1)) \
                            if re.search(r"\((\d{4})\)", forced) else None
                        ftitle = re.sub(r"\s*\(\d{4}\)\s*$", "", forced)
                        pinned_meta[forced] = (tmdb.identify(ftitle, fy, "movie")
                                               if tmdb is not None else None)
                    m = pinned_meta[forced]
                    eff = _eff_from(m if (m and m.get("id")) else None, forced, None)
                    eff["_forced"] = True
                    ids_done += 1
                    ref = MediaRef("movie", tmdb_id=(m or {}).get("id"), title=forced, forced=True)
                    add_item(ref, Instalment(), eff, m if (m and m.get("id")) else None,
                             "movie", (p, k, relparts))
                    pinned.setdefault(forced, []).append(p)
                    continue
                # a forced non-video (subtitle/sidecar) is filed with its title
                ref = MediaRef("movie", title=forced, forced=True)
                add_item(ref, Instalment(),
                         {"title": forced, "year": None, "author": None,
                          "original_language": None, "_forced": True},
                         None, "movie", (p, k, relparts))
                continue

            # --- sibling pack: many distinct works in one flat folder (an
            # anthology of shorts, a pile of episodes). Each file is its OWN
            # title, keyed by its remainder; never ranked against its siblings.
            dir_rel = os.sep.join(relparts[:-1])
            # --- disc sidecars: this folder is a disc the vision stage has read
            # (a verdict, and/or per-file release lines). Two things follow:
            #   * a file tagged with a release line groups under it (the family
            #     signal a rename destroys for the filename heuristic);
            #   * EVERY video here resolves from its OWN name, never the folder.
            # The second is the important one: on an anthology/collection the
            # folder is a container ("TEX_AVERY_D3"), and letting the ancestor
            # cascade name files after it resolved the whole disc to one title
            # ("The Compleat Tex Avery") — every file collapsed onto a single
            # destination.
            if (dir_rel in disc_struct or dir_rel in disc_lines) and k == "video":
                fkey = os.path.basename(p)
                hit = (disc_lines.get(dir_rel, {}).get(fkey)
                       or disc_lines.get(dir_rel, {}).get(os.path.splitext(fkey)[0]))
                rel_line = hit[0] if hit else ""
                own = (hit[1] if hit and hit[1] else os.path.splitext(fkey)[0])
                ck = ("movie", own.lower(), None)
                if tmdb is not None and own not in sibling_meta_cache_index:
                    sibling_meta_cache_index[own] = tmdb.identify(own, None, "movie")
                meta = sibling_meta_cache_index.get(own)
                ids_done += 1
                eff = _eff_from(meta if (meta and meta.get("id")) else None, own, None)
                # The disc verdict decides how much the release line means:
                #   collection — every work shares ONE banner, so the banner IS
                #     the grouping; it overrides the author level.
                #   anthology — banners differ; the release line is only a
                #     fallback author when the metadata supplies none.
                if rel_line and disc_struct.get(dir_rel) == "collection":
                    eff["author"] = rel_line
                else:
                    eff["author"] = eff.get("author") or rel_line or None
                if rel_line:
                    eff["_release_line"] = rel_line
                ref = MediaRef("movie", tmdb_id=(meta or {}).get("id"),
                               title=M.sanitize(own.lower()))
                add_item(ref, Instalment(), eff,
                         meta if (meta and meta.get("id")) else None,
                         "movie", (p, k, relparts))
                continue
            if dir_rel in sibling_dirs:
                prefix = sibling_dirs[dir_rel]
                own = siblings.episode_title(os.path.basename(p), prefix)
                if k == "video":
                    ck = ("movie", own.lower(), None)
                    if tmdb is not None and own not in sibling_meta_cache_index:
                        sibling_meta_cache_index[own] = tmdb.identify(own, None, "movie")
                    meta = sibling_meta_cache_index.get(own)
                    ids_done += 1
                    eff = _eff_from(meta if (meta and meta.get("id")) else None, own, None)
                    eff["_sibling_of"] = prefix
                    ref = MediaRef("movie", tmdb_id=(meta or {}).get("id"),
                                   title=M.sanitize(own.lower()))
                    add_item(ref, Instalment(), eff,
                             meta if (meta and meta.get("id")) else None,
                             "movie", (p, k, relparts))
                    continue

            # --- Blu-ray disc: identity from the disc folder, merged with any
            # standalone copy of the same title (bd_root marks disc items) ---
            drel = bd_root(relparts)
            if drel is not None:
                disc_abs = os.path.normpath(os.path.join(root, drel))
                disc_folder = os.path.basename(disc_abs) or os.path.basename(root)
                dmeta = None
                t, y = folder_title(disc_folder)
                if tmdb is not None and is_title_folder(t):
                    ck = ("movie", t.lower(), y)
                    if ck not in cache:
                        cache[ck] = tmdb.identify(t, y, "movie")
                    dmeta = cache.get(ck)
                ids_done += 1
                eff = _eff_from(dmeta if (dmeta and dmeta.get("id")) else None, t, y)
                # a Blu-ray disc is always a film: the disc branch used to hard-code
                # "movie" here while other branches passed `kind`, which is exactly
                # the inconsistency the dataclasses remove
                ref = MediaRef("movie", tmdb_id=(dmeta or {}).get("id"),
                               title=M.sanitize(eff["title"].lower()), year=eff["year"])
                add_item(ref, Instalment(), eff,
                         dmeta if (dmeta and dmeta.get("id")) else None,
                         "movie", (p, k, relparts))
                continue

            if is_extra_file(p, relparts):
                kind = parse_kind(p, relparts, root)
                eff, meta, _s, _e = resolve_identity(p, relparts, tmdb, cache, kind, root)
                ids_done += 1
                y = eff["year"]
                bonus_dir = os.path.join(M.CINEMA_BASE, M.BONUS_ROOT,
                                         M.sanitize(f"{eff['title']}{f' ({y})' if y else ''}"))
                bonuses.append({"src": p, "dest_dir": bonus_dir,
                                "name": M.sanitize(os.path.splitext(os.path.basename(p))[0]) + M.parse_name(p)["ext"],
                                "kind": "bonus"})
                continue

            kind = parse_kind(p, relparts, root)
            # A numbered folder is one work's episodes even when neither the
            # filename nor the path names a season. The folder structure is the
            # evidence: a shared prefix followed by distinct numbers. Decided
            # before the lookup, so TMDB is queried as a series — querying as a
            # film would return a film id that then labels the episodes as tv.
            folder_eps = episode_dirs.get(dir_rel)
            if folder_eps is not None:
                kind = "tv"
            eff, meta, season, episode = resolve_identity(p, relparts, tmdb, cache, kind, root)
            ids_done += 1
            if folder_eps is not None:
                # The per-file name is often unusable as a title: anime naming
                # ("Neon Genesis Evangelion - 03 - 720p HEVC") yields the whole
                # stem, so every episode became its own title and its own group,
                # keeping one episode and discarding the rest. The folder's
                # shared token run IS the series title, so use it — that is what
                # puts all 26 episodes in one group.
                fam = siblings.series_family(folder_eps)
                # The family is a LOWER bound on the title, derived from file
                # names alone; it must not displace a real identification. When
                # resolve_identity found a provider id, that name is
                # canonical and language-consistent ("Buffy the Vampire
                # Slayer"), whereas the shared prefix is whatever the release
                # wrote ("Buffy contre les vampires") — keeping the latter put
                # the same show under two spellings. The family is used only when
                # nothing was identified, which is exactly the anime case it was
                # written for.
                if fam and not (meta and meta.get("id")):
                    eff["title"] = fam
                    eff["_family"] = fam
                    eff["author"] = None      # series are flat: no author level
                if fam:
                    episode_dirs.setdefault(dir_rel, folder_eps)
                # the folder reading supplies (season, episode); a "./Saison N"
                # component or the filename may have supplied either already, and
                # the folder evidence wins because it is uniform for the folder
                pair = folder_eps.get(os.path.basename(p))
                if pair is not None:
                    fseason, fepisode = pair
                    if fepisode is not None:
                        episode = fepisode
                    if fseason is not None:
                        season = fseason
                if season is None:
                    season = 1
            ref = MediaRef(kind, tmdb_id=(meta or {}).get("id"),
                           title=M.sanitize(eff["title"].lower()), year=eff["year"])
            epkey = episode if episode is not None else ("F", os.path.basename(p))
            add_item(ref, Instalment(season, epkey), eff, meta, kind, (p, k, relparts))
    return groups, bonuses, files_seen, discs_seen


def write_remux_list(discs: dict, path: str, remux_root: str) -> int:
    """Write suggested makemkvcon commands for the raw DVD/ISO discs found.

    Suggest only: the disc is never copied. `makemkvcon mkv` **remuxes** — it
    copies the elementary streams verbatim into an MKV, no re-encode — one MKV
    per title, which a reflink cannot do (the feature spans VOBs). Each disc
    extracts into its own folder under `remux_root`, so a later pass can pick the
    main title per disc. Sorted for a stable file between runs.
    """
    lines = ["# Raw DVD/ISO sources: a reflink cannot represent them (the feature spans",
             "# VOBs, and an ISO is an image), so they were skipped. These commands",
             "# remux each disc, title by title, into an MKV (no re-encode). Run them,",
             "# then integrate the main title per disc folder; the worker does not",
             "# consume this tree on its own. makemkvcon needs the output directory to",
             "# exist, so it has been created for each disc below.", ""]
    for disc in sorted(discs):
        base = os.path.basename(disc)
        # an ISO's folder name should not carry the .ISO suffix
        if base.lower().endswith(".iso"):
            base = base[:-4]
        title = M.sanitize(base) or "disc"
        outdir = os.path.join(remux_root, title)
        try:
            os.makedirs(outdir, exist_ok=True)   # makemkvcon will not create it
        except OSError:
            pass
        # `file:` accepts both a disc folder and an ISO image; `disc:0` would
        # target a physical drive. Streams are copied verbatim (a remux).
        lines.append(f'makemkvcon mkv file:"{disc}" all "{outdir}"')
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        return 0
    return len(discs)


def build_plan(roots, tmdb=None, verbose=True, store=None, probeworkers=3, max_groups=None, ovr=None):
    cache: dict = {}
    groups, bonuses, files_seen, discs = collect_units(roots, tmdb, cache, verbose, max_groups=max_groups, ovr=ovr)

    # probe all candidate videos through a bounded pool (cached by stat in store)
    vids = [p for g in groups.values() for p, k, _r in g.items if k == "video"]
    probes: dict = {}
    with ThreadPoolExecutor(max_workers=probeworkers) as ex:
        for p, f in zip(vids, ex.map(lambda q: probe(q, store), vids)):
            probes[p] = f

    ops, decisions = [], []
    for g in groups.values():
        feats = {p: probes[p] for p, k, _r in g.items if k == "video" and p in probes}
        o, d = plan_group(g.items, g.eff, g.meta, g.kind,
                          g.instalment.season, g.instalment.episode, feats, ovr=ovr, tmdb=tmdb)
        ops.extend(o)
        if d:
            decisions.append(d)
    ops.extend(bonuses)
    return ops, decisions, files_seen, discs


def stream_apply(roots, dest_root, tmdb=None, store=None, probeworkers=3,
                 copyworkers=1, dry=False, verbose=True, max_groups=None,
                 max_clones=None, ovr=None, rename=False, remux_path=None):
    """Plan-and-copy one group at a time, overlapping probe / identify / copy.

    Copies are **serialized**: only one reflink is ever in flight, with a short
    settle gap after each, because concurrent block-clones put disproportionate
    pressure on the pool's transaction groups. Probing stays concurrent.

    Memory is bounded by the *largest group*, not the corpus: each group's videos
    are probed just before it is planned, and their feature dicts are dropped as
    soon as the decision is made. Probing everything up front (the earlier
    behaviour) retained a MediaInfo feature tree per video for the whole run.
    """
    cache: dict = {}
    budget = CloneBudget(max_clones)
    # read the collapse map once for the whole run
    shared = store.dest_use_counts() if (store is not None and not dry) else {}
    groups, bonuses, _n, discs = collect_units(roots, tmdb, cache, verbose, max_groups=max_groups, ovr=ovr)
    if discs:
        path = remux_path or os.path.join(dest_root, "to-remux.list")
        remux_root = os.path.join(dest_root, "Remuxes")
        if dry:
            if verbose:
                print(f"  {len(discs)} raw DVD/ISO source(s) would -> {path}")
        else:
            n = write_remux_list(discs, path, remux_root)
            if verbose:
                print(f"  {n} raw DVD/ISO source(s) -> {path}")
    # deterministic order; groups sharing an identity (same title/episode across
    # roots) stay adjacent so version selection and placement are contiguous
    order = [g for _k, g in sorted(groups.items(), key=lambda kv: str(kv[0]))]

    def do_group(g, feats):
        try:
            ops, dec = plan_group(g.items, g.eff, g.meta, g.kind,
                                  g.instalment.season, g.instalment.episode, feats, ovr=ovr, tmdb=tmdb)
            res = apply_plan(ops, dest_root, roots=roots, dry=dry, store=store,
                             budget=budget, shared_dests=shared, rename=rename,
                             skip_kinds=() if STAGE_BONUSES else ("bonus",))
            return ops, dec, res
        finally:
            feats.clear()          # release probe results with the group

    done = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=probeworkers) as pex, \
         ThreadPoolExecutor(max_workers=copyworkers) as cex:
        futures = []
        pending = []               # bounded: only a few groups' probes in flight

        def drain(gg, futs):
            futures.append(cex.submit(do_group, gg, {p: f.result() for p, f in zip(
                [q for q, k, _r in gg.items if k == "video"], futs)}))

        for g in order:
            # Stop scheduling once the clone budget is spent: a systematic halt
            # rather than running the rest of the plan and discarding it.
            if budget.exhausted():
                stopped = True
                break
            need = [p for p, k, _r in g.items if k == "video"]
            if not need:
                futures.append(cex.submit(do_group, g, {}))
                continue
            # probe just this group's videos, ahead of at most `probeworkers`
            # other groups, so the pool stays full without buffering the corpus
            pending.append((g, [pex.submit(probe, p, store) for p in need]))
            if len(pending) >= probeworkers * 2:
                gg, futs = pending.pop(0)
                drain(gg, futs)
        for gg, futs in pending:
            if budget.exhausted():
                stopped = True
                break
            drain(gg, futs)
        for f in futures:
            _o, d, r = f.result()
            done += 1
            if verbose and d:
                tag = "" if not any(str(x.get("status", "")).startswith("stopped")
                                    for x in r) else "  [budget stop]"
                print(f"  [{done}/{len(order)}] {d['group'][:44]:44} -> {os.path.basename(d['kept'])}{tag}")
    # bonuses last (need identity only, no probe); only if budget remains
    if bonuses and STAGE_BONUSES and not budget.exhausted():
        apply_plan(bonuses, dest_root, roots=roots, dry=dry, store=store,
                   budget=budget, shared_dests=shared, rename=rename)
    elif bonuses and STAGE_BONUSES:
        stopped = True
    if verbose:
        state = f"STOPPED early: clone budget {budget.done}/{budget.limit} reached" \
            if (budget.limit is not None and budget.exhausted()) else \
            f"complete: {budget.done} reflinks" if budget.limit is None else \
            f"complete within budget: {budget.done}/{budget.limit} reflinks"
        print(f"  {state}")
    return done


# ------------------------------------------------------------------ apply
def _reflink_once(src: str, dst: str, dry: bool = False, recursive: bool = False):
    if dry:
        return "dry"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # hard requirement: never fall back to a full copy.
    # No -p: this dataset is aclmode=restricted, where preserving mode/ownership
    # fails with EPERM and makes cp exit non-zero even though the clone succeeded.
    argv = ["cp", "--reflink=always"]
    if recursive:
        argv.append("-r")
    argv += ["--", src, dst]
    for attempt in range(2):
        p = subprocess.run(argv, capture_output=True, text=True)
        if p.returncode == 0:
            return "ok"
        # zfs_bclone_wait_dirty=0: cloning source data still dirty in cache fails
        # with EAGAIN ("Resource temporarily unavailable").
        #
        # Flush the *destination*, never the whole pool: a global `sync` waits on
        # every dataset's dirty data, which on a busy shared pool is minutes of
        # unrelated work, and this is the hot path. fsync on the file we are
        # about to write forces the source's blocks out of the import cache for
        # this stream without dragging in the rest of the pool.
        if attempt == 0 and ("temporarily unavailable" in p.stderr.lower()
                             or "resource temporarily" in p.stderr.lower()):
            try:
                fd = os.open(os.path.dirname(dst), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
            continue
        raise subprocess.CalledProcessError(p.returncode, p.args, p.stdout, p.stderr)
    raise subprocess.CalledProcessError(1, argv, "", "reflink retry exhausted")


def plan_collisions(ops, dest_root):
    """Destinations targeted by more than one op in a single plan.

    These mean two groups should have merged; applying them in sequence would
    fight over one path. Reported so the caller can skip rather than thrash.
    """
    seen: dict[str, list] = {}
    for op in ops:
        if op.get("kind") == "marker":
            continue
        key = os.path.join(op["dest_dir"], op["name"])
        seen.setdefault(key, []).append(op["src"])
    return {k: v for k, v in seen.items() if len(v) > 1}


# A reflink is cheap in bytes but not in filesystem pressure: each clone updates
# the block-reference table and must be committed by a transaction group. Only
# one clone may be in flight at a time, and a short pause between them lets the
# previous txg settle instead of piling work into the next one.
#
# The lock is a *file* lock, not a threading one, so it also serializes across
# processes — the CLI apply and the web UI are separate programs and must not
# clone simultaneously.
_CLONE_SETTLE_S = None            # resolved from config on first use
_CLONE_LOCK_HANDLE = None         # kept open for the process lifetime


def _clone_settle() -> float:
    global _CLONE_SETTLE_S
    if _CLONE_SETTLE_S is None:
        _CLONE_SETTLE_S = C.clone_settle_seconds()
    return _CLONE_SETTLE_S


@contextlib.contextmanager
def _clone_lock():
    """Exclusive advisory lock held for the duration of one clone."""
    global _CLONE_LOCK_HANDLE
    if _CLONE_LOCK_HANDLE is None:
        try:
            _CLONE_LOCK_HANDLE = open(C.clone_lock_path(), "a+")
        except OSError:
            _CLONE_LOCK_HANDLE = None
    if _CLONE_LOCK_HANDLE is None:      # no lock possible: proceed unguarded
        yield
        return
    try:
        import fcntl
        fcntl.flock(_CLONE_LOCK_HANDLE.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        yield
        return
    try:
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(_CLONE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass


def clone_serialized(src: str, dst: str, dry: bool = False, recursive: bool = False) -> None:
    """One `cp --reflink=always` at a time, with a settle gap afterwards.

    The lock is cross-process, so this is also safe against the web UI cloning
    at the same moment.
    """
    if dry:
        return
    with _clone_lock():
        _reflink_once(src, dst, recursive=recursive)
        delay = _clone_settle()
        if delay > 0:
            time.sleep(delay)


def delete_reflink(path: str) -> None:
    """Delete a file, serialized and paced like a clone.

    Removing a reflinked file is not a metadata-only unlink: its block-reference
    entry must be dropped and the transaction group committed, which on this pool
    is slow and can queue behind other work. So it takes the same cross-process
    lock and settle gap as `clone_serialized`, instead of being treated as free.
    """
    with _clone_lock():
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        delay = _clone_settle()
        if delay > 0:
            time.sleep(delay)


def delete_tree_paced(root: str, verbose: bool = True, budget=None) -> int:
    """Remove a tree, one file at a time, each behind the delete gate.

    Used to purge stale library output: those files are reflinks, so a bulk
    `rm -rf` would drop a large batch of block-reference entries at once. Pacing
    keeps the txg pressure flat, which is the same reason clones are serialized.
    """
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            delete_reflink(os.path.join(dirpath, name))
            removed += 1
            if budget is not None:
                budget.note()
            if verbose and removed % 200 == 0:
                print(f"  … removed {removed}", flush=True)
        try:
            os.rmdir(dirpath)
        except OSError:
            pass                      # non-empty or gone: leave it
    try:
        os.rmdir(root)
    except OSError:
        pass
    return removed


def apply_plan(ops, dest_root, roots=None, dry=True, store=None, rename=False,
               budget=None, shared_dests=None, skip_kinds=()):
    """Apply a plan. `budget` is a CloneBudget capping successful reflinks.

    The stop is a deliberate, clean halt between clones: skipped ops do not
    count, and no clone is ever interrupted mid-write. Re-running resumes,
    because placements are persisted and already-placed ops are skipped.

    `skip_kinds` drops whole classes of op at the one place every emitter feeds
    into — used to keep bonuses out of the staged library while their detection
    is unreliable.
    """
    if skip_kinds:
        ops = [o for o in ops if o.get("kind") not in skip_kinds]
    results = []
    # Destinations claimed by more than one source (an old collapse) cannot be
    # reused by rename. The counts come from the caller because the streaming
    # path calls this once per group and would otherwise only ever see one
    # claimant at a time.
    shared_dests = shared_dests or {}
    dest_abs = os.path.realpath(dest_root)
    # Refuse anything that is not inside a MediaLibrary directory. The subtree
    # may be nested (e.g. MediaLibrary/trial for a bounded test run), so the
    # check is on the path components, not just the basename.
    if "MediaLibrary" not in dest_abs.split(os.sep):
        raise SystemExit(f"refusing to write outside MediaLibrary: {dest_root}")
    src_roots = [os.path.realpath(r).rstrip("/") + os.sep for r in (roots or [])]
    # destinations this plan itself contends for: never replace on those
    contended = set(plan_collisions(ops, dest_root))
    if contended:
        print(f"  ! {len(contended)} destination(s) targeted by more than one op; "
              f"they will not replace existing files", file=sys.stderr)
    for op in ops:
        if budget is not None and budget.exhausted():
            results.append({"src": None, "dst": None,
                            "status": f"stopped after {budget.done} clones (budget)"})
            break
        dst = os.path.join(dest_root, op["dest_dir"], op["name"])
        dst_abs = os.path.realpath(os.path.dirname(dst))
        # hard guard: never write into a source tree
        if any(dst_abs == r.rstrip("/") or (dst_abs + os.sep).startswith(r) for r in src_roots):
            results.append({"src": op["src"], "dst": dst, "status": "REFUSED writes into source"})
            continue
        if not dst_abs.startswith(dest_abs):
            results.append({"src": op["src"], "dst": dst, "status": "REFUSED outside MediaLibrary"})
            continue
        # informational marker, written in place of a copy
        if op.get("kind") == "marker":
            try:
                if not dry:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "w") as fh:
                        fh.write(op.get("text", ""))
                results.append({"src": op["src"], "dst": dst,
                                "status": "marker" if not dry else "dry"})
            except OSError as e:
                results.append({"src": op["src"], "dst": dst, "status": f"marker failed: {e}"})
            continue
        relkey = os.path.join(op["dest_dir"], op["name"])
        if relkey in contended:
            results.append({"src": op["src"], "dst": dst, "status": "skipped (plan collision)"})
            continue
        if os.path.realpath(op["src"]) == os.path.realpath(dst):
            results.append({"src": op["src"], "dst": dst, "status": "REFUSED src==dst"})
            continue

        rec = store.get_placement(op["src"]) if store is not None else None
        if rec and not dry:
            prev_dest, _prev_size = rec
            if os.path.exists(prev_dest):
                if prev_dest == dst:
                    results.append({"src": op["src"], "dst": dst, "status": "skipped (placed)"})
                    continue
                if not rename:
                    results.append({"src": op["src"], "dst": prev_dest, "status": "skipped (placed elsewhere)"})
                    continue
                # rename: move the existing clone to the new destination.
                #
                # Only valid when that clone represents THIS source alone. After a
                # grouping fix, many sources can share one old destination (the
                # earlier bug placed every episode at the same path); moving it
                # would hand all of them the same file, and after the first move
                # the rest would find nothing. A shared destination is therefore
                # re-cloned: the clone it needs does not exist yet.
                if shared_dests.get(prev_dest, 0) > 1:
                    pass                      # fall through to the clone path below
                else:
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if os.path.exists(dst):
                            results.append({"src": op["src"], "dst": dst, "status": "skipped (dest occupied)"})
                            continue
                        os.rename(prev_dest, dst)
                        _record(store, op, dst)
                        results.append({"src": op["src"], "dst": dst, "status": "renamed"})
                        continue
                    except OSError as e:
                        results.append({"src": op["src"], "dst": dst, "status": f"rename failed: {e}"})
                        continue

        # A name collision means the destination holds an older plan's copy.
        # The library is disposable (reflink copies), so replace rather than
        # mint a `__dup` that would leave the stale file looking canonical.
        if os.path.exists(dst) and not dry:
            try:
                same = os.path.getsize(dst) == os.path.getsize(op["src"])
            except OSError:
                same = False
            if same:
                if store is not None:
                    _record(store, op, dst)
                results.append({"src": op["src"], "dst": dst, "status": "skipped (exists)"})
                continue
            try:
                # replacing a stale copy deletes a reflink, so it goes through
                # the paced delete rather than a bare unlink
                delete_reflink(dst)
                if store is not None:
                    store.put_placement(op["src"], 0, dst, kind=op.get("kind"))
            except OSError as e:
                results.append({"src": op["src"], "dst": dst, "status": f"stale remove failed: {e}"})
                continue
        try:
            clone_serialized(op["src"], dst, dry=dry)
            if budget is not None:
                # Counted even in dry mode so a rehearsal stops exactly where
                # the real run would, rather than running the whole plan.
                budget.note()
            results.append({"src": op["src"], "dst": dst, "status": "ok" if not dry else "dry"})
            if store is not None and not dry:
                _record(store, op, dst)
        except subprocess.CalledProcessError as e:
            results.append({"src": op["src"], "dst": dst, "status": f"FAIL reflink: {e}"})
    return results


class CloneBudget:
    """A limit on how many reflinks a run may perform, shared across groups.

    The streaming path applies one group at a time but each group is its own
    `apply_plan` call, so the count has to live outside any single call. Only
    *successful* clones are counted: a skipped op (already placed, collision)
    does not consume budget, and the stop is checked between clones, never
    mid-copy, so the halt is clean and a re-run simply resumes.
    """

    def __init__(self, limit=None):
        self.limit = limit
        self.done = 0
        self._lock = threading.Lock()

    def exhausted(self) -> bool:
        if self.limit is None:
            return False
        with self._lock:
            return self.done >= self.limit

    def note(self) -> None:
        with self._lock:
            self.done += 1


def _record(store, op, dst):
    try:
        size = os.path.getsize(op["src"])
    except OSError:
        size = 0
    meta = op.get("meta") or {}
    mid = meta.get("id")
    # a negative id is the synthetic AniDB-only marker, not a provider id
    store.put_placement(op["src"], size, dst, kind=op.get("kind"),
                        title=meta.get("title"), category=meta.get("_category"),
                        media_id=mid if (mid and mid > 0) else None)
    # rejected versions are provenance, stored not staged; render on demand
    for a in op.get("alternatives") or []:
        store.put_alternative(op["src"], a.get("src"), score=a.get("score"),
                              size=a.get("size"), reason=a.get("reason"))


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None)
    ap.add_argument("--dest", default=None,
                    help="library root (default: $MEDIA_LIBRARY, else $MEDIA_ROOT/MediaLibrary)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", default=None)
    ap.add_argument("--store", default=None,
                    help="sqlite state path (default: $MEDIA_STATE_DB)")
    ap.add_argument("--no-tmdb", action="store_true")
    ap.add_argument("--no-anidb", action="store_true",
                    help="skip the offline AniDB title index (no effect on TMDB)")
    ap.add_argument("--rename", action="store_true",
                    help="move already-placed copies to a newly-computed destination")
    ap.add_argument("--stream", action="store_true",
                    help="plan-and-copy group by group (progressive writes)")
    ap.add_argument("--probe-workers", type=int, default=3)
    ap.add_argument("--max-groups", type=int, default=None,
                    help="stop identifying after N groups (bounds the walk, and thus the API calls)")
    ap.add_argument("--max-clones", type=int, default=None,
                    help="stop after N successful reflinks (clean halt between clones; re-run resumes)")
    ap.add_argument("--copy-workers", type=int, default=1)
    ap.add_argument("--show-config", action="store_true",
                    help="print the resolved configuration and exit")
    ap.add_argument("--purge", default=None, metavar="DIR",
                    help="delete a tree under MediaLibrary, one file at a time "
                         "behind the reflink gate (deletion is not free); "
                         "--max-clones bounds how many files to remove")
    ap.add_argument("--overrides", default=None,
                    help="forced-classification file (default: $MEDIA_OVERRIDES, "
                         "else <config dir>/overrides.yaml)")
    ap.add_argument("--remux-list", default=None,
                    help="write suggested makemkvcon commands for raw DVD/ISO "
                         "sources (default: <dest>/to-remux.list)")
    args = ap.parse_args(argv)

    if args.show_config:
        print(C.describe())
        print(f"MEDIA_STATE_DB={C.state_db()}")
        return 0

    if args.purge:
        target = os.path.realpath(args.purge)
        lib = os.path.realpath(args.dest or C.media_library())
        # refuse anything that is not inside the library: a purge of a source
        # tree would be unrecoverable, and the sources are read-only by contract
        if not (target == lib or target.startswith(lib.rstrip(os.sep) + os.sep)):
            raise SystemExit(f"refusing to purge outside the library: {args.purge}")
        if not os.path.exists(target):
            raise SystemExit(f"nothing to purge: {args.purge}")
        n = delete_tree_paced(target, budget=CloneBudget(args.max_clones))
        print(f"purged {n} file(s) from {args.purge}")
        return 0

    try:
        ovr = OV.load(args.overrides or C.overrides_file())
    except OV.OverrideError as e:
        raise SystemExit(f"overrides file is unusable: {e}")
    if ovr:
        print(f"  overrides: {len(ovr)} entr(ies) across {len(ovr.mapping)} key(s)")

    args.dest = args.dest or C.media_library()
    args.store = args.store or C.state_db()
    roots = args.root or C.default_sources()
    store = tmdb = None
    if not args.no_tmdb:
        try:
            from store import Store
            store = Store(args.store)
            # Offline AniDB title index (daily dump, no per-title requests).
            anidb = None
            if store is not None:
                try:
                    import anidb as _anidb
                    anidb = None if args.no_anidb else _anidb.AniDB()
                except Exception:
                    anidb = None
            tmdb = M.TMDB(store=store, anidb=anidb)
        except Exception as e:
            print(f"  ! TMDB disabled: {e}", file=sys.stderr)

    if args.stream:
        t0 = time.time()
        n = stream_apply(roots, args.dest, tmdb=tmdb, store=store,
                         probeworkers=args.probe_workers, copyworkers=args.copy_workers,
                         dry=not args.apply, max_groups=args.max_groups,
                         max_clones=args.max_clones, ovr=ovr, rename=args.rename,
                         remux_path=args.remux_list)
        print(f"streamed {n} groups in {time.time()-t0:.1f}s")
        if tmdb:
            print("api calls:", tmdb.n_calls)
            tmdb.close()
        return

    t0 = time.time()
    ops, decisions, nfiles, discs = build_plan(roots, tmdb, store=store, max_groups=args.max_groups, ovr=ovr)
    if discs:
        path = args.remux_list or os.path.join(args.dest, "to-remux.list")
        n = write_remux_list(discs, path, os.path.join(args.dest, "Remuxes"))
        print(f"  {n} raw DVD/ISO source(s) -> {path}")
    print(f"scanned {nfiles} files -> {len(ops)} ops, {len(decisions)} groups in {time.time()-t0:.1f}s")
    for d in decisions:
        print(f"  {d['group'][:46]:46} {d['versions']}v -> {os.path.basename(d['kept'])}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"ops": ops, "decisions": decisions}, fh, ensure_ascii=False, indent=2)
        print(f"wrote {args.json}")
    if args.apply:
        res = apply_plan(ops, args.dest, roots=roots, dry=False, store=store,
                         rename=args.rename, budget=CloneBudget(args.max_clones),
                         skip_kinds=() if STAGE_BONUSES else ("bonus",))
        # A status is a failure only when it neither placed nor legitimately
        # skipped: 'renamed' (moved an existing clone) and the 'skipped (...)'s
        # (already placed / plan collision / dest occupied) are not faults, and
        # counting them as such reported 84 failures on a run that placed 84.
        bad = [r for r in res if not (r["status"] == "ok" or r["status"] == "renamed"
                                      or r["status"].startswith("skipped"))]
        print(f"applied {len(res)} ops, {len(bad)} failures")
        for r in bad[:20]:
            print("  FAIL", r["src"], r["status"])
    if tmdb:
        print("api calls:", tmdb.n_calls)
        tmdb.close()


if __name__ == "__main__":
    main()
