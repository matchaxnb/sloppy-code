#!/usr/bin/env python3
"""Offline unit tests proving the flow contract in core.py.

Pure standard-library unittest. No network, no clock, no filesystem.
Wires FakeLdapClient / FakeTrueNasClient into core.change_password and asserts
the ordering, retriability, partial-application, and validation contracts.

Run:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

# Make core.py importable regardless of where unittest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import messages  # noqa: E402
from core import ChangeError, change_password  # noqa: E402
from messages import ErrorKind  # noqa: E402
from tests.fakes import FakeLdapClient, FakeTrueNasClient, _CallLog  # noqa: E402

USER = "alice"
OLD = "oldpass123"
NEW = "newpass456"
REPLICA = {"id": 81, "local": True, "smb": True, "uid": 3005}


def _make(ldap_kw=None, tn_kw=None):
    """Build a shared-log pair of fakes so calls interleave correctly."""
    log = _CallLog()
    lk = {"current_password": OLD, **(ldap_kw or {})}
    tk = {"local_replica": REPLICA, **(tn_kw or {})}
    ldap = FakeLdapClient(log=log, **lk)
    tn = FakeTrueNasClient(log=log, **tk)
    return ldap, tn, log


class TestHappyPath(unittest.TestCase):
    def test_both_stores_changed_ok(self):
        ldap, tn, _ = _make()
        r = change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertTrue(r.ok)
        self.assertTrue(r.lldap)
        self.assertTrue(r.truenas)
        self.assertTrue(r.truenas_applicable)
        self.assertFalse(r.partial)


class TestOrdering(unittest.TestCase):
    """SPEC §6.1: bind(old) -> find_local_replica -> tn.set_password
    -> ldap.set_password -> bind(new). lldap is changed LAST."""

    def test_call_sequence(self):
        ldap, tn, log = _make()
        change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)

        expected = [
            ("bind", USER, OLD),  # 1. authenticate
            ("find_local_replica", USER),  # 2a. locate replica
            ("tn_set_password", USER, NEW),  # 2b. TrueNAS first
            ("set_password", USER, OLD, NEW),  # 3. lldap last
            ("bind", USER, NEW),  # 4. verify
        ]
        self.assertEqual(log.entries, expected)

    def test_truenas_before_lldap(self):
        """The NAS password is set before the directory password."""
        ldap, tn, log = _make()
        change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        tn_idx = next(i for i, e in enumerate(log.entries) if e[0] == "tn_set_password")
        ldap_idx = next(i for i, e in enumerate(log.entries) if e[0] == "set_password")
        self.assertLess(tn_idx, ldap_idx)

    def test_lldap_changed_last_before_verify(self):
        """No store mutation happens after the lldap set_password except verify."""
        ldap, tn, log = _make()
        change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        ldap_idx = next(i for i, e in enumerate(log.entries) if e[0] == "set_password")
        after = log.entries[ldap_idx + 1 :]
        # The only thing after the lldap change is the verification bind.
        self.assertEqual(after, [("bind", USER, NEW)])


class TestTrueNasFailureLeavesLldapUnchanged(unittest.TestCase):
    """SPEC §6.2: a TrueNAS failure means NOTHING changed -> clean retry."""

    def test_lldap_password_still_old(self):
        ldap, tn, _ = _make(tn_kw={"fail_set_password": True})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        # lldap.set_password was never called -> password unchanged.
        self.assertEqual(ldap.current_password, OLD)
        self.assertFalse(any(e[0] == "set_password" for e in ldap.calls))

    def test_user_can_still_bind_with_old(self):
        ldap, tn, _ = _make(tn_kw={"fail_set_password": True})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        # The old password must still authenticate — the retry precondition.
        ldap.bind(USER, OLD)  # must not raise


class TestLldapRejectsNewAfterTrueNasSuccess(unittest.TestCase):
    """When lldap refuses after the NAS was written, the NAS is put back."""

    def test_raises_change_error(self):
        ldap, tn, _ = _make(ldap_kw={"reject_new_password": True})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)

    def test_nas_is_reverted_to_the_old_password(self):
        ldap, tn, _ = _make(ldap_kw={"reject_new_password": True})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        # A second set_password back to OLD must have been attempted.
        writes = [e for e in tn.calls if e[0] == "tn_set_password"]
        self.assertEqual(writes[-1][2], OLD)

    def test_reverted_change_is_not_reported_as_partial(self):
        ldap, tn, _ = _make(ldap_kw={"reject_new_password": True})
        with self.assertRaises(ChangeError) as ctx:
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        # Rollback succeeded, so the stores agree: not a partial application.
        self.assertIsNot(ctx.exception.kind, ErrorKind.PARTIAL)

    def test_still_partial_when_rollback_fails(self):
        ldap, tn, _ = _make(
            ldap_kw={"reject_new_password": True}, tn_kw={"fail_set_password_after": 1}
        )
        with self.assertRaises(ChangeError) as ctx:
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertIs(ctx.exception.kind, ErrorKind.PARTIAL)
        self.assertIn("directory", str(ctx.exception).lower())

    def test_reverted_failure_keeps_the_directory_kind(self):
        # lldap refused, the NAS was put back: the user is told why lldap refused.
        ldap, tn, _ = _make(ldap_kw={"reject_new_password": True})
        with self.assertRaises(ChangeError) as ctx:
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertIs(ctx.exception.kind, ErrorKind.POLICY)
        self.assertNotIn("directory password is still", str(ctx.exception))

    def test_old_password_still_works(self):
        ldap, tn, _ = _make(ldap_kw={"reject_new_password": True})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        ldap.bind(USER, OLD)


class TestVerifyBindFailsAfterBothWrites(unittest.TestCase):
    """Both stores accepted the password but the check bind failed.

    The password IS changed, so the user must be told that rather than given a
    generic error and left guessing which password works.
    """

    def test_reported_as_unverified(self):
        ldap, tn, _ = _make(ldap_kw={"reject_verify_bind": True})
        with self.assertRaises(ChangeError) as ctx:
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertIs(ctx.exception.kind, ErrorKind.UNVERIFIED)

    def test_user_is_not_told_the_credentials_were_wrong(self):
        ldap, tn, _ = _make(ldap_kw={"reject_verify_bind": True})
        with self.assertRaises(ChangeError) as ctx:
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertIsNot(ctx.exception.kind, ErrorKind.INVALID_CREDENTIALS)
        # And the suggested next step names the new password.
        self.assertIn("new password", messages.error_text(ctx.exception.kind))

    def test_truenas_was_changed(self):
        ldap, tn, _ = _make(ldap_kw={"reject_new_password": True})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertIn(("tn_set_password", USER, NEW), tn.calls)


class TestNoLocalReplica(unittest.TestCase):
    """SPEC §6.4: no local replica -> truenas False, still ok."""

    def test_succeeds_without_truenas(self):
        ldap, tn, _ = _make(tn_kw={"local_replica": None})
        r = change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertTrue(r.ok)
        self.assertFalse(r.truenas)
        self.assertFalse(r.truenas_applicable)
        self.assertTrue(r.lldap)

    def test_truenas_set_password_not_called(self):
        ldap, tn, _ = _make(tn_kw={"local_replica": None})
        change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self.assertFalse(any(e[0] == "tn_set_password" for e in tn.calls))


class TestValidation(unittest.TestCase):
    """All validation failures raise ChangeError and touch no store."""

    def _assert_no_set_password(self, log):
        self.assertFalse(
            any(e[0] in ("set_password", "tn_set_password") for e in log.entries)
        )

    def test_malformed_username(self):
        ldap, tn, log = _make()
        # USERNAME_RE = ^[A-Za-z0-9][A-Za-z0-9._-]*$ — these are all invalid.
        for bad in [
            "",
            "  ",
            "-dash",
            "has space",
            "user@host",
            ".dotstart",
            "user/name",
        ]:
            with self.assertRaises(ChangeError, msg=f"expected failure for {bad!r}"):
                change_password(bad, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self._assert_no_set_password(log)

    def test_wrong_old_password(self):
        # A plain wrong password: bind with OLD fails because current_password
        # was set to something else.
        ldap, tn, log = _make(ldap_kw={"current_password": "somethingelse"})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
        self._assert_no_set_password(log)

    def test_too_short_new_password(self):
        ldap, tn, log = _make()
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, "short", ldap_client=ldap, tn_client=tn)
        self._assert_no_set_password(log)

    def test_new_equals_old(self):
        ldap, tn, log = _make()
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, OLD, ldap_client=ldap, tn_client=tn)
        self._assert_no_set_password(log)


class TestVerificationBind(unittest.TestCase):
    """SPEC §6.3: if the final bind(new) fails, do not report success."""

    def test_verify_failure_raises(self):
        ldap, tn, _ = _make(ldap_kw={"reject_verify_bind": True})
        with self.assertRaises(ChangeError):
            change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)

    def test_no_success_returned(self):
        ldap, tn, _ = _make(ldap_kw={"reject_verify_bind": True})
        try:
            r = change_password(USER, OLD, NEW, ldap_client=ldap, tn_client=tn)
            self.assertFalse(r.ok, "must not report success when verify bind fails")
        except ChangeError:
            pass  # raising is also acceptable


if __name__ == "__main__":
    unittest.main()
