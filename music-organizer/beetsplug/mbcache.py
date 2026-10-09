"""beets plugin: persist MusicBrainz API responses to SQLite.

Why
---
beets has no disk cache for MusicBrainz. `musicbrainzngs`/`beetsplug._utils.
musicbrainz` keep at most an in-process `cached_property` on the API helper,
and beets' own resilience is the `incremental` *import history*, which skips
directories wholesale but re-queries MusicBrainz in full for anything that is
re-imported — e.g. after a failed or interrupted run, or for the same release
appearing under two source trees (a FLAC and an MP3 copy of one album).

Every API call goes through one method:

    MusicBrainzAPI._get_resource(resource, includes=None, **kwargs)
        -> self.get_json(f"{api_root}/{resource}", params=...)

so wrapping that single method caches every search, lookup and browse.

Notes
-----
* Only *successful* payloads are cached. Errors (including 404) propagate
  uncached, so a transient failure is never remembered as "no such release".
* The cache key is the resource path plus the canonicalised query parameters,
  so a re-request with different `inc=`/`limit=` is a different entry.
* Failures of the cache itself are non-fatal: on any sqlite error the plugin
  logs a warning and transparently falls back to live requests. Losing the
  cache must never break an import.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from hashlib import sha256

from beets.plugins import BeetsPlugin

DEFAULT_CACHE = "/mnt/largepool/bulk/MediaLibrary/.music-organizer/mbcache.db"


def _canonical(resource: str, includes, kwargs: dict) -> str:
    """Stable cache key for one API request."""
    params = {k: v for k, v in kwargs.items()}
    if includes:
        params["inc"] = "+".join(includes)
    blob = json.dumps(
        [resource, sorted(params.items(), key=lambda kv: kv[0])],
        default=str,
        ensure_ascii=False,
    )
    return sha256(blob.encode("utf-8")).hexdigest()


class MBCachePlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "cache": DEFAULT_CACHE,
                "enabled": True,
            }
        )
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._path: str | None = None
        self._hits = 0
        self._misses = 0

        if self.config["enabled"].get(bool):
            self._install()

    # ---------------------------------------------------------------- plumbing
    def _connection(self) -> sqlite3.Connection:
        """Open (once) and return the cache connection."""
        path = str(self.config["cache"].as_str())
        if self._conn is None or path != self._path:
            conn = sqlite3.connect(
                path, timeout=30.0, check_same_thread=False
            )
            conn.execute("pragma journal_mode=WAL")
            conn.execute("pragma synchronous=NORMAL")
            conn.execute(
                "create table if not exists mb_response ("
                "  key text primary key,"
                "  resource text not null,"
                "  fetched real not null default (julianday('now')),"
                "  body text not null"
                ")"
            )
            conn.commit()
            self._conn = conn
            self._path = path
            self._log.info("musicbrainz cache: {}", path)
        return self._conn

    def _install(self) -> None:
        """Wrap the API's single request choke point."""
        try:
            from beetsplug._utils.musicbrainz import MusicBrainzAPI
        except Exception as exc:  # pragma: no cover - import layout change
            self._log.warning(
                "musicbrainz cache disabled: cannot import MusicBrainzAPI "
                "({})",
                exc,
            )
            return

        plugin = self
        original = MusicBrainzAPI._get_resource

        def cached_get_resource(self, resource, includes=None, **kwargs):
            key = _canonical(resource, includes, kwargs)
            try:
                conn = plugin._connection()
                with plugin._lock:
                    row = conn.execute(
                        "select body from mb_response where key = ?", (key,)
                    ).fetchone()
                if row is not None:
                    plugin._hits += 1
                    return json.loads(row[0])
            except Exception as exc:
                plugin._log.warning(
                    "musicbrainz cache read failed ({}); going live", exc
                )

            # Miss (or cache failure): hit the network.
            payload = original(self, resource, includes=includes, **kwargs)
            plugin._misses += 1
            try:
                conn = plugin._connection()
                with plugin._lock:
                    conn.execute(
                        "insert or replace into mb_response "
                        "(key, resource, body) values (?, ?, ?)",
                        (key, resource, json.dumps(payload, ensure_ascii=False)),
                    )
                    conn.commit()
            except Exception as exc:
                plugin._log.warning(
                    "musicbrainz cache write failed ({}); not cached", exc
                )
            return payload

        MusicBrainzAPI._get_resource = cached_get_resource
        self._log.info("musicbrainz cache: wrapping _get_resource")

    def stats(self) -> tuple[int, int]:
        """Return (hits, misses) for this process."""
        return self._hits, self._misses
