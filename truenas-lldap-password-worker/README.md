# truenas-lldap-password-worker

> **Part of [`sloppy-code`](../README.md). Written by an AI assistant, unreviewed.
> Read it before you run it.**

Lets a user change their password **once** and have it written to **two** stores:
an LDAP directory (RFC 3062 Password Modify, tested against
[lldap](https://github.com/lldap/lldap)) and a TrueNAS host's local user database
plus SMB passdb (middleware JSON-RPC). Without it the two drift apart: the web
directory and the file share have different backends, so changing one leaves the
other stale. This changes both, and says so honestly when only one succeeded.

## Features

- **Two-stage login.** Stage 1 takes a username and *cannot fail*; stage 2 takes
  the password. The failure is byte-identical for "no such user" and "wrong
  password", and every path sleeps a similar random interval, so the form cannot
  enumerate accounts.
- **Rate limiting.** Per-IP cooldown escalating to an exponential ban, plus a
  circuit breaker that refuses the whole service when too many distinct client
  addresses appear at once.
- **Trusted subnets.** Configured subnets are exempt from both mechanisms, and are
  the only peers whose `X-Forwarded-For` is believed. Trust is not authorization:
  a trusted client still authenticates and passes every rule.
- **No technical detail leaks to the browser.** No backend names, hostnames,
  ports, or exception classes. Transient failures say "try again" instead of
  implying the user did something wrong.
- **Editable banners.** Two plain HTML fragments on disk, one per auth state.
  No redeploy to change them.
- **One-shot partial recovery.** If only one store is written, the message tells
  the user their old password still works and how to retry.

## Quick deployment

Needs: Python 3.11, an LDAP server, a TrueNAS host with the middleware API, and
an API key. `truenas_api_client` is **not on PyPI** — the image builds it from
`github.com/truenas/api_client` at a pinned tag (see `Dockerfile`).

**1. Create a least-privilege API key** on the NAS and store it read-only:

```sh
install -m 400 -o 10001 key.txt /etc/pw-worker/pw.key
```

**2. Get an image.** Build and push your own — the name below is a placeholder:

```sh
docker build -t YOURNAME/truenas-lldap-password-management:0.1.0 .
docker push YOURNAME/truenas-lldap-password-management:0.1.0
```

**3. Run it.** Compose, with the key mounted and the port pinned to a LAN address:

```yaml
services:
  password-worker:
    image: YOURNAME/truenas-lldap-password-management:0.1.0
    restart: unless-stopped
    ports:
      - "10.0.0.5:8099:8099"          # a LAN address, never 0.0.0.0
    environment:
      PW_LDAP_URI: "ldap://10.0.0.5:30326"
      PW_LDAP_BASE: "dc=example,dc=lan"
      PW_TN_WSS: "wss://10.0.0.5/api/current"   # the host, not 127.0.0.1
      PW_TN_KEY_FILE: "/run/secrets/pw.key"
      PW_BANNER_DIR: "/banners"
      PW_TRUSTED_SUBNETS: "10.0.0.0/24"
    volumes:
      - /etc/pw-worker/pw.key:/run/secrets/pw.key:ro
      - /mnt/pool/pwortal-data:/banners:ro
```

**4. Put it behind a reverse proxy** that **appends** to `X-Forwarded-For`. A
proxy that forwards a client-supplied header verbatim makes the ban spoofable.
Point the proxy at the LAN address above.

**5. Verify.** Load the page, change a password, then confirm the new one binds
over LDAP *and* works for SMB.

On TrueNAS itself, `deploy_app.py` creates the custom app for you:

```sh
python3 deploy_app.py --host-ip 10.0.0.5 --port 8099 \
  --key-file /etc/pw-worker/pw.key --banner-dir /mnt/pool/pwortal-data --dry-run
```

`--host-ip` also sets the middleware URL, so the publish address and the API
address cannot diverge. `app.create` is a job that returns before the app is
queryable — start it separately.

### Required configuration

| Variable | Meaning |
|---|---|
| `PW_BIND` | Host-side publish address. **A LAN address, never `0.0.0.0`.** |
| `PW_LDAP_URI` / `PW_LDAP_BASE` | LDAP endpoint and base DN |
| `PW_TN_WSS` | Middleware URL. Must be reachable *from the container*: `127.0.0.1` there is the container itself. |
| `PW_TN_KEY_FILE` / `PW_TN_KEY` | API key; prefer the file |
| `PW_TRUSTED_SUBNETS` | **No default.** Comma-separated CIDRs exempt from rate limiting. Loopback always trusted. Unset means `X-Forwarded-For` is never believed and every client shares the proxy's address — the safe failure. |

Others: `PW_LISTEN`, `PW_BANNER_DIR`, `PW_BANNER_FILE(_AUTHED)`, `PW_MIN_LEN`,
`PW_FAIL_THRESHOLD`, `PW_BAN_BASE`/`PW_BAN_MAX`, `PW_FIRST_COOLDOWN`,
`PW_DISTINCT_IPS_MAX`/`_WINDOW`/`_COOLDOWN`, `PW_MAX_PENDING`/`PW_MAX_SESSIONS`,
`PW_DELAY_MIN`/`PW_DELAY_MAX`, `PW_SESSION_TTL`. Defaults are in `server.py` and
`authgate.py`.

## Running and testing

```sh
python3 server.py
python3 -m unittest discover -s tests
```

`tests/integration_lldap.sh` is a **live** test that changes data in a real
directory. Read it first; it is not part of the unit suite.

## Read before trusting it

- **Unreviewed** (see the parent README).
- **The LDAP server's policy is the only real one.** In the lldap version tested,
  the user password-change path has *no server-side length check* — the 8-character
  minimum in its web UI is client-side `wasm` validation that a direct LDAP client
  never hits. Only `PW_MIN_LEN` applies.
- Sessions do not survive a refresh, and ban state is in memory — a restart
  forgets it. Both are deliberate.
