#!/usr/bin/env bash
# Full run for the music library.
#
#   tmux new-session -d -s mo "~/music-organizer/run-full.sh > /tmp/mo-run.log 2>&1"
#
# Two passes, and that order is deliberate: a reflink of a file that is *also*
# imported in the same run gets its source rewritten in place by beets'
# tag write, which would (a) touch a source we promised never to touch and
# (b) break the clone's sign-off. Indexing first — with `-C -W`, so nothing is
# copied or written — and cloning afterwards keeps the source read-only for the
# whole run.
#
# Resumable: beets' import history plus the plugin's `mo_source` attribute mean
# a re-run skips finished work. Safe to kill at any point.

set -u

export BEETSDIR=/home/omp-agent/music-organizer
export PYTHONPATH=/home/omp-agent/music-organizer/shim

V=/home/omp-agent/music-organizer/.venv/bin/beet
C=/home/omp-agent/music-organizer/config.yaml
M=/mnt/largepool/bulk/Music

log() { printf '%s %s\n' "$(date -Is)" "$*"; }

log "=== INDEX start ==="
"$V" -c "$C" import -C -W "$M/CleanFLAC" "$M/VGM" "$M/CleanMP3"
log "=== INDEX done ==="

log "=== ORGANIZE start ==="
"$V" -c "$C" musicorganize
log "=== ORGANIZE done ==="

log "ALL DONE"
