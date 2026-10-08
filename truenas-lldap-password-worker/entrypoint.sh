#!/bin/sh
# Run the service as the non-root `pw` user. An argument overrides the command.
set -eu

if [ "$#" -gt 0 ]; then
    case "$1" in
        -*|python3|python)
            exec "$@"
            ;;
        *)
            exec python3 "$@"
            ;;
    esac
fi

exec python3 server.py
