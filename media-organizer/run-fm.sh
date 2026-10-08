#!/usr/bin/env bash
# Start the reflink arranger UI.
#
# Everything is environment-sourced (see config.py); nothing is host-specific.
# Common overrides:
#   MEDIA_ROOT        media mount            (default: /mnt/media)
#   MEDIA_LIBRARY     writable library root  (default: $MEDIA_ROOT/MediaLibrary)
#   MEDIA_FM_HOST     bind address           (default: 127.0.0.1)
#   MEDIA_FM_PORT     port                   (default: 8099)
#
# The UI is unauthenticated and can write inside the library, so it binds to
# loopback by default. Expose it deliberately (e.g. MEDIA_FM_HOST=0.0.0.0) and
# only on a trusted network.
cd "$(dirname "$0")"
exec .venv/bin/python filemanager.py \
    --host "${MEDIA_FM_HOST:-127.0.0.1}" \
    --port "${MEDIA_FM_PORT:-8099}"
