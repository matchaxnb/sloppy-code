#!/usr/bin/env python3
"""Durable store for media-organizer: metadata cache, probe cache, placements.

SQLite is not multi-thread friendly, so all writes funnel through **one writer
thread** behind an in-memory queue, flushed in batched transactions. Readers open
their own thread-local connection (WAL permits concurrent reads while a write
transaction is open), and worker threads never issue a write themselves.

Tables
  lookup     query key -> resolved TMDB id (or a recorded miss)
  media      shaped TMDB payloads keyed by (kind, tmdb_id)
  probe      container features keyed by (path, size, mtime)
  placement  immutable source path -> destination path
  fetchlog   one row per API call actually made
"""
from __future__ import annotations
import json, os, queue, re, sqlite3, threading, time

SCHEMA_VERSION = 2


def query_key(title: str, year, kind: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return f"{kind}|{t}|{year or ''}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lookup (
    qkey      TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    tmdb_id   INTEGER,
    miss      INTEGER NOT NULL DEFAULT 0,
    resolved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    kind      TEXT NOT NULL,
    tmdb_id   INTEGER NOT NULL,
    title     TEXT,
    year      TEXT,
    orig_lang TEXT,
    author    TEXT,
    payload   TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (kind, tmdb_id)
);
CREATE TABLE IF NOT EXISTS fetchlog (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint  TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    ts        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS probe (
    path      TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    mtime     INTEGER NOT NULL,
    features  TEXT NOT NULL,
    ts        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS placement (
    src       TEXT PRIMARY KEY,
    src_size  INTEGER NOT NULL,
    dest      TEXT NOT NULL,
    kind      TEXT,
    title     TEXT,
    category  TEXT,
    media_id  TEXT,
    ts        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS episode (
    tmdb_id   INTEGER NOT NULL,
    season    INTEGER NOT NULL,
    episode   INTEGER NOT NULL,
    title     TEXT,
    air_year  TEXT,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (tmdb_id, season, episode)
);
CREATE TABLE IF NOT EXISTS alternative (
    kept_src  TEXT NOT NULL,
    src       TEXT NOT NULL,
    score     REAL,
    size      INTEGER,
    reason    TEXT,
    ts        REAL NOT NULL,
    PRIMARY KEY (kept_src, src)
);
"""

# upsert sql per table, keyed by op name
SQL = {
    "lookup": ("INSERT INTO lookup(qkey,kind,tmdb_id,miss,resolved_at) VALUES(?,?,?,?,?) "
               "ON CONFLICT(qkey) DO UPDATE SET tmdb_id=excluded.tmdb_id, miss=excluded.miss, "
               "resolved_at=excluded.resolved_at"),
    "media": ("INSERT INTO media(kind,tmdb_id,title,year,orig_lang,author,payload,fetched_at) "
              "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(kind,tmdb_id) DO UPDATE SET "
              "title=excluded.title, year=excluded.year, orig_lang=excluded.orig_lang, "
              "author=excluded.author, payload=excluded.payload, fetched_at=excluded.fetched_at"),
    "fetchlog": "INSERT INTO fetchlog(endpoint,ok,ts) VALUES(?,?,?)",
    "probe": ("INSERT INTO probe(path,size,mtime,features,ts) VALUES(?,?,?,?,?) "
              "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
              "features=excluded.features, ts=excluded.ts"),
    "placement": ("INSERT INTO placement(src,src_size,dest,kind,title,category,media_id,ts) "
                  "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(src) DO UPDATE SET "
                  "src_size=excluded.src_size, dest=excluded.dest, kind=excluded.kind, "
                  "title=excluded.title, category=excluded.category, media_id=excluded.media_id, ts=excluded.ts"),
    "episode": ("INSERT INTO episode(tmdb_id,season,episode,title,air_year,fetched_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(tmdb_id,season,episode) DO UPDATE SET "
                "title=excluded.title, air_year=excluded.air_year, fetched_at=excluded.fetched_at"),
    "alternative": ("INSERT INTO alternative(kept_src,src,score,size,reason,ts) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(kept_src,src) DO UPDATE SET "
                    "score=excluded.score, size=excluded.size, reason=excluded.reason, ts=excluded.ts"),
}


class Store:
    """Single-writer, batched, thread-safe store."""

    def __init__(self, path: str, batch: int = 256, flush_interval: float = 0.5):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.path = path
        self.batch = batch
        self.flush_interval = flush_interval
        self._local = threading.local()
        self._wq: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._outstanding = 0
        self.db.executescript(SCHEMA)
        self._init_meta()
        self._writer = threading.Thread(target=self._writer_loop, name="store-writer", daemon=True)
        self._writer.start()

    # ---------------- connections ----------------
    @property
    def db(self):
        """Thread-local connection. WAL allows many readers alongside one writer."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=60000")
            self._local.conn = c
        return c

    def _init_meta(self):
        row = self.db.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta(k,v) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        elif int(row["v"]) != SCHEMA_VERSION:
            # Changes so far are additive (CREATE TABLE IF NOT EXISTS runs on
            # every open, so any new table already exists by now). An OLDER
            # stored version is therefore upgraded in place, not recreated — the
            # lookup/media cache is the expensive part and must survive. A NEWER
            # version means the code is older than the data; refuse that.
            if int(row["v"]) > SCHEMA_VERSION:
                raise RuntimeError(f"store schema {row['v']} > code {SCHEMA_VERSION}; upgrade the code")
            self.db.execute("UPDATE meta SET v=? WHERE k='schema_version'", (str(SCHEMA_VERSION),))

    # ---------------- writer thread ----------------
    def _writer_loop(self):
        conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        pending = []
        last = time.monotonic()
        while True:
            timeout = max(0.01, self.flush_interval - (time.monotonic() - last))
            try:
                pending.append(self._wq.get(timeout=timeout))
            except queue.Empty:
                pass
            drained = len(pending) >= self.batch or (pending and self._wq.empty()
                                                     and time.monotonic() - last >= self.flush_interval)
            if pending and drained:
                n = len(pending)
                try:
                    conn.execute("BEGIN")
                    for op, args in pending:
                        conn.execute(SQL[op], args)
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                pending = []
                last = time.monotonic()
                with self._lock:
                    self._outstanding -= n
            if self._stop.is_set() and self._wq.empty() and not pending:
                conn.close()
                return

    def _enqueue(self, op: str, args: tuple):
        with self._lock:
            self._outstanding += 1
        self._wq.put((op, args))

    def flush(self, timeout: float = 30.0):
        """Block until queued writes have been committed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._outstanding == 0:
                    return
            time.sleep(0.02)

    # ---------------- writes (queued) ----------------
    def put_lookup(self, qkey: str, kind: str, tmdb_id):
        self._enqueue("lookup", (qkey, kind, tmdb_id, 0 if tmdb_id else 1, time.time()))

    def put_media(self, meta: dict):
        self._enqueue("media", (meta["kind"], meta["id"], meta.get("title"), meta.get("year"),
                                meta.get("original_language"), meta.get("author"),
                                json.dumps(meta, ensure_ascii=False), time.time()))

    def log_fetch(self, endpoint: str, ok: bool):
        self._enqueue("fetchlog", (endpoint, 1 if ok else 0, time.time()))

    def put_probe(self, path: str, size: int, mtime: int, features: dict):
        self._enqueue("probe", (path, size, mtime, json.dumps(features, ensure_ascii=False), time.time()))

    def put_placement(self, src: str, src_size: int, dest: str, kind=None,
                      title=None, category=None, media_id=None):
        self._enqueue("placement", (src, src_size, dest, kind, title, category,
                                    str(media_id) if media_id else None, time.time()))

    def put_episode(self, tmdb_id: int, season: int, episode: int, title, air_year=None):
        self._enqueue("episode", (tmdb_id, season, episode, title, air_year, time.time()))

    def put_alternative(self, kept_src: str, src: str, score=None, size=None, reason=None):
        self._enqueue("alternative", (kept_src, src, score, size, reason, time.time()))

    # ---------------- reads ----------------
    def get_alternatives(self, kept_src: str) -> list:
        return [dict(r) for r in self.db.execute(
            "SELECT src, score, size, reason FROM alternative WHERE kept_src=? ORDER BY score DESC",
            (kept_src,))]

    def get_episode(self, tmdb_id: int, season: int, episode: int):
        r = self.db.execute("SELECT title, air_year FROM episode WHERE tmdb_id=? AND season=? AND episode=?",
                            (tmdb_id, season, episode)).fetchone()
        return (r["title"], r["air_year"]) if r else None

    def get_lookup(self, qkey: str):
        """None = never looked up; -1 = looked up, no match; else the tmdb id."""
        r = self.db.execute("SELECT tmdb_id, miss FROM lookup WHERE qkey=?", (qkey,)).fetchone()
        if r is None:
            return None
        return -1 if r["miss"] else r["tmdb_id"]

    def get_media(self, kind: str, tmdb_id: int):
        r = self.db.execute("SELECT payload FROM media WHERE kind=? AND tmdb_id=?", (kind, tmdb_id)).fetchone()
        return json.loads(r["payload"]) if r else None

    def get_probe(self, path: str, size: int, mtime: int):
        r = self.db.execute("SELECT size, mtime, features FROM probe WHERE path=?", (path,)).fetchone()
        if r is None or r["size"] != size or r["mtime"] != mtime:
            return None
        return json.loads(r["features"])

    def dest_use_counts(self) -> dict:
        """How many sources already point at each destination.

        A count above 1 means an earlier run collapsed several sources onto one
        path, so that clone cannot be moved to serve any single one of them —
        the caller must re-clone instead. Computed from the whole table rather
        than from one plan, because the streaming path plans a single group at a
        time and would never see the other claimants.
        """
        out: dict = {}
        for (dest, n) in self.db.execute("SELECT dest, COUNT(*) FROM placement GROUP BY dest"):
            if n > 1:
                out[dest] = n
        return out

    def get_placement(self, src: str):
        r = self.db.execute("SELECT dest, src_size FROM placement WHERE src=?", (src,)).fetchone()
        return (r["dest"], r["src_size"]) if r else None

    def stats(self) -> dict:
        g = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {"lookup": g("SELECT COUNT(*) FROM lookup"),
                "media": g("SELECT COUNT(*) FROM media"),
                "probe": g("SELECT COUNT(*) FROM probe"),
                "placement": g("SELECT COUNT(*) FROM placement"),
                "fetches": g("SELECT COUNT(*) FROM fetchlog")}

    def close(self, timeout: float = 30.0):
        self.flush(timeout)
        self._stop.set()
        self._writer.join(timeout=timeout)
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
