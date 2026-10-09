#!/usr/bin/env python3
"""media-organizer: identify & route visual media into a rational tree.

Deterministic core (parse -> identify -> score -> route -> render) so it can be
dry-run against any filename corpus without touching the pool. The only
side-effecting stage is `copy`, which uses cp --reflink=always.
"""
from __future__ import annotations
import os, re, json, subprocess, unicodedata, datetime, time, random
import queue, threading, concurrent.futures

try:
    from store import query_key
except ImportError:  # single-file use
    def query_key(title, year, kind):
        t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
        return f"{kind}|{t}|{year or ''}"

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".flv", ".webm"}
SUB_EXT   = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".sup"}
# Audio is never staged: .flac/.mp3 beside a film are leaked soundtrack/bonus
# tracks, and the mime fallback would otherwise classify them as video.
AUDIO_EXT = {".flac", ".mp3", ".aac", ".m4a", ".ogg", ".opus", ".wav", ".wma",
             ".ac3", ".dts", ".mka", ".ape", ".alac", ".m3u", ".m3u8"}
AUX_EXT   = {".nfo", ".jpg", ".jpeg", ".png", ".webp", ".txt", ".cue"}
SAMPLE_EXT = {".nfo"}  # .nfo is aux, but can be "sample.nfo"

# ---------------------------------------------------------------- parse
_ANIME_GROUP = re.compile(r"^\[([^\]]+)\]\s*")
_ANIME_EPISODE = re.compile(r"\b(\d{1,4})(?:v(\d+))?\b")
EXTRA_TOKENS = re.compile(
    r"(?i)(\bextras?\b|\bbonus\b|\bfeaturette|\binterview|behind[- ]the[- ]scenes|"
    r"\bmaking[-_. ]?(of|[\w]*\.)|deleted[- ]scenes?|bloopers?|gag[- ]reel|\btrailer|"
    r"\bteaser|\bsample\b|\bmenu\b|ncop|nced|\bop\d+\b|\bed\d+\b|preview|pv\d+|"
    r"creditless|\bsp\d+\b|\bspecials?\b|\bbehind[\w ]*scenes\b)")
EXTRA_DIRS = {"extras", "extra", "bonus", "bonuses", "featurettes", "special features",
              "samples", "sample", "menu", "menus", "subs", "subtitles", "ncop", "nced"}
JUNK_DIRS = {"__macosx", ".ds_store", "thumbnails", "backdrops", "metadata",
             ".@__thumb", "@eadir", "certificate", "auxdata", "bdjo", "jar",
             "playlist", "clipinf", "backup", "bdmv/meta"}

TV_RE = re.compile(r"(?i)\bS(\d{1,2})[\s._-]*E(\d{1,3})\b|\b(\d{1,2})x(\d{2,3})\b")
# Explicit episode markers that S01E01/1x02 miss — fansub and broadcast naming:
# "[E-D]_Title_Ep10v2_(CRC)", "Title - 13 - Episode 13", "Show EP.05".
# The lookbehind is letters-only, not \b: in "_Ep10v2_" the preceding "_" is a
# word character, so \b would not fire. The trailing lookahead rejects
# "Ep1080p", which is a resolution, not an episode.
EP_EXPLICIT_RE = re.compile(r"(?i)(?<![a-z])ep(?:isode)?[\s._-]*(\d{1,4})(?:v(\d+))?(?![\dp])")
# Scene naming uses a bare "e" for the episode: "cowboy.bebop.e02.multi.1080p...".
# Only when delimited by a separator on both sides, so an "e" inside a word
# ("the.thing") never matches, and capped at 3 digits so "e1080" is not one.
EP_DOTTED_E_RE = re.compile(r"(?i)(?:^|[._\s-])e(\d{1,3})(?:v(\d+))?(?=[._\s-])")
# Bare dash-delimited episode number, as in "Mini Moni the TV - 13 - Episode 13".
# Digits are capped at 3 so a year in "Title - 1969 - Title" is not an episode.
EP_DASH_RE = re.compile(r"(?<!\d)[-–—][\s._-]*(\d{1,3})(?:v(\d+))?[\s._-]*[-–—](?!\d)")
# Inline episode number bounded by underscores, as in
# "[Some-Stuffs]_Jojo_..._Crusaders_47_(1920x1080..." and
# "[a-s]_mahoromatic_..._-_12_-_to_the_scenery...". Sits after the anime-group
# branch so the "[a1b2c3d4]" update hash is already removed, and the number is
# bounded to 3 digits so a year is never taken for an episode.
EP_UNDERSCORE_RE = re.compile(r"(?<=\d)_(\d{1,3})(?:v(\d+))?_(?!\d)|(?<=[a-z])_(\d{1,3})(?:v(\d+))?(?=_|\])")
# Season and episode concatenated with no separator: "The.Wire.S0401" is s04e01.
# Trailing lookahead stops a 5-digit run being read as season+episode.
EP_CONCAT_RE = re.compile(r"(?i)(?:^|[._\s-])s(\d{2})(\d{2})(?!\d)")
# An episode after a single dash, not dashes on both sides: "Victory - 09v2".
# The trailing lookahead rejects a resolution ("- 1080p"), which is not an episode.
EP_DASH_ONE_RE = re.compile(r"(?<!\d)[-–—][\s._-]*(\d{1,3})(?:v(\d+))?(?![\dp])(?!\.\d)")
# ^ the final lookahead rejects a decimal, so the "1" in an audio token
#   ("DTS-HD-1.0", "DD-5.1") is not read as episode 1 — a false positive that
#   mislabelled whole films as S01E01 and filed them as series episodes.
YEAR_RE = re.compile(r"(?:^|[\s._\[(\-])((?:19|20)\d{2})(?:[\s._\])\)\-]|$)")
PAREN_YEAR = re.compile(r"[\(\[]((?:19|20)\d{2})[\)\]]")


def plausible_year(y: int) -> bool:
    return 1900 <= y <= datetime.date.today().year + 1


