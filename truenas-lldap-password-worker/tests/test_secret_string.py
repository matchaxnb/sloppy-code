#!/usr/bin/env python3
"""Offline tests for the credential wrapper and the response serializer.

The serializer matters because `SecretString` subclasses `str`: json.dumps
serializes it natively and never consults `default`, so a naive hook silently
leaks. These tests exist to catch exactly that.

Run:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from secret_string import SecretString, json_response  # noqa: E402

REAL = "hunter2-correct-horse"


class TestSecretString(unittest.TestCase):
    def test_repr_and_str_are_redacted(self):
        s = SecretString(REAL)
        self.assertNotIn(REAL, repr(s))
        self.assertNotIn(REAL, str(s))
        self.assertNotIn(REAL, f"{s}")
        self.assertNotIn(REAL, "{:s}".format(s))

    def test_repr_inside_a_tuple_is_redacted(self):
        # This is the traceback shape: repr() of an exception's arguments.
        self.assertNotIn(REAL, repr(("bind failed", SecretString(REAL))))

    def test_value_and_reveal_return_the_real_string(self):
        s = SecretString(REAL)
        self.assertEqual(s.value, REAL)
        self.assertEqual(s.reveal(), REAL)

    def test_still_behaves_as_a_str_for_apis(self):
        s = SecretString(REAL)
        self.assertTrue(isinstance(s, str))
        self.assertEqual(s.encode(), REAL.encode())
        self.assertEqual(s, REAL)
        self.assertEqual(len(s), len(REAL))


class TestJsonResponse(unittest.TestCase):
    def test_a_wrapped_secret_never_appears(self):
        out = json_response({"note": SecretString(REAL)}).decode()
        self.assertNotIn(REAL, out)
        self.assertIn("<secret>", out)

    def test_secret_is_redacted_at_every_nesting_level(self):
        payload = {
            "flat": SecretString(REAL),
            "nested": {"deep": SecretString(REAL)},
            "list": [SecretString(REAL), "plain"],
            "tuple": (SecretString(REAL),),
        }
        out = json_response(payload).decode()
        self.assertNotIn(REAL, out)

    def test_ordinary_payload_is_unchanged(self):
        out = json_response({"ok": True, "msg": "changed"}).decode()
        self.assertEqual(out, '{"ok": true, "msg": "changed"}')


if __name__ == "__main__":
    unittest.main()
