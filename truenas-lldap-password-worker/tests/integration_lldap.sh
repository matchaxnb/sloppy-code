#!/usr/bin/env bash
#
# integration_lldap.sh — real lldap integration test for the LDAP half of the
# password worker.
#
# Spins up a throwaway lldap container, creates a throwaway user, then exercises
# LdapClient.bind / LdapClient.set_password (from core.py) end-to-end and
# verifies SPEC §3.3 (the constraintViolation when the user identity is omitted
# from the RFC 3062 Password Modify extended op).
#
# Requirements:
#   - Docker (dockerd) with Docker Hub reachable
#   - /tmp/venv/bin/python with ldap3 2.9.1
#
# Usage:  bash integration_lldap.sh
#
# The container is torn down at the end regardless of test outcome.
set -euo pipefail

CONTAINER_NAME="pw-lldap-test"
LDAP_PORT="13890"
HTTP_PORT="17170"
LDAP_URI="ldap://127.0.0.1:${LDAP_PORT}"
BASE_DN="dc=example,dc=com"
ADMIN_USER="admin"
ADMIN_PASS="testadminpass123"
JWT_SECRET="testsecretnotforproduction0123456789"
ADMIN_EMAIL="admin@example.com"

# Throwaway test user
TEST_USER="pwtestuser"
INITIAL_PW="InitialPass123!"
OLD_PW="${INITIAL_PW}"
NEW_PW="ChangedPass456!"

PY="/tmp/venv/bin/python"
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "### lldap integration test"
echo "### python: ${PY}"
echo

# ----------------------------------------------------------------- teardown helper
teardown() {
    echo
    echo "### Tearing down container ${CONTAINER_NAME} ..."
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
    if docker ps -a --filter "name=^${CONTAINER_NAME}$" --format '{{.Names}}' | grep -q "${CONTAINER_NAME}"; then
        echo "### FAIL: container still present after rm -f"
    else
        echo "### OK: container removed"
    fi
}
trap teardown EXIT

# ------------------------------------------------------- 1. start lldap container
echo "### Step 1: starting lldap container ..."
# Remove any leftover container first
docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

docker run -d --name "${CONTAINER_NAME}" \
    -p "${LDAP_PORT}:3890" -p "${HTTP_PORT}:17170" \
    -e "LLDAP_LDAP_BASE_DN=${BASE_DN}" \
    -e "LLDAP_LDAP_USER_PASS=${ADMIN_PASS}" \
    -e "LLDAP_JWT_SECRET=${JWT_SECRET}" \
    -e "LLDAP_LDAP_USER_EMAIL=${ADMIN_EMAIL}" \
    lldap/lldap:stable

echo "### Waiting for lldap to accept LDAP binds ..."
for i in $(seq 1 30); do
    if "${PY}" -c "
from ldap3 import Server, Connection
s = Server('${LDAP_URI}', connect_timeout=5)
c = Connection(s, user='uid=${ADMIN_USER},ou=people,${BASE_DN}', password='${ADMIN_PASS}', auto_bind=True)
c.unbind()
" 2>/dev/null; then
        echo "### lldap ready (attempt ${i})"
        break
    fi
    echo "  attempt ${i}: not ready ..."
    sleep 2
    if [ "${i}" -eq 30 ]; then
        echo "### FAIL: lldap did not become ready in 60s"
        exit 1
    fi
done

# ------------------------------------------------------- 2. create throwaway user
echo
echo "### Step 2: creating throwaway user '${TEST_USER}' ..."
"${PY}" - <<'PYEOF'
import json, urllib.request

LDAP_URI = "ldap://127.0.0.1:13890"
HTTP_URL = "http://127.0.0.1:17170"
BASE_DN  = "dc=example,dc=com"
ADMIN_USER = "admin"
ADMIN_PASS = "testadminpass123"
TEST_USER  = "pwtestuser"
INITIAL_PW = "InitialPass123!"

# --- admin login via /auth/simple/login ---
body = json.dumps({"username": ADMIN_USER, "password": ADMIN_PASS}).encode()
req = urllib.request.Request(
    f"{HTTP_URL}/auth/simple/login", data=body,
    headers={"Content-Type": "application/json"})
token = json.loads(urllib.request.urlopen(req).read())["token"]
print("  admin token obtained")

# --- create user via GraphQL ---
gql = f'{HTTP_URL}/api/graphql'
create_q = '''
mutation {{
  createUser(user: {{
    id: "{user}"
    email: "pwtestuser@example.com"
    displayName: "PW Test User"
    firstName: "PW"
    lastName: "Test"
  }}) {{
    __typename
    ... on User {{ id }}
  }}
}}
'''.format(user=TEST_USER)
body = json.dumps({"query": create_q}).encode()
req = urllib.request.Request(gql, data=body, headers={
    "Content-Type": "application/json",
    "Authorization": f"Bearer {token}",
})
resp = json.loads(urllib.request.urlopen(req).read())
print(f"  createUser: {resp['data']['createUser']}")