def norm(path: str) -> str:
    return unicodedata.normalize("NFKC", path)


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Identity of the current match-scoring rule. Written onto every cached media
# record; a cached record that lacks it — or carries an older value — was scored
# by a weaker rule and is re-verified once against the current one. Bump this
# whenever the scoring changes, so stale/incorrect identities heal themselves.
# "loc1" added localization-aware scoring (see TMDB._identify_uncached).
_SCORING_RULE = "loc1"

# A single quality/source token, as it appears after separator-splitting.
_NOISE_TOKEN_RE = re.compile(
    r"(?i)^(?:ntsc|pal|dvdrip|dvd|bluray|blu-ray|bdrip|brrip|web-?dl|webrip|hdtv|"
    r"remux|x264|x265|h\.?264|h\.?265|hevc|avc|aac|ac3|eac3|flac|ddp?[\d.]*|dd[\d.]*|"
    r"multi|dual|vostfr|truefrench|internal|repack|proper|hi10p?|\d{3,4}p|\d{3,4}x\d{3,4})$")
_TITLE_TOKEN_SPLIT_RE = re.compile(r"[\s._]+")


def _file_episode_title(text: str) -> str | None:
    """The episode title written in a file name, if any.

    guessit is tried first: it knows release tags, codecs and groups and returns
    the title from scenes like "...S01E01 1080p Welcome To The Hellmouth.HDTV.
    DD2.0.x264". Its answer is authoritative — when it says there is no title,
    the regex must not fill the gap with release noise ("...S02E01.1080p.WEB.
    h264-KOGi" must stay title-less, not become "KOGi"). One form guessit misses
    is a SPACE-SEPARATED title after the resolution ("Buffy S01E08 1080p Out Of
    Mind, Out Of Sight.HDTV..."), so the regex runs only when the remainder
    carries a space — a human-named file. A pure-dotted remainder is a scene
    release whose trailing tokens are noise, so nothing is returned there. The
    regex is also the whole story on a host without guessit (the dev box).
    """
    try:
        from guessit import guessit
    except ImportError:
        guessit = None
    if guessit is not None:
        try:
            t = guessit(text).get("episode_title")
        except Exception:
            t = None
        if t and str(t).strip():
            return str(t).strip()
    m = re.search(r"(?i)(?:\bS\d{1,2}[\s._-]*E\d{1,3}\b|\b\d{1,2}x\d{2,3}\b)(?P<rest>.*)$", text)
    if not m:
        return None
    rest = re.sub(r"(?i)\.(?:mkv|mp4|avi|m4v|mov|ts|wmv|mpg|mpeg)$", "", m.group("rest"))
    if guessit is not None and " " not in rest:
        return None
    kept = []
    for tok in _TITLE_TOKEN_SPLIT_RE.split(rest):
        tok = tok.strip("[]{}")
        if not tok:
            continue
        if _NOISE_TOKEN_RE.match(tok) or _NOISE_TOKEN_RE.match(tok.split("-", 1)[0]):
            continue
        kept.append(tok)
    # "...DD2.0.x264" splits into "DD2" (noise) and a stray "0"; drop trailing
    # bare-number fragments so the title does not end with channel noise.
    while kept and kept[-1].strip().isdigit():
        kept.pop()
    return " ".join(kept).strip(" -_.") or None


def sanitize(name: str) -> str:
    """Make a path component safe for NTFS/SMB: drop illegal chars, no trailing dot/space."""
    s = _ILLEGAL.sub("-", name)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"-{2,}", "-", s).strip(" -.").rstrip(".")
    return s or "Unknown"


def parse_name(path: str) -> dict:
    """Best-effort structure from a filename BEFORE metadata lookup."""
    base = os.path.basename(norm(path))
    root, ext = os.path.splitext(base)
    ext = ext.lower()
    out = {"ext": ext, "stem": root, "group": None, "season": None, "episode": None,
           "version": None, "year": None, "resolution": None, "is_extra": False,
           "anime": False}

    m = _ANIME_GROUP.match(root)
    stripped = root
    if m:  # [SubsPlease] Frieren - 12v2 (1080p) [A1B2C3D4]
        out["group"] = m.group(1)
        out["anime"] = True
        stripped = _ANIME_GROUP.sub("", root)
        stripped = re.sub(r"\[[0-9A-Fa-f]{8}\]", "", stripped)          # CRC
        tv = TV_RE.search(stripped)
        if tv:
            out["season"] = int(tv.group(1) or tv.group(3))
            out["episode"] = int(tv.group(2) or tv.group(4))
        else:
            em = re.search(r"[-–][\s._-]*(\d{1,4})(?:v(\d+))?", stripped)
            if em:
                out["episode"] = int(em.group(1))
                out["version"] = int(em.group(2)) if em.group(2) else None
                stripped = stripped[:em.start()]

    tv = TV_RE.search(stripped)
    if tv and out["season"] is None:
        out["season"] = int(tv.group(1) or tv.group(3))
        out["episode"] = int(tv.group(2) or tv.group(4))

    # Explicit episode markers that the S01E01 form does not cover. Without
    # these, fansub naming ("_Ep10v2_") and broadcast naming ("- 13 - Episode
    # 13") parse as films, and every episode of a series then collapses into one
    # version group — each episode ranked as an alternative encode of the others.
    if out["episode"] is None:
        # EP_CONCAT first: it carries a season too, which the others cannot see
        cm = EP_CONCAT_RE.search(stripped)
        if cm:
            out["season"] = int(cm.group(1))
            out["episode"] = int(cm.group(2))
        for rx in (EP_EXPLICIT_RE, EP_DOTTED_E_RE, EP_DASH_RE,
                   EP_DASH_ONE_RE, EP_UNDERSCORE_RE):
            em = rx.search(stripped)
            if em:
                groups = [g for g in em.groups()]
                num = next((g for i, g in enumerate(groups) if g and i % 2 == 0), None)
                ver = next((g for i, g in enumerate(groups) if g and i % 2 == 1), None)
                if num:
                    out["episode"] = int(num)
                    if ver:
                        out["version"] = int(ver)
                    break

    # The episode TITLE is often written in the file name ("Show - 1x01 - Title",
    # "Show S01E01 - Title"). It is a free, exactly-correct fallback for the
    # provider lookup used for naming; discarding it left episodes named with
    # just their number whenever the provider had no name for that instalment.
    if out["episode"] is not None:
        out["episode_title_file"] = _file_episode_title(stripped)

    ym = None
    pm = PAREN_YEAR.search(stripped)
    if pm and plausible_year(int(pm.group(1))):
        ym = pm
    else:
        for cand in YEAR_RE.finditer(stripped):
            if plausible_year(int(cand.group(1))):
                ym = cand
                break
    if ym:
        out["year"] = int(ym.group(1))

    rm = re.search(r"(?i)\b(2160p|1080p|720p|480p|4k|uhd|8k)\b", stripped)
    if rm:
        out["resolution"] = rm.group(1).lower()

    if EXTRA_TOKENS.search(stripped):
        out["is_extra"] = True
    if out["episode"] is not None and re.search(r"(?i)\b(?:op|ed|ncop|nced|pv|sp)\d*\b", stripped):
        out["is_extra"] = True
    return out


