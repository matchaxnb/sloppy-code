#!/usr/bin/env bash
# Full streaming apply: identify the source corpus and reflink it into the library.
#
# Everything is environment-sourced (see config.py); nothing is host-specific.
#   MEDIA_ROOT        media mount            (default: /mnt/media)
#   MEDIA_LIBRARY     library root           (default: $MEDIA_ROOT/MediaLibrary)
#   MEDIA_SOURCES     PATHSEP list of source roots (default: layout under MEDIA_ROOT)
#   MEDIA_STATE_DB    sqlite state           (default: $XDG_STATE_HOME/...)
#   TMDB_TOKEN / TMDB_API_KEY   credential (or put them in MEDIA_TMDB_ENV)
#
#   tmux new-session -d -s mapply "$PWD/run-apply.sh > /tmp/apply.log 2>&1"
#
# Resumable: TMDB answers, probe results and placements are all persisted, so a
# re-run skips finished work (0 API calls, no re-probing).
#
# Resource posture: the pool is shared, and reflinks are serialized on purpose.
#   copy  ~ one at a time (`clone_serialized`), settle gap between clones: a
#           block-clone updates the block-reference table and must be committed
#           by a txg, so concurrency multiplies pressure for no throughput gain
#   probe ~ `PROBE_WORKERS` concurrent container reads (IO heavy, CPU light)
# Raise probe workers only when nothing else needs the pool.
cd "$(dirname "$0")"

# Load credentials if a file is configured and the values are not already set.
if [ -n "${MEDIA_TMDB_ENV:-}" ] && [ -r "${MEDIA_TMDB_ENV}" ]; then
    set -a; . "${MEDIA_TMDB_ENV}"; set +a
fi

exec .venv/bin/python orchestrator.py \
    --stream --apply \
    --probe-workers "${PROBE_WORKERS:-3}" \
    --copy-workers  "${COPY_WORKERS:-1}"
