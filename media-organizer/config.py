#!/usr/bin/env python3
"""Configuration — every host-specific value comes from the environment.

Nothing here names a machine, a mount point or a pool: the toolchain is meant to
run anywhere the sources and the library are reachable.

Inputs (all optional, with defaults):

    MEDIA_ROOT        mount holding the sources and the library  [/mnt/media]
    MEDIA_LIBRARY     where the organised tree is written
                      [$MEDIA_ROOT/MediaLibrary]
    MEDIA_SOURCES     PATHSEP-separated source roots, overriding MEDIA_LAYOUT
    MEDIA_LAYOUT      comma-separated source directory names under MEDIA_ROOT
    MEDIA_OVERRIDES   forced classifications (see overrides.py)
    MEDIA_STATE_DB    sqlite state file
    MEDIA_TMDB_ENV    credential file
    REFLINK_LOCK      clone lock file
    REFLINK_SETTLE    seconds between clones  [0.4]

Where state lives
-----------------
`XDG_*` is a freedesktop convention, so it is honoured only where it means
something: on Linux, or wherever it is explicitly set. macOS and BSD get their
own conventions, Windows gets `LOCALAPPDATA`, and a system account (root, or an
account with no writable home) gets a uid-scoped directory under the temp dir
instead of a home-directory path it cannot own.

Precedence for each purpose:

  1. an explicit override (e.g. MEDIA_STATE_DB, REFLINK_LOCK)
  2. `XDG_*`, when set to an absolute path (empty counts as unset)
  3. the platform convention
  4. uid-scoped temp dir for system accounts

Placements are durable and not regenerable, so local state defaults to a *state*
directory rather than a cache directory. Every path resolves once, lazily: a
later change to the environment cannot shift it mid-run.
"""
from __future__ import annotations
import os
import sys

DEFAULT_MEDIA_ROOT = "/mnt/media"
DEFAULT_LAYOUT = ("Films", "CleanFilms", "Movies", "Series", "CleanSeries",
                  "SeriesCollec", "FilmsCollec")
DEFAULT_CATEGORIES = ("FeatureFilms", "Series", "AnimeFilms", "AnimeSeries",
                      "Animation", "ShortFilms", "Experimental", "_JUNK_")

APP_NAME = "media-organizer"
_cached: dict = {}


def _cached_get(key: str, produce):
    if key not in _cached:
        _cached[key] = produce()
    return _cached[key]


def _env_path(name: str) -> str | None:
    """An environment variable as an absolute path, or None.

    Empty counts as unset; relative values are ignored; `~` is expanded.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    expanded = os.path.expanduser(raw)
    return expanded if os.path.isabs(expanded) else None


def _platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("darwin"):
        return "macos"
    if sys.platform.startswith(("freebsd", "openbsd", "netbsd", "dragonfly")):
        return "bsd"
    return "linux"


def _is_system_account() -> bool:
    """Root, or an account with no usable home directory to write into."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    home = os.path.expanduser("~")
    return not home or home == "~" or not os.path.isdir(home)


def _temp_dir() -> str:
    return os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp"


def _base_dir(kind: str) -> str:
    """Directory that holds our config/cache/state/runtime files."""
    def produce():
        xdg = {"config": "XDG_CONFIG_HOME", "cache": "XDG_CACHE_HOME",
               "state": "XDG_STATE_HOME", "runtime": "XDG_RUNTIME_DIR"}[kind]
        explicit = _env_path(xdg)
        if explicit and (kind != "runtime" or os.path.isdir(explicit)):
            return explicit

        if _is_system_account():
            uid = os.getuid() if hasattr(os, "getuid") else "user"
            return os.path.join(_temp_dir(), f"{APP_NAME}-{uid}")

        plat = _platform()
        home = os.path.expanduser("~")
        if plat == "macos":
            if kind == "cache":
                return os.path.join(home, "Library", "Caches")
            if kind == "runtime":
                return _temp_dir()
            return os.path.join(home, "Library", "Application Support")
        if plat == "windows":
            if kind == "config":
                return os.environ.get("APPDATA") or home
            if kind == "runtime":
                return _temp_dir()
            return os.environ.get("LOCALAPPDATA") or home
        # linux / bsd dotfile convention when XDG is unset
        if kind == "config":
            return os.path.join(home, ".config")
        if kind == "cache":
            return os.path.join(home, ".cache")
        if kind == "state":
            return os.path.join(home, ".local", "state")
        return _temp_dir()

    return _cached_get("base_" + kind, produce)


def app_dir(kind: str = "state") -> str:
    """Our own directory under the resolved base, created on demand.

    kind: 'config' | 'cache' | 'state' | 'runtime'
    """
    path = os.path.join(_base_dir(kind), APP_NAME)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------- media paths
