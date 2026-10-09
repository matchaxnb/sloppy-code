#!/usr/bin/env python3
"""Offline AniDB title resolution from the daily `anime-titles.dat.gz` dump.

AniDB's HTTP API forbids the traffic a library scan would generate: it needs a
registered client, allows one request per two seconds, and states that asking
for the same dataset more than once a day can get a client BANNED. A per-title
lookup is therefore the wrong shape entirely.

Instead we use the dump AniDB publishes for exactly this purpose — every title
spelling mapped to an AID, refreshed daily. It is downloaded at most once per
day and read locally, so a scan performs **no** AniDB requests at all: strictly
offline, and no ban risk.

The dump format is `<aid>|<type>|<language>|<title>`:
  type 1 = primary title (one per anime)
  type 2 = synonym
  type 3 = short title
  type 4 = official title (one per language; language column names it)
  type 5 = kana reading
  type 6 = title card

Resolution is title -> AID, by exact or near match over every spelling. It is a
fallback for ANIME only; TMDB stays the primary provider.
"""
from __future__ import annotations
import gzip, os, re, time, urllib.request

DUMP_URL = "https://anidb.net/api/anime-titles.dat.gz"
# AniDB blocks clients that do not identify themselves; a descriptive UA is
# required, and the download must not look like a crawler.
USER_AGENT = "media-organizer/0.1 (+https://github.com/)"
REFRESH_SECONDS = 24 * 3600          # never re-request within a day (ban trigger)
_TYPE_PRIMARY, _TYPE_SYNONYM, _TYPE_SHORT, _TYPE_OFFICIAL = 1, 2, 3, 4


def default_cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "media-organizer", "anime-titles.dat.gz")


def _norm(text: str) -> str:
    """Fold a title for comparison: case, punctuation, spacing."""
    s = re.sub(r"[^\w\s]", " ", (text or "").lower())
    return re.sub(r"\s+", " ", s).strip()


class AniDB:
    """Local title index. Construct once; `identify` is then pure lookups."""

    def __init__(self, path: str | None = None, auto_download: bool = True):
        self.path = path or default_cache_path()
        self.title_to_aids: dict[str, set] = {}
        self.aid_titles: dict[int, dict] = {}     # aid -> {primary, official, synonyms}
        self.loaded = False
        self._maybe_refresh(auto_download)
        self._load()

    # ---------------- fetch (at most daily) ----------------
    def _maybe_refresh(self, auto: bool) -> None:
        if os.path.exists(self.path) and time.time() - os.path.getmtime(self.path) < REFRESH_SECONDS:
            return                    # fresh enough; do NOT touch the network
        if not auto:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            req = urllib.request.Request(DUMP_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=180) as r, open(self.path + ".tmp", "wb") as fh:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
            os.replace(self.path + ".tmp", self.path)
        except Exception:
            # A failed refresh keeps any older dump and never blocks a scan.
            pass

    # ---------------- load ----------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with gzip.open(self.path, "rt", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    if not ln or ln.startswith("#"):
                        continue
                    parts = ln.rstrip("\n").split("|", 3)
                    if len(parts) != 4:
                        continue
                    aid, typ, lang, title = parts
                    try:
                        aid = int(aid); typ = int(typ)
                    except ValueError:
                        continue
                    title = title.strip()
                    if not title:
                        continue
                    self.aid_titles.setdefault(aid, {"primary": None, "official": None, "synonyms": []})
                    rec = self.aid_titles[aid]
                    if typ == _TYPE_PRIMARY:
                        rec["primary"] = rec["primary"] or title
                    elif typ == _TYPE_OFFICIAL and lang in ("en", "ja", "x-jat"):
                        rec["official"] = rec["official"] or title
                    else:
                        rec["synonyms"].append(title)
                    self.title_to_aids.setdefault(_norm(title), set()).add(aid)
        except (OSError, gzip.BadGzipFile):
            return
        self.loaded = bool(self.aid_titles)

    # ---------------- query (offline) ----------------
    def identify(self, title: str):
        """(aid, canonical title) for a title, or None. Offline, exact then relaxed.

        A title the dump does not carry cannot be resolved — we do not fall back
        to the network, because the whole point of the dump is to avoid it.
        """
        if not self.loaded or not title:
            return None
        key = _norm(title)
        aids = self.title_to_aids.get(key)
        if aids:
            aid = sorted(aids)[0]
            return aid, self._canonical(aid)
        # a leading article or a trailing '(season N)' is noise the dump may not
        # store; try one relaxed form before giving up
        relaxed = re.sub(r"\s*\((?:season|part)\s*\d+\)$", "", key)
        relaxed = re.sub(r"^(the|a|an)\s+", "", relaxed)
        aids = self.title_to_aids.get(relaxed)
        if aids:
            aid = sorted(aids)[0]
            return aid, self._canonical(aid)
        return None

    def _canonical(self, aid: int) -> str:
        rec = self.aid_titles.get(aid) or {}
        return rec.get("primary") or rec.get("official") or (rec.get("synonyms") or [""])[0]