# --- set initial password via LDAP admin reset (RFC 3062, no old_pw) ---
from ldap3 import Server, Connection
from ldap3.extend.standard.modifyPassword import ModifyPassword

s = Server(LDAP_URI, connect_timeout=5)
admin_dn = f"uid={ADMIN_USER},ou=people,{BASE_DN}"
user_dn  = f"uid={TEST_USER},ou=people,{BASE_DN}"
c = Connection(s, user=admin_dn, password=ADMIN_PASS, auto_bind=True)
ok = ModifyPassword(c, user=user_dn, new_password=INITIAL_PW).send()
print(f"  admin set initial password: {ok}")
c.unbind()

# --- verify the user can bind ---
c2 = Connection(s, user=user_dn, password=INITIAL_PW, auto_bind=True)
print(f"  user bind with initial password: OK")
c2.unbind()
PYEOF

# ------------------------------------------------------- 3. exercise LdapClient (a-d)
echo
echo "### Step 3: exercising LdapClient from core.py (steps a-d) ..."
"${PY}" - <<'PYEOF'
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import LdapClient, ChangeError

LDAP_URI = "ldap://127.0.0.1:13890"
BASE_DN  = "dc=example,dc=com"
USERNAME = "pwtestuser"
OLD_PW   = "InitialPass123!"
NEW_PW   = "ChangedPass456!"

client = LdapClient(LDAP_URI, BASE_DN, timeout=8)

print("=" * 60)
print("Step a: bind(user, initial_password) -> expects success")
print("=" * 60)
try:
    client.bind(USERNAME, OLD_PW)
    print("RESULT: bind succeeded")
except ChangeError as e:
    print(f"RESULT: bind FAILED -> {e}")

print()
print("=" * 60)
print("Step b: set_password(user, old, new) -> expects success")
print("=" * 60)
try:
    client.set_password(USERNAME, OLD_PW, NEW_PW)
    print("RESULT: set_password succeeded")
except ChangeError as e:
    print(f"RESULT: set_password FAILED -> {e}")

print()
print("=" * 60)
print("Step c: bind(user, new_password) -> expects success")
print("=" * 60)
try:
    client.bind(USERNAME, NEW_PW)
    print("RESULT: bind succeeded")
except ChangeError as e:
    print(f"RESULT: bind FAILED -> {e}")

print()
print("=" * 60)
print("Step d: bind(user, old_password) -> MUST FAIL")
print("=" * 60)
try:
    client.bind(USERNAME, OLD_PW)
    print("RESULT: bind succeeded (UNEXPECTED!)")
except ChangeError as e:
    print(f"RESULT: bind FAILED (expected) -> {e}")
PYEOF

# ------------------------------------------------------- 4. step e (SPEC §3.3)
echo
echo "### Step 4: SPEC §3.3 — Password Modify WITHOUT user identity ..."
"${PY}" - <<'PYEOF'
from ldap3 import Server, Connection
from ldap3.extend.standard.modifyPassword import ModifyPassword
from ldap3.core.exceptions import LDAPException

LDAP_URI = "ldap://127.0.0.1:13890"
user_dn  = "uid=pwtestuser,ou=people,dc=example,dc=com"

s = Server(LDAP_URI, connect_timeout=5)
c = Connection(s, user=user_dn, password="ChangedPass456!",
               auto_bind=True, raise_exceptions=True)
print("Bound as pwtestuser (current password), raise_exceptions=True.")

print()
print("=" * 60)
print("Step e: Password Modify WITHOUT user identity")
print("       (raw ldap3, not LdapClient)")
print("       expects: constraintViolation")
print('       "Missing either user_id or password"')
print("=" * 60)

try:
    result = ModifyPassword(c, new_password="AnotherPass789!").send()
    print(f"RESULT: unexpected success -> {result}")
except LDAPException as e:
    print(f"RESULT: LDAPException raised (expected)")
    print(f"  type:         {type(e).__name__}")
    print(f"  str(e):       {e}")
    for attr in ("result", "description", "type", "message"):
        if hasattr(e, attr):
            print(f"  {attr}: {getattr(e, attr)}")
except Exception as e:
    print(f"RESULT: unexpected exception type: {type(e).__name__}: {e}")

try:
    c.unbind()
except Exception:
    pass
PYEOF

echo
echo "### All steps complete."
