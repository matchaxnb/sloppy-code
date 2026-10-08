# syntax=docker/dockerfile:1
# Password-change worker: lldap + TrueNAS.

# truenas_api_client is not on PyPI; build it from source at a tag matching the
# target host's TrueNAS version. git is needed only in this stage.
FROM python:3.11-slim AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --wheel-dir /wheels \
      "truenas_api_client @ git+https://github.com/truenas/api_client.git@TS-25.04.2.6"

FROM python:3.11-slim

RUN groupadd --system --gid 10001 pw \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/pw --create-home pw

WORKDIR /app

COPY requirements.txt ./
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir -r requirements.txt --find-links /wheels \
 && rm -rf /wheels

# Every module must be listed: a missed one starts and then dies on import.
COPY core.py authgate.py server.py messages.py secret_string.py \
     healthcheck.py entrypoint.sh ./
COPY static ./static
RUN chmod +x entrypoint.sh

# Defaults for every variable the service reads. Override at run time; secrets
# have no default and must be supplied.
ENV PW_LISTEN=0.0.0.0:8099 \
    PW_BIND=127.0.0.1:8099 \
    PW_LDAP_URI=ldap://127.0.0.1:389 \
    PW_LDAP_BASE=dc=example,dc=lan \
    PW_LDAP_DN_TEMPLATE=uid={username},ou=people,{base_dn} \
    PW_TN_WSS= \
    PW_TN_KEY= \
    PW_TN_KEY_FILE=/run/secrets/pw.key \
    PW_BANNER_DIR=/etc/pw-worker \
    PW_BANNER_FILE=banner.html \
    PW_BANNER_FILE_AUTHED=banner-authed.html \
    PW_TRUSTED_SUBNETS= \
    PW_MIN_LEN=8 \
    PW_FAIL_THRESHOLD=2 \
    PW_BAN_BASE=5 \
    PW_BAN_MAX=900 \
    PW_FIRST_COOLDOWN=3 \
    PW_DELAY_BASE=0.8 \
    PW_DELAY_VARIANCE=0.4 \
    PW_SESSION_TTL=300 \
    PW_MAX_TRACKED_IPS=4096 \
    PW_DISTINCT_IPS_MAX=10 \
    PW_DISTINCT_IPS_WINDOW=60 \
    PW_DISTINCT_IPS_COOLDOWN=900 \
    PW_MAX_PENDING=4096 \
    PW_MAX_SESSIONS=1024

# In-container port only; host-side exposure is the compose port mapping.
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python3", "healthcheck.py"]

USER pw

ENTRYPOINT ["./entrypoint.sh"]
