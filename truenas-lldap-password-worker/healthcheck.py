#!/usr/bin/env python3
"""Container healthcheck: fetch the login page. Exits non-zero on failure."""

from __future__ import annotations

import os
import sys
import urllib.request

__all__ = ["main"]


def main() -> int:
    listen = os.environ.get("PW_LISTEN", "0.0.0.0:8099")
    port = listen.rsplit(":", 1)[-1] if ":" in listen else "8099"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            r.read(1)
    except Exception as e:
        print(f"healthcheck failed: {type(e).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
