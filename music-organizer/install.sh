# Deploy the beets curation pipeline for the MusicLibrary.
#
#   ./install.sh            install/refresh the venv, shim and units
#   ./install.sh --dry-run  show what would happen
#
# Idempotent. Run it as the identity that will own the library (`omp-agent`
# on arch-privileged, i.e. 192.168.1.131) -- NOT as root, and never on the
# TrueNAS host itself.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${MUSIC_ORGANIZER_PREFIX:-$HOME/music-organizer}"
VENV="$PREFIX/.venv"
UNITS="$HOME/.config/systemd/user"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

run() {
    if [ "$DRY" = 1 ]; then
        printf '  + %s\n' "$*"
    else
        "$@"
    fi
}

echo "prefix: $PREFIX"
echo "units:  $UNITS"

echo "== staging the program files =="
run mkdir -p "$PREFIX"
for f in config.yaml README.md; do
    run install -m 0644 "$HERE/$f" "$PREFIX/$f"
done
run mkdir -p "$PREFIX/shim" "$PREFIX/beetsplug"
run install -m 0644 "$HERE/shim/reflink.py" "$PREFIX/shim/reflink.py"
run install -m 0644 "$HERE/beetsplug/musicorganize.py" \
    "$PREFIX/beetsplug/musicorganize.py"

echo "== virtualenv =="
if [ ! -x "$VENV/bin/beet" ]; then
    run python3 -m venv "$VENV"
fi
run "$VENV/bin/pip" install --quiet --upgrade pip
run "$VENV/bin/pip" install --quiet beets

echo "== proving the shim is the one beets will import =="
if [ "$DRY" = 0 ]; then
    PYTHONPATH="$PREFIX/shim" "$VENV/bin/python" - <<'PY'
import reflink
expected = "shim/reflink.py"
assert reflink.__file__.endswith(expected), reflink.__file__
print("  reflink ->", reflink.__file__)
PY
fi

echo "== systemd (user) units =="
run mkdir -p "$UNITS"
for u in "$HERE"/systemd/*.service "$HERE"/systemd/*.timer; do
    run install -m 0644 "$u" "$UNITS/$(basename "$u")"
done

echo
echo "done. next, as your user:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now music-organizer-index.timer"
echo "  systemctl --user list-timers music-organizer-index.timer"
echo
echo "first run by hand (index only; sources are never written):"
echo "  BEETSDIR=$PREFIX PYTHONPATH=$PREFIX/shim \\"
echo "    $VENV/bin/beet -c $PREFIX/config.yaml import -C -W \\"
echo "    /mnt/largepool/bulk/Music/CleanFLAC /mnt/largepool/bulk/Music/VGM"
echo
echo "then curate (dry run first):"
echo "  BEETSDIR=$PREFIX PYTHONPATH=$PREFIX/shim \\"
echo "    $VENV/bin/beet -c $PREFIX/config.yaml musicorganize -n"
