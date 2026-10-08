# syntax=docker/dockerfile:1
# Image: lldap <-> TrueNAS password-change worker.
#
# Base: python:3.11-slim. ldap3 is pure-python, so no compiler / libldap
# headers are needed. truenas_api_client is not on PyPI (SPEC 3.5); it is
# installed from its standalone source repo and pulls in only the pure-python
# websocket-client. core.py degrades gracefully if that import fails.
#
# Runs as a non-root user. No secrets are baked in: the TrueNAS API key, lldap
# endpoint, and bind address all come from the environment at runtime.

# ---- builder: build the truenas_api_client wheel from git source -----------
# truenas_api_client is not on PyPI (SPEC 3.5). It has tagged releases; we pin
# to the tag matching the target host's TrueNAS version (25.04.2.6) so the
# client behaviour in the image matches the server. git is needed only here;
# it does NOT ship in the final image.
FROM python:3.11-slim AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --wheel-dir /wheels \
      "truenas_api_client @ git+https://github.com/truenas/api_client.git@TS-25.04.2.6"

# ---- final image -----------------------------------------------------------
FROM python:3.11-slim

# Create a non-root user (fixed uid/gid for predictability) and a home for any
# mounted key file with a known owner.
RUN groupadd --system --gid 10001 pw \
 && useradd  --system --uid 10001 --gid 10001 --home-dir /home/pw --create-home pw

WORKDIR /app

# Install dependencies. ldap3 comes from PyPI; truenas_api_client from the
# wheel built in the builder stage (its only runtime dep, websocket-client, is
# pure-python and pulled from PyPI automatically). --find-links supplements
# PyPI with the local wheel dir; --no-cache-dir keeps the image small.
COPY requirements.txt ./
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir -r requirements.txt --find-links /wheels \
 && rm -rf /wheels

# Application code. core.py is frozen (imported by server.py); server.py is the
# HTTP service; entrypoint.sh just execs it.
#
# static/ MUST be copied: server.py serves the page from
#   Path(__file__).parent / "static" / "index.html"
# Omitting it makes GET / return 404 with an empty /app/static -- an easy miss,
# because the API endpoint still works and only the page breaks.
# Application code. Copy explicitly rather than `COPY . .` so nothing
# unintended (tests, reports, secrets) can slip into the image -- but that means
# every new module must be added here. authgate.py was missed once and the
# container died with ModuleNotFoundError on startup.
COPY core.py authgate.py server.py entrypoint.sh ./
COPY static ./static
RUN chmod +x entrypoint.sh

# Default service port (PW_LISTEN default 0.0.0.0:8099). Only a hint for
# operators and for `docker run -P`. server.py binds PW_LISTEN INSIDE the
# container (0.0.0.0 is correct here -- the interface does not exist inside a
# container). Host-side exposure is controlled separately by the compose
# `ports` mapping (PW_BIND), which MUST default to the LAN address, never a
# bare 0.0.0.0 publish (that is how the lldap standby became internet-reachable).
EXPOSE 8099

# HEALTHCHECK: probe the service from inside the container. server.py serves
# GET / as the login page (200). Parse the port from PW_LISTEN robustly
# (rsplit on ':' handles IPv6 like [::]:8099); fall back to 8099.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import os,urllib.request; s=os.environ.get('PW_LISTEN','0.0.0.0:8099'); p=s.rsplit(':',1)[-1] if ':' in s else '8099'; urllib.request.urlopen('http://127.0.0.1:'+p+'/', timeout=2).read()" \
        || exit 1

USER pw

ENTRYPOINT ["./entrypoint.sh"]
