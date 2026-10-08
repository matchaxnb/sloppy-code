#!/usr/bin/env python3
"""Deploy the password worker as a TrueNAS custom app.

TrueNAS app creation, learned the hard way (see the skill):

  * `app.create` is a JOB.
  * A custom app is created with `custom_app: true` plus ONE of
    `custom_compose_config` (a dict) or `custom_compose_config_string` (YAML).
    Supplying both is an error; supplying neither is an error.
  * Compose in a TrueNAS custom app is "ix"-flavoured: `app.custom.create`
    renders it and runs `compose up`, so ordinary Compose keys work, but
    TrueNAS adds its own (portal, storage metadata) via labels.

Ports: the host-side publish is what controls exposure. Bind the container
internally on 0.0.0.0 and publish ONLY on the LAN address (see the README).

Usage:
    TN_KEY=<api key> python3 deploy_app.py [--port 30030] [--name pw-worker]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

try:
    from truenas_api_client import Client
except ImportError:
    print("run this on the NAS, or install truenas_api_client", file=sys.stderr)
    raise SystemExit(2)

IMAGE = "exampleuser/truenas-lldap-password-management:0.1.0"
# Where the worker reaches the middleware API. Defaults to the SAME host it is
# published on: inside the container 127.0.0.1 is the container itself, so
# loopback here silently fails to connect. Override only if the API lives
# somewhere other than the publishing host.
WSS_OVERRIDE = os.environ.get("TN_WSS", "").strip()
# LDAP endpoint for the directory the worker writes to. Deployment-specific, so
# no host address is guessed; the port and base DN are arguments/overrides.
LDAP_PORT = os.environ.get("TN_LDAP_PORT", "30326")
LDAP_BASE = os.environ.get("TN_LDAP_BASE", "dc=example,dc=lan")
# Trusted subnets: exempt from rate limiting and the only peers whose
# X-Forwarded-For is believed. Empty means "trust no subnet" (every client is
# seen as the proxy). No value is assumed -- set TN_TRUSTED_SUBNETS.
TRUSTED_SUBNETS = os.environ.get("TN_TRUSTED_SUBNETS", "").strip()
# Substituted into the compose template AFTER its own indentation, so this must
# be bare text: the template supplies the leading spaces, and textwrap.dedent
# then sees the line at the same indent as its neighbours. A whitespace-only
# result is ignored by dedent.
_TRUSTED_LINE = (f'PW_TRUSTED_SUBNETS: "{TRUSTED_SUBNETS}"'
                 if TRUSTED_SUBNETS else "")


def compose_yaml(app_name: str, port: int, host_ip: str, key_path: str,
                 banner_dir: str) -> str:
    """The custom-app compose document.

    `PW_LISTEN` is the IN-CONTAINER bind (0.0.0.0 is correct and required --
    a container netns has no LAN address). Host-side exposure is the `ports`
    publish, pinned to `host_ip`, never a bare 0.0.0.0.

    Volume form matches what TrueNAS itself renders for installed apps
    (long syntax: type/source/target), so it round-trips cleanly if the app is
    ever converted or re-rendered.

    The banner directory is mounted so the fragments can be edited on the NAS
    without touching the app or the image.
    """
    # Reach the middleware on the same host we publish on, unless told otherwise.
    # Using loopback here would point the container at itself and fail.
    wss = WSS_OVERRIDE or f"wss://{host_ip}/api/current"
    return textwrap.dedent(f"""\
        services:
          {app_name}:
            image: {IMAGE}
            container_name: {app_name}
            restart: unless-stopped
            ports:
              - "{host_ip}:{port}:8099"
            environment:
              PW_LISTEN: "0.0.0.0:8099"
              PW_BIND: "{host_ip}:{port}"
              PW_LDAP_URI: "ldap://{host_ip}:{LDAP_PORT}"
              PW_LDAP_BASE: "{LDAP_BASE}"
              PW_TN_WSS: "{wss}"
              PW_TN_KEY_FILE: "/run/secrets/pw.key"
              PW_BANNER_DIR: "/banners"
              {_TRUSTED_LINE}
            volumes:
              - type: bind
                source: {key_path}
                target: /run/secrets/pw.key
                read_only: true
                bind:
                  create_host_path: false
                  propagation: rprivate
              # Banner fragments: edit in place on the NAS, no redeploy needed.
              - type: bind
                source: {banner_dir}
                target: /banners
                read_only: true
                bind:
                  create_host_path: false
                  propagation: rprivate
    """)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="pw-worker")
    ap.add_argument("--port", type=int, default=30030)
    ap.add_argument("--host-ip", default="127.0.0.1",
                    help="host address to publish on; set this to your LAN address")
    ap.add_argument("--key-file", default="/etc/pw-worker/pw.key",
                    help="host path to the 0600 API key file")
    ap.add_argument("--banner-dir", default="/mnt/pool/pwortal-data",
                    help="host dataset holding the banner fragments (editable on the NAS)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("TN_KEY", "")
    if not key:
        print("TN_KEY is required", file=sys.stderr)
        return 2

    yaml_doc = compose_yaml(args.name, args.port, args.host_ip, args.key_file,
                            args.banner_dir)
    payload = {
        "app_name": args.name,
        "custom_app": True,
        "custom_compose_config_string": yaml_doc,
    }

    if args.dry_run:
        print("--- payload (dry run) ---")
        print(json.dumps({**payload, "custom_compose_config_string": "<yaml>"}, indent=2))
        print("--- compose ---")
        print(yaml_doc)
        return 0

    client = Client(WSS_OVERRIDE or f"wss://{args.host_ip}/api/current",
                    verify_ssl=False)
    try:
        if not client.call("auth.login_with_api_key", key):
            print("API key rejected", file=sys.stderr)
            return 1

        # refuse to clobber an existing app
        if client.call("app.query", [["id", "=", args.name]]):
            print(f"app {args.name!r} already exists; not touching it", file=sys.stderr)
            return 1

        # port must be free. NB: app.used_ports returns a flat list of ints,
        # not dicts -- verified against the live API.
        used = client.call("app.used_ports") or []
        taken = {int(p) for p in used if isinstance(p, (int, str)) and str(p).isdigit()}
        if args.port in taken:
            print(f"port {args.port} is already used by another app "
                  f"(in use: {sorted(taken)})", file=sys.stderr)
            return 1

        print(f"creating custom app {args.name!r} on {args.host_ip}:{args.port} ...")
        job_id = client.call("app.create", payload)
        # app.create IS a job. core.job_wait takes exactly ONE argument (the id):
        # passing a second raised "[EFAULT] Too many arguments (expected 1,
        # found 2)" and made the wait look like a create failure.
        try:
            client.call("core.job_wait", job_id)
            print(f"create job {job_id} finished")
        except Exception as e:
            print(f"warning: could not wait on job {job_id}: {e}", file=sys.stderr)
            print("check it with: midclt call core.get_jobs '[[\"id\",\"=\",<id>]]'",
                  file=sys.stderr)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
