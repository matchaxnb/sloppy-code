#!/usr/bin/env bash
# Full run for the music library: index and curate one top-level album at a
# time, so the library fills progressively and a partial run still leaves a
# populated, consistent library.
#
#   tmux new-session -d -s mo "~/music-organizer/run-full.sh > /tmp/mo-run.log 2>&1"
#
# Why per-album is safe for the sources: the import runs with `-C -W`, so it
# copies nothing and writes no tags; the only tag write in the whole pipeline
# is `musicorganize` writing to the clone it just created. Ordering index before
# clone for each album therefore cannot reach a source file.
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

run_source() {
    local root="$1" n=0 total
    total=$(find "$root" -mindepth 1 -maxdepth 1 -type d -print0 | tr -dc '\0' | wc -c)
    log "=== $root ($total albums) ==="
    while IFS= read -r -d '' album; do
        n=$((n + 1))
        log "[$n/$total] ${album#"$root"/}"
        "$V" -c "$C" import -C -W "$album" 2>&1 |
            grep -viE "backup|^$" | sed 's/^/    /'
        "$V" -c "$C" musicorganize -- "$album" 2>&1 | sed 's/^/    /'
    done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
    log "=== $root done ($n albums) ==="
}

run_source "$M/CleanFLAC"
run_source "$M/VGM"
run_source "$M/CleanMP3"

# Final pass over *everything*. Per-album scoping cannot collapse a duplicate
# work whose two releases were indexed in different iterations, so grouping
# gets one global sweep at the end. Idempotent: already-organized items carry
# `mo_source` and are skipped.
log "=== FINAL GROUPING PASS (global) ==="
"$V" -c "$C" musicorganize
log "=== FINAL GROUPING PASS done ==="

log "ALL DONE"
