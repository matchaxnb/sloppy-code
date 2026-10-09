"""Drop-in `reflink` module for beets, implemented with the FICLONE ioctl.

Why this exists
---------------
beets' ``import.reflink`` option calls
``beets.util.reflink()``, which does ``import_module("reflink").reflink(src,
dst)`` -- i.e. it expects the PyPI package ``reflink``. That package clones
and then *also* copies mode/ownership/times (``copystat``). On a ZFS dataset
with ``aclmode=restricted`` (the ``largepool/bulk`` layout) that second step
fails with ``EPERM``:

    OSError: Could not copy permissions (errno EPERM)

Consequences without this shim:
  * ``reflink: yes``  -> every import aborts with a FilesystemError;
  * ``reflink: auto`` -> the clone succeeds, the copystat fails, beets
    suppresses the exception and silently falls back to a *byte copy*
    (``beets.util.reflink(..., fallback=True)``), so you pay full space and
    never notice.

This module performs the clone and nothing else, so its semantics match
``cp --reflink=always`` exactly: shared blocks, no metadata copying, real
failure (never a silent full copy) if the clone cannot be made.

Install: put this directory on ``PYTHONPATH`` (see ``run-*.sh``) so it
shadows any installed ``reflink`` distribution. It has no dependencies.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

# FICLONE: _IOW(0x94, 9, int), i.e. 0x40049409 on Linux.
# The size in the ioctl number is the size of the *argument* type (int),
# not of anything on disk.
FICLONE = 0x40049409

__all__ = ["reflink", "supported_at", "FICLONE"]


def reflink(oldpath, newpath) -> None:
    """Clone ``oldpath`` to ``newpath`` via FICLONE.

    ``newpath`` is created (truncating an existing file). Raises ``OSError``
    if the filesystem does not support cloning or the files are on different
    filesystems -- never falls back to a byte copy.
    """
    oldpath = os.fspath(oldpath)
    newpath = os.fspath(newpath)

    # Same file: nothing to do (beets guards this too, but be explicit).
    if os.path.exists(newpath) and os.path.samefile(oldpath, newpath):
        return

    # Source read-only, destination writable: FICLONE requires exactly that.
    with open(oldpath, "rb") as src, open(newpath, "wb") as dst:
        fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())


def supported_at(path) -> bool:
    """Return whether ``FICLONE`` works for a directory (or file) at ``path``.

    Probes by cloning a zero-byte file into the same directory; the probe
    file is always removed. Used by tests; beets itself does not call it.
    """
    path = Path(os.fspath(path))
    if not path.is_dir():
        path = path.parent
    probe_src = path / ".reflink_probe_src"
    probe_dst = path / ".reflink_probe_dst"
    try:
        probe_src.write_bytes(b"")
        reflink(probe_src, probe_dst)
        return True
    except OSError:
        return False
    finally:
        for p in (probe_src, probe_dst):
            try:
                p.unlink()
            except OSError:
                pass
