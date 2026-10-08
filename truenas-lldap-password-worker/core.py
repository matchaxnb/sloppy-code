#!/usr/bin/env python3
"""Password-change logic across lldap and TrueNAS.

Pure logic with injectable clients, so it is testable without a network. The
clients here are the concrete ones used in the image.

Ordering matters. lldap authenticates, so it is changed LAST: if it were first
and TrueNAS then failed, the old password would stop working and the user could
not retry. TrueNAS first means a failure there leaves both stores untouched.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass

import ldap3
from ldap3.core.exceptions import LDAPBindError, LDAPException
from ldap3.extend.standard.modifyPassword import ModifyPassword
from truenas_api_client import Client

from messages import ErrorKind, operator_text

__all__ = [
    "ChangeError",
    "ChangeResult",
    "ConfigError",
    "LdapClient",
    "TrueNasPasswordClient",
    "build_ldap_client",
    "build_tn_client",
    "change_password",
]

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

DEFAULT_DN_TEMPLATE = "uid={username},ou=people,{base_dn}"


class ChangeError(Exception):
    """A failure with a kind, so the HTTP layer can word it without matching text."""

    def __init__(self, message: str, kind: ErrorKind = ErrorKind.UNKNOWN):
        super().__init__(message)
        self.kind = kind


class ConfigError(ChangeError):
    """The service is misconfigured. Not caused by a request; fix the deployment."""


@dataclass
class ChangeResult:
    """Machine-readable outcome, so the page can give precise guidance."""

    username: str
    lldap: bool = False
    truenas: bool = False
    truenas_applicable: bool = False
    partial: bool = False
    reverted: bool = False

    @property
    def ok(self) -> bool:
        """True only when lldap changed, and TrueNAS too where a replica exists."""
        return self.lldap and not (self.truenas_applicable and not self.truenas)


class TrueNasPasswordClient:
    """TrueNAS JSON-RPC over websocket.

    The API key goes to `auth.login_with_api_key`; it is not a constructor
    argument.
    """

    def __init__(self, wss_url: str, api_key: str, *, verify_ssl: bool = False):
        self.wss_url = wss_url
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self._client = None

    def _connect(self):
        if self._client is None:
            try:
                self._client = Client(self.wss_url, verify_ssl=self.verify_ssl)
                if not self._client.call("auth.login_with_api_key", self.api_key):
                    raise ChangeError("TrueNAS rejected the API key", ErrorKind.UNKNOWN)
            except ChangeError:
                self._client = None
                raise
            except Exception as e:
                self._client = None
                raise ChangeError(
                    f"cannot reach the TrueNAS API ({type(e).__name__})",
                    ErrorKind.TRANSIENT,
                ) from e
        return self._client

    def _call(self, method: str, *params):
        """Call a middleware method, reconnecting once if the socket is dead."""
        try:
            return self._connect().call(method, *params)
        except ChangeError:
            raise
        except Exception:
            # Sometimes the websocket expires - renew.
            self._reset()
            try:
                return self._connect().call(method, *params)
            except ChangeError:
                raise
            except Exception as second:
                raise ChangeError(
                    f"TrueNAS call failed ({type(second).__name__})",
                    ErrorKind.TRANSIENT,
                ) from second

    def _reset(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def find_local_replica(self, username: str):
        """The local row for `username`, or None.

        A user can appear as both a local replica and a directory view.
        """
        try:
            rows = self._call("user.query", [["username", "=", username]]) or []
        except ChangeError:
            raise
        except Exception as e:
            raise ChangeError(
                f"TrueNAS lookup failed ({type(e).__name__})", ErrorKind.TRANSIENT
            ) from e
        for row in rows:
            if row.get("local"):
                return row
        return None

    def set_password(self, username: str, new_password: str) -> None:
        try:
            self._call(
                "user.set_password",
                {"username": username, "new_password": new_password},
            )
        except ChangeError:
            raise
        except Exception as e:
            raise ChangeError(
                f"TrueNAS rejected the change ({type(e).__name__})",
                ErrorKind.TRANSIENT,
            ) from e

    def close(self) -> None:
        self._reset()


class LdapClient:
    """Bind, and RFC 3062 Password Modify."""

    def __init__(
        self,
        uri: str,
        base_dn: str,
        *,
        timeout: int = 8,
        dn_template: str = DEFAULT_DN_TEMPLATE,
    ):
        self.uri = uri
        self.base_dn = base_dn
        self.timeout = timeout
        self.dn_template = dn_template

    def dn(self, username: str) -> str:
        return self.dn_template.format(username=username, base_dn=self.base_dn)

    def _connection(self, dn: str, password: str):
        try:
            return ldap3.Connection(
                ldap3.Server(self.uri, connect_timeout=self.timeout),
                user=dn,
                password=password,
                auto_bind=True,
                raise_exceptions=False,
            )
        except LDAPBindError:
            raise ChangeError(
                "invalid credentials", ErrorKind.INVALID_CREDENTIALS
            ) from None
        except LDAPException as e:
            raise ChangeError(f"cannot reach lldap: {e}", ErrorKind.TRANSIENT) from None

    def bind(self, username: str, password: str) -> None:
        conn = self._connection(self.dn(username), password)
        conn.unbind()

    def set_password(self, username: str, old_password: str, new_password: str) -> None:
        """RFC 3062 Password Modify. lldap requires the explicit identity."""
        dn = self.dn(username)
        conn = self._connection(dn, old_password)
        try:
            ok = ModifyPassword(
                conn, user=dn, old_password=old_password, new_password=new_password
            ).send()
        except LDAPException as e:
            raise ChangeError(
                f"lldap rejected the password change: {e}", ErrorKind.POLICY
            ) from None
        finally:
            with contextlib.suppress(Exception):
                conn.unbind()
        if not ok:
            raise ChangeError(
                "lldap rejected the new password (policy, or the current password "
                "is wrong)",
                ErrorKind.POLICY,
            )


def change_password(
    username: str,
    old_password: str,
    new_password: str,
    *,
    ldap_client,
    tn_client=None,
    min_len: int = 8,
) -> ChangeResult:
    """Authenticate, then update both stores, retriably."""
    username = (username or "").strip()
    if not username or not USERNAME_RE.match(username):
        raise ChangeError(
            "username is required and must be a simple name", ErrorKind.VALIDATION
        )
    if not old_password:
        raise ChangeError("current password is required", ErrorKind.VALIDATION)
    if len(new_password or "") < min_len:
        raise ChangeError(
            f"new password must be at least {min_len} characters",
            ErrorKind.VALIDATION,
        )
    if new_password == old_password:
        raise ChangeError(
            "new password must differ from the current one", ErrorKind.VALIDATION
        )

    result = ChangeResult(username=username)

    ldap_client.bind(username, old_password)

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
            # Detail is logged, not shown: the user gets the text for the kind.
            raise ChangeError(
                operator_text("nas_password_set_failed", e=e), ErrorKind.TRANSIENT
            ) from None

    try:
        ldap_client.set_password(username, old_password, new_password)
        result.lldap = True
    except ChangeError as e:
        result.lldap = False
        result.partial = result.truenas
        if result.truenas:
            # Attempt to put the NAS back, so the stores do not drift apart.
            try:
                tn_client.set_password(username, old_password)
                result.truenas = False
                result.partial = False
                result.reverted = True
            except Exception as revert_error:
                raise ChangeError(
                    operator_text("nas_set_but_directory_failed", e=revert_error),
                    ErrorKind.PARTIAL,
                ) from None
        raise ChangeError(
            str(e), ErrorKind.PARTIAL if result.partial else e.kind
        ) from None

    try:
        ldap_client.bind(username, new_password)
    except ChangeError:
        # Both stores accepted the password, so the new one is live even though
        # the check bind failed. Say so: guessing which password to use is worse
        # than being told to try the new one.
        raise ChangeError(
            operator_text("changed_but_unverified"), ErrorKind.UNVERIFIED
        ) from None
    return result


def build_ldap_client(env=None):
    """Build the directory client from the environment, or raise ConfigError."""
    env = env if env is not None else os.environ
    uri = env.get("PW_LDAP_URI", "")
    base = env.get("PW_LDAP_BASE", "")
    if not uri:
        raise ConfigError(operator_text("no_ldap_uri"))
    if not base:
        raise ConfigError(operator_text("no_ldap_base"))
    return LdapClient(
        uri, base, dn_template=env.get("PW_LDAP_DN_TEMPLATE", DEFAULT_DN_TEMPLATE)
    )


def build_tn_client(env=None):
    """Select the TrueNAS client by configuration, not by editing code."""
    env = env if env is not None else os.environ
    key = env.get("PW_TN_KEY", "")
    key_file = env.get("PW_TN_KEY_FILE", "")
    if not key and key_file and os.path.exists(key_file):
        with open(key_file, encoding="utf-8") as fh:
            key = fh.read().strip()
    wss = env.get("PW_TN_WSS", "")
    if not key:
        raise ConfigError(operator_text("no_api_key"))
    if not wss:
        raise ConfigError(operator_text("no_wss_url"))
    return TrueNasPasswordClient(wss, key), True