def media_root() -> str:
    return _cached_get(
        "media_root",
        lambda: os.path.abspath(os.path.expanduser(
            os.environ.get("MEDIA_ROOT") or DEFAULT_MEDIA_ROOT)))


def media_library() -> str:
    return _cached_get(
        "media_library",
        lambda: os.path.abspath(os.path.expanduser(
            os.environ.get("MEDIA_LIBRARY") or os.path.join(media_root(), "MediaLibrary"))))


def layout_names() -> tuple:
    env = os.environ.get("MEDIA_LAYOUT")
    if env:
        return tuple(p.strip() for p in env.split(",") if p.strip())
    return DEFAULT_LAYOUT


def category_names() -> tuple:
    env = os.environ.get("MEDIA_CATEGORIES")
    if env:
        return tuple(p.strip() for p in env.split(",") if p.strip())
    return DEFAULT_CATEGORIES


def default_sources() -> list:
    """Source roots: explicit MEDIA_SOURCES, else the conventional layout."""
    def produce():
        env = os.environ.get("MEDIA_SOURCES")
        if env:
            return [os.path.abspath(os.path.expanduser(p))
                    for p in env.split(os.pathsep) if p.strip()]
        return [os.path.join(media_root(), name) for name in layout_names()]
    return list(_cached_get("sources", produce))


# ---------------------------------------------------------------- local state
def state_db() -> str:
    return _cached_get(
        "state_db",
        lambda: os.path.abspath(os.path.expanduser(
            os.environ.get("MEDIA_STATE_DB")
            or os.path.join(app_dir("state"), "state.db"))))


def tmdb_env_file() -> str:
    return _cached_get(
        "tmdb_env",
        lambda: os.path.abspath(os.path.expanduser(
            os.environ.get("MEDIA_TMDB_ENV")
            or os.path.join(app_dir("config"), "tmdb.env"))))


def clone_lock_path() -> str:
    return _cached_get(
        "clone_lock",
        lambda: os.path.abspath(os.path.expanduser(
            os.environ.get("REFLINK_LOCK")
            or os.path.join(app_dir("runtime"), "clone.lock"))))


def overrides_file() -> str:
    return _cached_get(
        "overrides",
        lambda: os.path.abspath(os.path.expanduser(
            os.environ.get("MEDIA_OVERRIDES")
            or os.path.join(app_dir("config"), "overrides.yaml"))))


# ---------------------------------------------------------------- VLM (vision)
# The title-reading workers use a local vision-language model served on an
# OpenAI-compatible endpoint. That endpoint is a *host-specific* value, so it is
# required from the environment — no default is baked in. See README.
def vlm_endpoint() -> str:
    """OpenAI-compatible chat-completions URL. Required (no default)."""
    return _cached_get(
        "vlm_endpoint",
        lambda: os.environ.get("VLM_ENDPOINT", "").strip())


def vlm_model() -> str:
    """Model id to request at the endpoint. Optional — a server that hosts a
    single model (LM Studio, llama.cpp) needs none; leave VLM_MODEL unset then."""
    return _cached_get(
        "vlm_model",
        lambda: os.environ.get("VLM_MODEL", "").strip())


def vlm_conf() -> tuple[str, str]:
    """(endpoint, model). Only the endpoint is required; the model may be ''."""
    ep = vlm_endpoint()
    if not ep:
        raise RuntimeError(
            "the VLM workers need VLM_ENDPOINT in the environment, e.g. "
            "VLM_ENDPOINT=http://host:port/v1/chat/completions "
            "(VLM_MODEL is optional; a single-model server can omit it)")
    return ep, vlm_model()


def clone_settle_seconds() -> float:
    try:
        return float(os.environ.get("REFLINK_SETTLE", "0.4"))
    except (TypeError, ValueError):
        return 0.4


def reset() -> None:
    """Forget resolved values (tests, or after changing the environment)."""
    _cached.clear()


def describe() -> str:
    lines = [
        f"platform    : {_platform()}{' (system account)' if _is_system_account() else ''}",
        f"config dir  : {app_dir('config')}",
        f"state dir   : {app_dir('state')}",
        f"runtime dir : {app_dir('runtime')}",
        "",
        f"media root  : {media_root()}",
        f"library     : {media_library()}",
        f"state db    : {state_db()}",
        f"tmdb env    : {tmdb_env_file()}",
        f"overrides   : {overrides_file()}"
        + ("  (present)" if os.path.exists(overrides_file()) else "  (none)"),
        f"clone lock  : {clone_lock_path()}",
        f"clone settle: {clone_settle_seconds():g}s",
        "",
        "sources:",
    ]
    for s in default_sources():
        lines.append(("  ok  " if os.path.isdir(s) else "  --  ") + s)
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
