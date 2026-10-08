#!/usr/bin/env bash
# zfs-reflink-monitor.sh — sample ZFS dataset space properties over time.
#
# Run this on the host that owns the pool (a container usually has no `zfs`), and
# write the output somewhere the reader can reach. Nothing here is host-specific:
# pass the dataset and, optionally, an output path.
#
#   ./zfs-reflink-monitor.sh                          # $ZFS_DATASET, 5s, stdout
#   ./zfs-reflink-monitor.sh tank/media 2 /path/mon.tsv
#
# Environment:
#   ZFS_DATASET   dataset to sample            (no default; required)
#   ZFS_MON_OUT   output TSV path              (default: stdout)
#   ZFS_MON_PROPS properties to sample         (see below)
#
# Reading a reflink: it adds no unique space, so `used` stays flat; `referenced`
# counts data that "may or may not be shared" (zfsprops(7)) and can still rise.
# A genuine full copy shows as a jump in `used`.

set -eu

DS="${1:-${ZFS_DATASET:-}}"
INTERVAL="${2:-${ZFS_MON_INTERVAL:-5}}"
OUT="${3:-${ZFS_MON_OUT:-}}"
PROPS="${ZFS_MON_PROPS:-used,logicalused,referenced,logicalreferenced,available,usedbydataset,usedbysnapshots}"

if [ -z "$DS" ]; then
    echo "usage: $0 <dataset> [interval] [outfile]   (or set ZFS_DATASET)" >&2
    exit 2
fi
if ! command -v zfs >/dev/null 2>&1; then
    echo "zfs not found: run this on the host that owns the pool" >&2
    exit 3
fi

emit() {
    if [ -n "$OUT" ]; then
        mkdir -p "$(dirname "$OUT")"
        if [ ! -s "$OUT" ]; then
            { printf 'epoch\tdataset'; for p in ${PROPS//,/ }; do printf '\t%s' "$p"; done; printf '\n'; } > "$OUT"
        fi
        printf '%s\t%s\t%s\n' "$(date +%s)" "$DS" "$1" >> "$OUT"
    else
        printf '%s\t%s\t%s\n' "$(date +%s)" "$DS" "$1"
    fi
}

[ -n "$OUT" ] && echo "sampling $DS every ${INTERVAL}s -> $OUT" >&2

while :; do
    vals=$(zfs get -Hp -o value "$PROPS" "$DS" | paste -sd '\t' -)
    emit "$vals"
    sleep "$INTERVAL"
done
