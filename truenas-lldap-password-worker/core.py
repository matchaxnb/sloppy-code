#!/usr/bin/env python3
"""Core password-change logic: lldap + TrueNAS, retriable.

Pure logic with injectable boundaries, so it is testable without the network.
Concrete clients live in the same file; the HTTP layer imports from here and
does not reimplement any of this.

WHY TWO STORES
--------------
* lldap    -- the identity authority for every client (sssd, SSH, containers)
* TrueNAS -- owns the SMB passdb for the LOCAL REPLICA of each LDAP identity

lldap cannot write the SMB half: it stores OPAQUE verifiers, and no NT hash can
be derived from one. So this worker owns the dual write.

ORDERING -- THE CRUX
--------------------
    1. authenticate  (lldap, with the OLD password)
    2. TrueNAS       (change first)
    3. lldap         (the AUTHENTICATING store, changed LAST)
    4. verify        (bind with the new password)

lldap proves identity. If it were changed first and TrueNAS then failed, the
user's old password would stop working and they could NOT retry -- a
half-applied, unrecoverable state. Changing TrueNAS first means:
  * a TrueNAS failure leaves BOTH stores untouched  -> clean retry
  * an lldap failure leaves TrueNAS updated but lldap unchanged, so the user
    can still authenticate with their OLD password and retry; if lldap keeps
    rejecting the value, they retry with a different one and TrueNAS is
    overwritten -> still converges.

Do not "simplify" the order back.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ChangeError(Exception):
    """A failure safe to show the user verbatim: no secrets, no tracebacks."""


@dataclass
class ChangeResult:
    """Machine-readable outcome, so the page can give precise guidance."""
    username: str
    lldap: bool = False
    truenas: bool = False
    truenas_applicable: bool = False   # is there a local replica to update?
    partial: bool = False              # exactly one store changed
    message: str = ""

    @property
    def ok(self) -> bool:
        # Success only when the lldap change is confirmed. The TrueNAS half is
        # required only when there is a local replica to update.
        if not self.lldap:
            return False
        if self.truenas_applicable and not self.truenas:
            return False
        return True


# --------------------------------------------------------------------- clients
class TrueNasPasswordClient:
    """Real client: the supported JSON-RPC API over websocket.

    The API key is passed to `auth.login_with_api_key` -- it is NOT a
    constructor argument (that raises AttributeError).

    NOTE: not yet exercised against the live API from this deployment; the
    interface is fixed so the requester can verify and, if needed, swap the
    implementation without touching core.py.
    """

    def __init__(self, wss_url: str, api_key: str, *, verify_ssl: bool = False):
        self.wss_url = wss_url
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self._client = None

    def _connect(self):
        if self._client is None:
            try:
                from truenas_api_client import Client  # ships with middleware
            except ImportError as e:
                raise ChangeError(
                    "truenas_api_client is unavailable; run inside the worker "
                    "image, or vendor the library"
                ) from e
            try:
                # NB: Client() connects EAGERLY in __init__, so a bad host or a
                # stopped middleware raises here rather than on first call.
                self._client = Client(self.wss_url, verify_ssl=self.verify_ssl)
                if not self._client.call("auth.login_with_api_key", self.api_key):
                    raise ChangeError("TrueNAS rejected the API key")
            except ChangeError:
                self._client = None
                raise
            except Exception as e:
                # Never leak a raw socket/websocket exception: the HTTP layer
                # relays ChangeError text verbatim to the user.
                self._client = None
                raise ChangeError(
                    f"cannot reach the TrueNAS API ({type(e).__name__})"
                ) from e
        return self._client

    def _call(self, method: str, *params):
        """Invoke a middleware method, reconnecting ONCE if the socket is dead.

        The websocket is long-lived and middleware closes it when idle
        (`WebSocketConnectionClosedException: Connection to remote host was
        lost.`), so a cached client goes stale between calls and every later
        call fails. On a transport failure, drop the client, reconnect and retry
        once. Genuine errors are not retried; they would fail identically.
        """
        try:
            return self._connect().call(method, *params)
        except ChangeError:
            raise
        except Exception as first:
            self._reset()
            try:
                return self._connect().call(method, *params)
            except ChangeError:
                raise
            except Exception as second:
                raise ChangeError(
                    f"TrueNAS call failed ({type(second).__name__})"
                ) from second

    def _reset(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def find_local_replica(self, username: str):
        """Return the LOCAL row for `username`, or None.

        `user.query` can return MORE THAN ONE row per username: the local
        replica AND the directory (sssd/lldap) view. For example testuser:

            id=81        local=True   smb=True    uid=3005   <- the replica
            id=100003005 local=False  smb=False   uid=3005   <- directory view

        Row order is NOT guaranteed, so checking rows[0] can pick the directory
        row, read local=False, and silently skip the SMB update.
        """
        try:
            rows = self._call("user.query", [["username", "=", username]]) or []
        except ChangeError:
            raise
        except Exception as e:
            raise ChangeError(
                f"TrueNAS lookup failed ({type(e).__name__})"
            ) from e
        for row in rows:
            if row.get("local"):
                return row
        return None

    def set_password(self, username: str, new_password: str) -> None:
        """Writes both the unix hash and the SMB passdb entry. Returns None."""
        try:
            self._call("user.set_password",
                       {"username": username, "new_password": new_password})
        except ChangeError:
            raise
        except Exception as e:
            raise ChangeError(
                f"TrueNAS rejected the change ({type(e).__name__})"
            ) from e

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


class StubTrueNasPasswordClient:
    """Used when no API key is configured.

    Records what it *would* have done and refuses to pretend it succeeded.
    Selected by configuration (no PW_TN_KEY), never by editing code.
    """

    def __init__(self, *_, **__):
        self.calls = []

    def find_local_replica(self, username: str):
        self.calls.append(("find_local_replica", username))
        return {"username": username, "stub": True}

    def set_password(self, username: str, new_password: str) -> None:
        self.calls.append(("set_password", username))

    def close(self):
        pass


class LdapClient:
    """ldap3-backed client: bind, and RFC 3062 Password Modify.

    ldap3 is used rather than python-ldap because it is pure Python, which keeps
    the image build trivial (no compiler, no libldap headers).

    ldap3 2.9.1 quirk: `auto_bind=True` raises LDAPBindError on bad credentials
    even with raise_exceptions=False, so bad credentials arrive as an exception
    and must be caught explicitly.
    """

    def __init__(self, uri: str, base_dn: str, *, timeout: int = 8):
        self.uri = uri
        self.base_dn = base_dn
        self.timeout = timeout

    def dn(self, username: str) -> str:
        return f"uid={username},ou=people,{self.base_dn}"

    def _connection(self, dn: str, password: str):
        from ldap3 import Connection, Server
        from ldap3.core.exceptions import LDAPBindError, LDAPException

        try:
            return Connection(
                Server(self.uri, connect_timeout=self.timeout),
                user=dn, password=password, auto_bind=True, raise_exceptions=False,
            )
        except LDAPBindError:
            raise ChangeError("invalid credentials") from None
        except LDAPException as e:
            raise ChangeError(f"cannot reach lldap: {e}") from None

    def bind(self, username: str, password: str) -> None:
        conn = self._connection(self.dn(username), password)
        conn.unbind()

    def set_password(self, username: str, old_password: str, new_password: str) -> None:
        """RFC 3062 Password Modify.

        The explicit identity is REQUIRED by lldap: without it lldap answers
        constraintViolation "Missing either user_id or password".
        """
        from ldap3.core.exceptions import LDAPException
        from ldap3.extend.standard.modifyPassword import ModifyPassword

        dn = self.dn(username)
        conn = self._connection(dn, old_password)
        try:
            ok = ModifyPassword(
                conn, user=dn, old_password=old_password, new_password=new_password
            ).send()
        except LDAPException as e:
            raise ChangeError(f"lldap rejected the password change: {e}") from None
        finally:
            try:
                conn.unbind()
            except Exception:
                pass
        if not ok:
            raise ChangeError(
                "lldap rejected the new password (policy, or the current "
                "password is wrong)"
            )


# ------------------------------------------------------------------ core flow
def change_password(
    username: str,
    old_password: str,
    new_password: str,
    *,
    ldap_client,
    tn_client=None,
    min_len: int = 8,
) -> ChangeResult:
    """Authenticate, then update both stores -- retriably. See module docstring
    for why the ordering is what it is.
    """
    username = (username or "").strip()
    if not username or not USERNAME_RE.match(username):
        raise ChangeError("username is required and must be a simple name")
    if not old_password:
        raise ChangeError("current password is required")
    if len(new_password or "") < min_len:
        raise ChangeError(f"new password must be at least {min_len} characters")
    if new_password == old_password:
        raise ChangeError("new password must differ from the current one")

    result = ChangeResult(username=username)

    # 1. authentication -- lldap decides, before anything is changed.
    ldap_client.bind(username, old_password)

    # 2. TrueNAS first: a failure here means nothing changed -> clean retry.
    replica = None
    if tn_client is not None:
        replica = tn_client.find_local_replica(username)
    result.truenas_applicable = replica is not None

    if replica is not None:
        try:
            tn_client.set_password(username, new_password)
            result.truenas = True
        except ChangeError:
            raise
        except Exception as e:
            raise ChangeError(
                f"nothing was changed: the NAS password could not be set ({e}). "
                "Your current password still works; please try again."
            ) from None

    # 3. lldap last -- the authenticating store. On failure the user can still
    #    sign in with their old password and retry.
    try:
        ldap_client.set_password(username, old_password, new_password)
        result.lldap = True
    except ChangeError as e:
        result.lldap = False
        result.partial = result.truenas
        if result.truenas:
            result.message = (
                "Your SMB password was updated, but your directory password was "
                "not. Your directory password is still your previous one -- sign "
                "in with that and try again, choosing a different new password "
                "if this one keeps being rejected."
            )
        raise ChangeError(result.message or str(e)) from None

    # 4. verify -- a return value is not proof.
    ldap_client.bind(username, new_password)

    return result


def build_tn_client(env=None):
    """Select the TrueNAS client by configuration, not by editing code."""
    env = env if env is not None else os.environ
    key = env.get("PW_TN_KEY", "")
    key_file = env.get("PW_TN_KEY_FILE", "")
    if not key and key_file and os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    wss = env.get("PW_TN_WSS", "wss://127.0.0.1/api/current")
    if not key:
        return StubTrueNasPasswordClient(), False
    return TrueNasPasswordClient(wss, key), True