# Release/quality noise, used to simplify an over-long title before querying.
_NOISE_RE = re.compile(
    r"(?i)\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|dv|bluray|blu-ray|bdrip|brrip|web-?dl|"
    r"webrip|hdtv|dvdrip|remux|x264|x265|h\.?264|h\.?265|hevc|avc|aac|ac3|dts|ddp?5\.1|"
    r"flac|multi|dual|vostfr|truefrench|internal|repack|proper|extended|criterion|"
    r"\d{3,4}x\d{3,4})\b")


def query_cascade(title: str, kind: str) -> list[str]:
    """Search queries for a title, simplest last.

    Identification under-matches when the query is the *whole* filename stem
    ("neon genesis evangelion - 03 - 720p hevc"), which TMDB does not have. The
    title is therefore reduced in steps, trying the specific form first so a
    precise match still wins:

      1. the title as-is (minus a trailing CRC/group tag);
      2. release and quality tokens removed;
      3. trailing tokens dropped one at a time, down to a floor.

    Bounded on purpose: each step costs an API call, and the rate limit is shared.
    Whether a step *helps* is left to the caller, which keeps the best-scoring
    result across all steps rather than the first that returns anything.
    """
    seen, out = set(), []
    def add(t):
        t = re.sub(r"[\[\]()]", " ", t)                 # stray brackets from stripping
        t = " ".join(t.split()).strip(" -_.(")
        if t and t.lower() not in seen:
            seen.add(t.lower()); out.append(t)

    base = re.sub(r"\[[0-9A-Fa-f]{6,8}\]", " ", str(title))          # [A1B2C3D4]
    # a leading release-group tag ("[Some-Stuffs]", "[HorribleSubs]") is not part
    # of the title and hides it from search
    base = re.sub(r"^\s*\[[^\]]{2,24}\]\s*", " ", base)
    base = re.sub(r"[._]", " ", base)
    add(base)
    stripped = _NOISE_RE.sub(" ", base)
    add(stripped)
    if kind == "tv":
        # "... - 03 - ..." and "e03" name the episode, not the series
        add(re.sub(r"(?i)\b(?:s\d{1,2}\s*e\d{1,3}|\d{1,2}x\d{1,3}|e\d{1,3}|ep\d{1,3})\b", " ", stripped))
        add(re.sub(r"(?i)\s-\s*\d{1,3}\s-.*$", " ", stripped))
        # a scene tag glued to the title with a dash ("cowboy bebop -kazetv")
        add(re.sub(r"\s-\S+$", "", out[-1]))
    toks = out[-1].split()
    # drop trailing tokens down to a two-token floor: the reduction is only
    # useful if it reaches the bare title ("cowboy bebop"), so stopping at three
    # would defeat the purpose
    for n in range(len(toks) - 1, 1, -1):
        add(" ".join(toks[:n]))
    return out[:7]


def parse_with_guessit(path: str) -> dict:
    try:
        from guessit import guessit
    except ImportError:
        return {}
    g = guessit(os.path.basename(norm(path)))
    return dict(g)


# ---------------------------------------------------------------- title match
# Comparing a parsed title with TMDB is where a naive client fails: it takes the
# first search result, so a romaji name ("Shingetsutan Tsukihime") or an
# abbreviated one never reaches the entry that holds the right alias
# ("Lunar Legend Tsukihime"). We instead score every candidate against the
# canonical title and every alternative title TMDB reports.
#
# Similarity is deliberately NOT allowed to override year and kind: titles are
# adversarial for similarity ("The Thing" 1982 vs 2011, sequels, remakes). Those
# two stay hard filters, and only the ordering within them is semantic.
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def title_key(text: str) -> str:
    """Fold a title to comparable form: case, punctuation, spacing, articles."""
    if not text:
        return ""
    s = norm(str(text)).lower()
    # "Thing, The" and "Créateur, Le" invert the article. This must run before
    # punctuation is folded to spaces, or the comma it keys on is already gone.
    m = re.match(r"^(.*?),\s*(the|a|an|le|la|les|el|los|der|die|das|il|lo)$", s.strip())
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # a leading article is noise for matching ("The Thing" == "Thing")
    s = re.sub(r"^(the|a|an|le|la|les|el|los|der|die|das|il|lo)\s+", "", s)
    return s


def title_tokens(text: str) -> list[str]:
    """Tokenise a title for comparison.

    Character ratios (WRatio and friends) work on the raw string, so they are
    thrown by word *order* and by differences that do not change meaning:
    "Cowboy Bebop: Knockin' on Heaven's Door" vs "Cowboy Bebop - The Movie".
    Tokenising first makes the comparison about which words are present, which
    is how a person reads a title. Tokens are already lowercased and stripped of
    punctuation by `title_key`, so no extra work is needed beyond splitting.
    """
    return title_key(text).split()


