"""In-memory stand-ins for the two boundaries in core.py.

No network, no clock, no filesystem. Calls are recorded in one shared ordered
log so a test can assert the exact interleaved sequence. FakeLdapClient tracks
the live password, so bind() succeeds only with the current one.
"""

from __future__ import annotations

from core import ChangeError
from messages import ErrorKind


class _CallLog:
    """A shared, ordered list of boundary calls across both fakes."""

    def __init__(self):
        self.entries = []

    def record(self, entry):
        self.entries.append(entry)


class FakeLdapClient:
    """In-memory lldap boundary: records calls, tracks the live password."""

    def __init__(
        self,
        *,
        current_password="oldpass",
        reject_new_password=False,
        reject_verify_bind=False,
        log=None,
    ):
        self.current_password = current_password
        self.reject_new_password = reject_new_password
        self.reject_verify_bind = reject_verify_bind
        self._set_password_done = False
        self._log = log if log is not None else _CallLog()

    @property
    def calls(self):
        """This fake's calls only (filtered from the shared log)."""
        return [
            e
            for e in self._log.entries
            if e[0] in ("bind", "set_password") and len(e) > 2
        ]

    # -- public boundary interface ----------------------------------------
    def bind(self, username, password):
        self._log.record(("bind", username, password))
        # If set_password already ran and we're configured to reject the
        # verification bind, fail it — simulates the new password not yet
        # being visible / accepted.
        if self._set_password_done and self.reject_verify_bind:
            raise ChangeError("verification bind failed")
        # Normal behaviour: only the live password binds.
        if password != self.current_password:
            raise ChangeError("invalid credentials", ErrorKind.INVALID_CREDENTIALS)

    def set_password(self, username, old_password, new_password):
        self._log.record(("set_password", username, old_password, new_password))
        self._set_password_done = True
        if self.reject_new_password:
            raise ChangeError(
                "lldap rejected the new password (policy, or the current "
                "password is wrong)",
                ErrorKind.POLICY,
            )
        # On success the live password flips: old stops working, new binds.
        self.current_password = new_password


class FakeTrueNasClient:
    """In-memory TrueNAS boundary: records calls, tracks local replica."""

    def __init__(
        self,
        *,
        local_replica=None,
        fail_set_password=False,
        set_password_exc=None,
        fail_set_password_after=None,
        log=None,
    ):
        # local_replica: a row dict (e.g. {"id": 81, "local": True, "smb": True})
        # or None when no local replica exists.
        self._local_replica = local_replica
        self.fail_set_password = fail_set_password
        self.set_password_exc = set_password_exc  # alternative exception type
        # Fail only from the Nth set_password onwards, so a test can let the
        # first write succeed and the compensating write fail.
        self.fail_set_password_after = fail_set_password_after
        self._set_password_count = 0
        self._log = log if log is not None else _CallLog()

    @property
    def calls(self):
        return [
            e
            for e in self._log.entries
            if e[0] in ("find_local_replica", "tn_set_password")
        ]

    # -- public boundary interface ----------------------------------------
    def find_local_replica(self, username):
        self._log.record(("find_local_replica", username))
        return self._local_replica

    def set_password(self, username, new_password):
        self._log.record(("tn_set_password", username, new_password))
        self._set_password_count += 1
        fail_now = self.fail_set_password or (
            self.fail_set_password_after is not None
            and self._set_password_count > self.fail_set_password_after
        )
        if fail_now:
            if self.set_password_exc is not None:
                raise self.set_password_exc("nas unreachable")
            raise ChangeError(
                "nothing was changed: the NAS password could not be set "
                "(nas unreachable). Your current password still works; "
                "please try again.",
                ErrorKind.TRANSIENT,
            )
