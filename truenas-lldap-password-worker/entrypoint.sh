#!/bin/sh
# Container entrypoint for the lldap <-> TrueNAS password-change worker.
#
# Runs the service module (server.py) as the non-root `pw` user.
# No secrets are baked in: PW_TN_KEY / PW_TN_KEY_FILE / PW_LDAP_* come from
# the environment or mounted files at runtime.
#
# If $1 looks like an option/flag or a python module, exec python3 directly so
# `docker run ... --help` or a one-off diagnostic still works.
set -eu

# Accept an optional override command (default: run the server).
if [ "$#" -gt 0 ]; then
    case "$1" in
        -*|python3|python)
            exec "$@"
            ;;
        *)
            # Anything else: treat as the server command.
            exec python3 "$@"
            ;;
    esac
fi

exec python3 server.py