def token_similarity(qtokens: list[str], ctokens: list[str]) -> float:
    """Jaccard-style overlap of two token sets, weighted toward coverage.

    Coverage (how much of the *query* the candidate accounts for) matters more
    than a symmetric intersection, but a candidate that simply contains the
    query plus a lot more must not score full marks — that is how "The Thing"
    would match "The Thing From Another World". So the score is the harmonic
    mean of coverage and precision: both must be high.
    """
    if not qtokens or not ctokens:
        return 0.0
    qs, cs = set(qtokens), set(ctokens)
    inter = len(qs & cs)
    if not inter:
        return 0.0
    coverage = inter / len(qs)        # how much of the query is explained
    precision = inter / len(cs)       # how much of the candidate is explained
    if coverage == 0 or precision == 0:
        return 0.0
    return 2 * coverage * precision / (coverage + precision)


def title_similarity(query: str, candidate_titles) -> float:
    """Best similarity of `query` against a candidate's title set, in [0, 1].

    Two readings are combined, taking the better:
      * token overlap — robust to word order and to noise words, which is the
        common case for titles ("...Knockin' on Heaven's Door" vs "...The Movie"
        is partial, but "Cowboy Bebop" is fully contained in both);
      * character ratio (WRatio) — catches typos and transliteration variants
        that share no exact tokens.

    Neither is allowed to rate a candidate that merely *contains* the query plus
    extra words as a perfect match: that is how "The Thing" would match "The
    Thing From Another World", two different films. The token score's precision
    term and an explicit superset penalty both guard against it.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return 0.0
    q = title_key(query)
    if not q:
        return 0.0
    qtok = q.split()
    best = 0.0
    for t in candidate_titles:
        c = title_key(t)
        if not c:
            continue
        if q == c:
            return 1.0
        ctok = c.split()
        score = max(token_similarity(qtok, ctok), fuzz.WRatio(q, c) / 100.0)
        # superset: candidate says everything the query says, plus more
        if len(ctok) > len(qtok) and set(qtok) <= set(ctok):
            score -= min(0.3, 0.1 * (len(ctok) - len(qtok)))
        if score > best:
            best = score
    return best


def year_is_release(text: str, total_sec: float | None) -> int | None:
    """Extract a title-year from a friendly name, avoiding episode/quality noise."""
    for m in YEAR_RE.finditer(text):
        y = int(m.group(1))
        if 1900 <= y <= datetime.date.today().year + 1:
            return y
    return None


# ---------------------------------------------------------------- scoring
# source authenticity: a lossless remux of a disc is the best available form;
# the raw disc structure itself ranks below it (it is not a library file).
SRC_RANK = {"remux": 6, "bluray": 5, "bdrip": 5, "brrip": 5, "uhd": 5,
            "web-dl": 3, "webdl": 3, "web": 3, "amzn": 3, "nf": 3, "atvp": 3,
            "webrip": 2, "hdtv": 1, "dvdrip": 1, "dvd": 1, "tvrip": 0}
RAW_DISC_RANK = 4          # a .m2ts/BDMV stream: authentic but not consumable
RES_RANK = {"8k": 7, "4320p": 7, "2160p": 6, "4k": 6, "uhd": 6, "1080p": 5,
            "1080i": 4, "720p": 3, "576p": 2, "480p": 1}


def source_rank(path: str, features: dict) -> int:
    """How 'authentic' the file is: remux > blu-ray encode > raw disc > web..."""
    if features.get("raw_disc"):
        return RAW_DISC_RANK
    txt = (features.get("source") or "") + " " + os.path.basename(path or "")
    g = parse_with_guessit(path) if path else {}
    src = str(g.get("source") or features.get("source") or "").lower()
    for key, rank in SRC_RANK.items():
        if key in src or key in txt.lower():
            return rank
    return -1


def _is_43(features: dict) -> bool:
    """Does this file present a ~4:3 (1.333) frame?

    Broadcast-era TV was shot 4:3; a widescreen release of such a show is often a
    pan-and-scan **crop** that loses picture. Resolution alone cannot see that, so
    this reads the stored width/height. Tolerance keeps 1.33 and 1.37 (Academy)
    on the same side and excludes 1.6/1.78.
    """
    w, h = features.get("width"), features.get("height")
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        return False
    if h <= 0:
        return False
    return abs(w / h - 4 / 3) <= 0.18


def quality_score(features: dict, path: str | None = None,
                  original_language: str | None = None, kind: str | None = None) -> tuple:
    """Higher = better, as a structural tuple so each axis stays visible.

    Order, most significant first:
      1. original (or multi-lingual including original) audio present
      2. **TV only:** 4:3 presentation — a widescreen version of a 4:3 show is
         usually a pan-and-scan crop, so it must not win on resolution
      3. resolution
      4. source authenticity (remux > blu-ray > raw disc > web/iTunes > ...)
      5. HDR, then bit depth, then bitrate

    Axis 2 applies only when `kind == "tv"`: films are natively widescreen and
    must never be downgraded for being so.
    """
    res = RES_RANK.get((features.get("resolution") or "").lower(), 0)
    src = source_rank(path or "", features)
    orig = 1 if has_original_audio(features, original_language) else 0
    nar = 1 if (kind == "tv" and _is_43(features)) else 0
    hdr = 1 if features.get("hdr") else 0
    try:
        depth = int(features.get("bit_depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        rate = int(features.get("bitrate") or 0)
    except (TypeError, ValueError):
        rate = 0
    return (orig, nar, res, src, hdr, depth, rate)


def has_original_audio(features: dict, original_language: str | None) -> bool:
    langs = features.get("audio_langs") or []
    if not langs:            # unknown -> permissive
        return True
    if not original_language:
        return True
    if original_language in langs:
        return True
    # undetermined/unknown or multiple: permissive. The probe stores the first
    # two chars, so MediaInfo's "und" lands here as "un" — both must be caught.
    if "un" in langs or "und" in langs or "mul" in langs:
        return True
    return False


# ---------------------------------------------------------------- routing
def _dedup_tokens(text: str) -> str:
    """Collapse repeated whitespace-separated tokens: '2160p UHD UHD' -> '2160p UHD'."""
    out = []
    for tok in text.split():
        if tok.lower() not in [t.lower() for t in out]:
            out.append(tok)
    return " ".join(out)


def _int_year(y):
    try:
        return int(y) if y else None
    except (TypeError, ValueError):
        return None


def decade(year: int | None) -> str:
    year = _int_year(year)
    if not year:
        return "Unknown"
    return f"{(year // 10) * 10}s"


def feature_stem(title: str, year: int | None, director: str | None) -> str:
    """`{Title} ({Year}) ({Director})` — the mandated feature file stem.

    Missing parts are omitted rather than filled with a placeholder: Jellyfin
    reads the parentheticals positionally, so a bogus "(Unknown)" year is worse
    than none.
    """
    year = _int_year(year)
    s = sanitize(title) if title else "Unknown"
    if year:
        s += f" ({year})"
    if director:
        s += f" ({sanitize(director)})"
    return s


def show_dir(title: str, first: int | None, last: int | None) -> str:
    """`{Show} ({FirstYear}-{LastYear})` — the mandated series folder.

    Collapses to a single year when the run is one year (or the end is unknown,
    as for an in-progress show).
    """
    first, last = _int_year(first), _int_year(last)
    s = sanitize(title) if title else "Unknown"
    if first and last and last != first:
        s += f" ({first}-{last})"
    elif first:
        s += f" ({first})"
    return s


def episode_stem(season, episode, ep_title: str | None) -> str:
    """`S{NN}E{NN} - {EpisodeTitle}` — the mandated episode file stem."""
    s = f"S{int(season):02d}E{int(episode):02d}"
    if ep_title:
        s += f" - {sanitize(ep_title)}"
    return s


def media_dir(title: str, year: int | None, author: str | None, kind: str = "movie",
              last_year: int | None = None, season=None) -> str:
    """The directory a work sits in, under its category.

    Jellyfin has no use for the decade/author nesting this used to produce: it
    wants `category/{show}/` for a series and nothing deeper for a film, with all
    the distinguishing information in the file NAME. So a film returns "" (its
    category directory is the whole path) and a series returns its show folder.
    """
    if kind == "tv":
        return show_dir(title, year, last_year)
    return ""


def subtitle_keep(lang: str | None, anime: bool) -> bool:
    if not lang:
        return False
    lang = lang.lower()
    wanted = {"ja", "fr"} if anime else {"ja", "en"}
    return lang in wanted


BONUS_ROOT = "_BONUS_"
CINEMA_BASE = os.path.join("Cinema")
JUNK_CATEGORY = "_JUNK_"
TMDB_ANIMATION_GENRE = 16

SHORT_EXT = {".avi", ".mkv", ".mp4", ".mov"}  # short films still video


def is_junk_path(hint_path: str) -> bool:
    return any(x.lower() in ("junk", "junk2", "unsorted", "todo") for x in (hint_path or "").split(os.sep))


def _path_segments(hint_path: str) -> list:
    """Folder name segments of a hint path, lowercased."""
    return [p for p in (hint_path or "").replace("\\", "/").split("/") if p]


def _has_segment(hint_path: str, *words) -> bool:
    """True when one of `words` is a whole PATH SEGMENT or a token in one.

    A substring test over the whole path is wrong: the release group
    "x264-SHORTBREHD" made every South Park episode a "short film", because the
    path contains "short". Matching a segment (or a token within one, split on
    separators) keys on a real folder like "Shorts/" or a token like "anime".
    """
    for seg in _path_segments(hint_path):
        toks = re.split(r"[^a-z0-9]+", seg)
        for w in words:
            if w in toks:
                return True
    return False


def category(kind: str, eff: dict, meta: dict | None, duration_ms, hint_path: str = "",
             n_videos: int = 1) -> str:
    """Map a resolved title to one of the on-disk Cinema categories."""
    if is_junk_path(hint_path):
        return JUNK_CATEGORY
    if _has_segment(hint_path, "short", "shorts"):
        return "ShortFilms"
    if _has_segment(hint_path, "anime"):
        return "AnimeSeries" if kind == "tv" else "AnimeFilms"
    if _has_segment(hint_path, "experimental"):
        return "Experimental"
    genres = set((meta or {}).get("genres") or [])
    gids = set((meta or {}).get("genre_ids") or [])
    lang = eff.get("original_language")
    is_animation = ("Animation" in genres or TMDB_ANIMATION_GENRE in gids)
    if kind == "tv":
        # Japanese live-action series are Shows, not Anime — require animation.
        return "AnimeSeries" if (lang == "ja" and (is_animation or meta is None)) else "Series"
    if lang == "ja" and (is_animation or meta is None):
        return "AnimeFilms"
    if "Animation" in genres or TMDB_ANIMATION_GENRE in gids:
        return "Animation"
    try:
        dur = float(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        dur = None
    # A short is a standalone work, not one instalment of a multi-part title.
    multi_part = n_videos > 1 or bool(re.search(r"(?i)(^|[/_ .-])t\d{1,2}([/_ .-]|$)", hint_path or ""))
    if dur and dur < 2_400_000 and not multi_part:
        return "ShortFilms"
    return "FeatureFilms"


# ---------------------------------------------------------------- routing


def route(path: str, meta: dict, features: dict, videos_in_dir: list[str],
          from_filename: bool = True, season=None, episode=None) -> dict:
    """Decide target directory + filename. Pure; no I/O.

    When the caller has already resolved title/year at group level, it passes
    `from_filename=False` so a file name can never re-derive (or corrupt) them.

    `season`/`episode` are the *group's* resolved instalment when it is known.
    A series whose filenames carry no season token ("cowboy.bebop.e02...") still
    has to be filed as an episode, so the episode branch is gated on the episode
    — a name re-parsed here would report `season is None` and fall through to the
    movie branch, giving every episode one shared name (each clone overwriting
    the last).
    """
    p = parse_name(path)
    # The group's resolved instalment wins, but only when it is a real number:
    # a movie group carries `('F', <filename>)` as its episode marker, which is
    # an identity for grouping, not an episode to name with. Anything unusable
    # falls back to the file name (a bare parse of "cowboy.bebop.e02..." yields
    # no season at all, which is why the group value matters).
    season = season if isinstance(season, int) else p["season"]
    episode = episode if isinstance(episode, int) else p["episode"]
    # an episode with no season token is season 1 — it is still an episode, never
    # a film, or every episode of the group collapses onto one destination name
    is_episode = episode is not None
    if is_episode and season is None:
        season = 1
    ext = p["ext"]
    cat = meta.setdefault("_category", "FeatureFilms") if isinstance(meta, dict) else "FeatureFilms"
    base_prefix = os.path.join(CINEMA_BASE, cat)
    is_bonus = p["is_extra"] or any(part.lower() in EXTRA_DIRS for part in path.split(os.sep)[:-1])
    title = sanitize(meta.get("title") or p["stem"])
    year = _int_year(meta.get("year") if not from_filename else (meta.get("year") or p["year"]))
    author = meta.get("author")                 # director, for a film
    last_year = _int_year(meta.get("last_year"))
    tag = _dedup_tokens(features.get("tag") or "")
    def _with_tag(n):
        return n + (f" [{tag}]" if tag else "")
    if is_bonus:
        return {"kind": "bonus",
                "dest_dir": os.path.join(CINEMA_BASE, BONUS_ROOT, feature_stem(title, year, author)),
                "name": sanitize(os.path.splitext(os.path.basename(path))[0]) + ext}
    if is_episode:
        # Jellyfin wants the season as its own directory and the episode number
        # AND title in the file name; the show folder carries the year span.
        d = os.path.join(base_prefix, media_dir(title, year, author, "tv", last_year=last_year))
        # Provider episode name first; the file name's own episode title is the
        # fallback when the provider has none (a movie-collection match, an
        # unmapped season, a 404). Either way the number alone is never used.
        ep_title = features.get("episode_title") or p.get("episode_title_file")
        if ext in SUB_EXT:
            lang = features.get("language")
            base = episode_stem(season, episode, ep_title)
            if lang:
                base += f".{lang}"
            return {"kind": "subtitle", "dest_dir": d, "name": sanitize(base) + ext}
        n = episode_stem(season, episode, ep_title)
        if p["version"]:
            n += f"v{p['version']}"
        return {"kind": "episode", "dest_dir": d, "name": sanitize(_with_tag(n)) + (ext or ".mkv")}
    if ext in VIDEO_EXT:
        # no per-title directory: the film's whole identity is in the name
        return {"kind": "movie", "dest_dir": base_prefix,
                "name": sanitize(_with_tag(feature_stem(title, year, author))) + ext}
    # sidecar subtitle sits beside the film, same stem
    lang = features.get("language")
    stem = feature_stem(title, year, author)
    if lang:
        stem += f".{lang}"
    return {"kind": "subtitle", "dest_dir": base_prefix,
            "name": sanitize(stem) + ext, "keep": subtitle_keep(lang, p["anime"])}


# ---------------------------------------------------------------- identify
class TokenBucket:
    """Shared rate limiter: at most `rate` acquisitions per second, burst-capped.

    TMDB allows ~40 requests/second per API key; we stay well under it. Thread
    safe, monotonic-clock based (immune to wall-clock jumps). `penalise()` drains
    the bucket and holds it dry for a cool-off, so a 429 slows *every* worker,
    not just the one that saw it.
    """
    def __init__(self, rate: float = 20.0, burst: float | None = None):
        self.rate = max(0.01, float(rate))
        self.capacity = float(burst if burst is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._ts = time.monotonic()
        self._lock = threading.Lock()
        self._hold_until = 0.0

    def acquire(self, tokens: float = 1.0):
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._ts) * self.rate)
                self._ts = now
                if now >= self._hold_until and self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
                if now < self._hold_until:
                    wait = max(wait, self._hold_until - now)
            time.sleep(min(wait, 5.0))

    def penalise(self, seconds: float = 5.0):
        """Server said stop: empty the bucket and hold it for `seconds`."""
        with self._lock:
            self._tokens = 0.0
            self._ts = time.monotonic()
            self._hold_until = max(self._hold_until, time.monotonic() + float(seconds))


class RequestQueue:
    """Rate-limited HTTP job pool (shared token bucket, a few workers).

    Jobs are callables returning a requests.Response. A small pool drains them
    while a *shared* token bucket caps the global request rate, so raising worker
    count does not raise the rate. Retries on 429/5xx with exponential backoff,
    honouring Retry-After. Results come back through concurrent.futures.Future,
    so producers block on `.run(...)` like a normal call. In-memory only.
    """
    def __init__(self, rate: float = 20.0, burst: float | None = None,
                 workers: int = 4, max_retries: int = 4, base_backoff: float = 2.0):
        self.bucket = TokenBucket(rate=rate, burst=burst)
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self._q: "queue.Queue" = queue.Queue()
        self.n_calls = 0
        self._lock = threading.Lock()
        self._workers = [threading.Thread(target=self._run, name=f"reqq{i}", daemon=True)
                         for i in range(max(1, workers))]
        for w in self._workers:
            w.start()

    def _run(self):
        while True:
            job = self._q.get()
            if job is None:
                self._q.task_done()
                return
            call, fut, attempt = job
            try:
                self.bucket.acquire()
                with self._lock:
                    self.n_calls += 1
                res = call()
                if getattr(res, "status_code", 200) == 429 or getattr(res, "status_code", 200) >= 500:
                    raise requests.HTTPError(f"http {res.status_code}", response=res)
                if not fut.cancelled():
                    fut.set_result(res)
            except Exception as e:  # noqa: BLE001 - retry boundary
                resp = getattr(e, "response", None)
                code = getattr(resp, "status_code", None)
                ra = (getattr(resp, "headers", {}) or {})
                if code == 429:
                    # slow every worker, not just this one; prefer the server's
                    # Retry-After, else back off for a full TMDB window
                    try:
                        cool = float(ra.get("Retry-After", 0)) or 10.0
                    except (TypeError, ValueError):
                        cool = 10.0
                    self.bucket.penalise(max(cool, 5.0))
                elif code is not None and code >= 500:
                    self.bucket.penalise(2.0)
                if attempt >= self.max_retries:
                    if not fut.cancelled():
                        fut.set_exception(e)
                else:
                    backoff = self.base_backoff * (2 ** attempt)
                    try:
                        backoff = max(backoff, float(ra.get("Retry-After", 0)))
                    except (TypeError, ValueError):
                        pass
                    t = threading.Timer(backoff + random.uniform(0, 0.5),
                                        lambda j=(call, fut, attempt + 1): self._q.put(j))
                    t.daemon = True
                    t.start()
            finally:
                self._q.task_done()

    def submit(self, call):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put((call, fut, 0))
        return fut

    def run(self, call, timeout: float | None = 60):
        return self.submit(call).result(timeout=timeout)

    def close(self):
        for _ in self._workers:
            self._q.put(None)
        for w in self._workers:
            w.join(timeout=5)


class TMDB:
    def __init__(self, token: str | None = None, api_key: str | None = None,
                 rate: float | None = None, workers: int | None = None, store=None,
                 anidb=None):
        self.token = token or os.environ.get("TMDB_TOKEN")
        self.key = api_key or os.environ.get("TMDB_API_KEY")
        if not self.token and not self.key:
            raise RuntimeError("no TMDB credential (TMDB_TOKEN / TMDB_API_KEY)")
        # TMDB allows ~40 req/s per key; default to half of that.
        self.queue = RequestQueue(
            rate=float(rate if rate is not None else os.environ.get("TMDB_RATE", "20")),
            burst=float(os.environ.get("TMDB_BURST", "10")),
            workers=int(workers if workers is not None else os.environ.get("TMDB_WORKERS", "4")))
        self.store = store
        # Optional OFFLINE anime index (see anidb.py): used only as a fallback
        # when TMDB fails or matches weakly, and it makes no network calls.
        self.anidb = anidb
        self._cache: dict = {}

    def close(self):
        self.queue.close()

    @property
    def n_calls(self):
        return self.queue.n_calls

    def _get(self, path: str, **params):
        import requests
        url = f"https://api.themoviedb.org/3{path}"
        if self.token:
            headers = {"Authorization": f"Bearer {self.token}"}
        else:
            headers = {}
            params["api_key"] = self.key
        try:
            j = self.queue.run(lambda: requests.get(url, headers=headers, params=params, timeout=20)).json()
        except Exception:
            if self.store:
                self.store.log_fetch(path, ok=False)
            raise
        if self.store:
            self.store.log_fetch(path, ok=True)
        return j

    def identify(self, title: str, year: int | None, kind: str):
        key = (title.lower(), year, kind)
        if key in self._cache:
            return self._cache[key]

        qkey = query_key(title, year, kind)
        if self.store:
            hit = self.store.get_lookup(qkey)
            if hit == -1:
                self._cache[key] = None
                return None
            if hit:
                cached = self.store.get_media(kind, hit)
                # Only trust a cached identity scored by the CURRENT rule. One
                # written by an older (weaker) rule is re-verified once; the
                # re-scored record carries the marker, so the cost is one API
                # round per stale entry, once, not per run.
                if cached is not None and cached.get("_rule") == _SCORING_RULE:
                    self._cache[key] = cached
                    return cached

        out = self._identify_uncached(title, year, kind)
        # AniDB fallback, anime only. Trying a query against the (anime-only)
        # dump and finding nothing means "not anime", which is a free check; a
        # hit means the work IS anime and we get the romaji/AID that TMDB's
        # search often misses (English "Attack on Titan" -> "Shingeki no Kyojin").
        # Only used when TMDB failed or matched weakly, so TMDB stays primary.
        weak = out is None or (out.get("_match") or 0) < 0.60
        if weak and self.anidb is not None:
            hit = self.anidb.identify(title)
            if hit:
                aid, canonical = hit
                if out is None:
                    # no TMDB id at all: at least a stable identity + canonical
                    # anime title, so the file is named and grouped correctly.
                    # The AID is stored as a NEGATIVE synthetic id so the record
                    # round-trips through the lookup/media cache like any other
                    # (id 0/None would be written as a miss and lost next run).
                    # Consumers must not send a negative id to a provider.
                    out = {"id": -aid, "title": canonical, "year": None, "author": None,
                           "original_language": "ja", "genres": ["Animation"], "genre_ids": [16],
                           "kind": kind, "_anidb_aid": aid, "_anidb_title": canonical,
                           "_match": 0.75, "_rule": _SCORING_RULE}
                else:
                    out["_anidb_aid"] = aid
                    out["_anidb_title"] = canonical
        if self.store:
            self.store.put_lookup(qkey, kind, out["id"] if out else None)
        self._cache[key] = out
        return out

    def _identify_uncached(self, title, year, kind):
        ep = "/search/tv" if kind == "tv" else "/search/movie"
        year_field = "first_air_date_year" if kind == "tv" else "year"

        def search(q, **extra):
            params = {"query": q}
            params.update(extra)
            return self._get(ep, **params).get("results") or []

        # Try the title as written first, then progressively simpler forms.
        # Under-matching happens when the query is the whole filename stem, which
        # TMDB does not have; the cascade reduces it in bounded steps. The best
        # result across all steps is kept, not the first that returns anything, so
        # a specific query is never displaced by a vaguer one.
        ranked = []
        for q in query_cascade(title, kind):
            res = search(q, **({year_field: year} if year else {}))
            if not res and year:
                # a wrong year must not hide the title: retry unconstrained and
                # let the year filter below decide
                res = search(q)
            if not res:
                continue
            # Year and kind are hard filters; similarity only orders what survives.
            local = []
            for cand in res[:10]:
                name = cand.get("name") or cand.get("title") or ""
                yr = self._cand_year(cand, kind)
                if year and yr and abs(yr - year) > 1:
                    continue
                local.append((title_similarity(title, [name]), yr, cand))
            if not local:
                continue
            local.sort(key=lambda t: -t[0])
            ranked = local if not ranked else sorted(ranked + local, key=lambda t: -t[0])
            if local[0][0] >= 0.90:
                break                            # a precise match; stop asking
        if not ranked:
            return None
        ranked.sort(key=lambda t: -t[0])

        best = None
        # Score every candidate against ALL its localized titles, not just the
        # one the search happened to return. The search endpoint answers in one
        # language (English), so a query written in another language is compared
        # across languages and the wrong work wins: "Buffy contre les vampires"
        # (the French name of show 95) is a token superset of the English name of
        # the Season-8 spinoff, so the spinoff scored higher. Pulling the full
        # translations set makes the query match the correct localized name
        # exactly, whatever language it is in. Ranking order is also unreliable
        # once the query is non-English, so the top handful is scored, not the
        # first — #3 is where a correct match can hide (see the Buffy case: #2).
        for score, yr, cand in ranked[:5]:
            detail = self._get(f"/{'tv' if kind == 'tv' else 'movie'}/{cand['id']}",
                               append_to_response="credits,images,alternative_titles,translations")
            names = self._all_names(detail, kind)
            total = title_similarity(title, names)
            if best is None or total > best[0]:
                shaped = self._shape(detail, kind)
                shaped["kind"] = kind
                shaped["_match"] = round(total, 3)
                shaped["_rule"] = _SCORING_RULE
                best = (total, shaped)
            if best[0] >= 0.98:
                break                            # exact match; no need to look further
        if best is None:
            return None
        out = best[1]
        # Floor is deliberately low: only a catastrophic overlap is rejected.
        # The previous behaviour accepted the top hit with no check at all, so a
        # high floor would lose placements that currently resolve. The score is
        # recorded on the record instead, so weak identifications stay auditable.
        if best[0] < 0.40:
            return None
        if self.store:
            self.store.put_media(out)
        return out

    def _cand_year(self, cand, kind):
        date = cand.get("first_air_date") if kind == "tv" else cand.get("release_date")
        try:
            return int((date or "")[:4]) or None
        except ValueError:
            return None

    def episode_title(self, tv_id: int, season: int, episode: int):
        """(episode name, air year) for one instalment, cached in the store.

        Jellyfin's episode naming wants the episode TITLE, which the series-level
        identification does not carry — it is a separate endpoint. A NEGATIVE
        result is cached too (empty name): a season with no episode names, or a
        special that 404s, must not be re-requested on every run — no provider
        should be asked the same question twice.
        """
        if self.store:
            hit = self.store.get_episode(tv_id, season, episode)
            if hit is not None:
                return hit
        try:
            d = self._get(f"/tv/{tv_id}/season/{season}/episode/{episode}")
        except Exception:
            return (None, None)          # transient failure: do not cache as a miss
        name = (d or {}).get("name")
        air = ((d or {}).get("air_date") or "")[:4] or None
        if self.store:
            self.store.put_episode(tv_id, season, episode, name or "", air)
        return (name, air)

    @staticmethod
    def _all_names(detail, kind) -> list:
        """Every name TMDB reports: primary, all translations, alternates.

        `detail` must carry `translations` and `alternative_titles`.
        """
        names = [detail.get("name") or detail.get("title") or ""]
        for tr in ((detail.get("translations") or {}).get("translations") or []):
            one = (tr.get("data") or {}).get("name")
            if one:
                names.append(one)
        names += TMDB._alt_titles(detail, kind)
        return names

    @staticmethod
    def _alt_titles(detail, kind) -> list:
        """Alternative and original titles reported by TMDB."""
        out = []
        alts = detail.get("alternative_titles") or {}
        for t in (alts.get("titles") if kind == "movie" else alts.get("results")) or []:
            if t.get("title"):
                out.append(t["title"])
        for one in (detail.get("original_name"), detail.get("original_title")):
            if one:
                out.append(one)
        return out

    @staticmethod
    def _shape(d, kind) -> dict:
        genres = [g.get("name") for g in d.get("genres") or []]
        genre_ids = [g.get("id") for g in d.get("genres") or []]
        if kind == "tv":
            creatives = [c.get("name") for c in d.get("created_by") or []]
            studios = [c.get("name") for c in d.get("production_companies") or []]
            return {"id": d["id"], "title": d.get("name"), "year": (d.get("first_air_date") or "")[:4] or None,
                    "last_year": (d.get("last_air_date") or "")[:4] or None,
                    "original_language": d.get("original_language"), "genres": genres, "genre_ids": genre_ids,
                    "author": (studios or creatives or [None])[0], "creatives": creatives, "studios": studios}
        crew = (d.get("credits") or {}).get("crew") or []
        directors = [c["name"] for c in crew if c.get("job") == "Director"]
        studios = [c.get("name") for c in d.get("production_companies") or []]
        return {"id": d["id"], "title": d.get("title"),
                "year": (d.get("release_date") or "")[:4] or None,
                "original_language": d.get("original_language"), "genres": genres, "genre_ids": genre_ids,
                "author": (directors or studios or [None])[0], "directors": directors, "studios": studios}


if __name__ == "__main__":
    import sys
    sample = sys.argv[1:] or [
        "Ghost.in.the.Shell.1995.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.5.1-FGT.mkv",
        "[SubsPlease] Frieren - 12v2 (1080p) [A1B2C3D4].mkv",
        "Blade Runner 2049 (2017) [BluRay 1080p x265].mkv",
        "Extra/Interview with the Director.mkv",
        "Series/Show Name (2019)/Season 02/Show.Name.S02E05.1080p.WEB-DL.mkv",
    ]
    for s in sample:
        p = parse_name(s)
        print(json.dumps(p, ensure_ascii=False), "<-", s)
