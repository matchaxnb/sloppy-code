#!/usr/bin/env python3
"""Offline unit tests for the gate's identity trust, bans, and circuit breaker.

Pure standard-library unittest. No network, no clock (time.monotonic is patched
where the window matters), no filesystem.

Covers:
  * X-Forwarded-For is honoured only from a private/RFC1918 peer
  * the ban grows exponentially and is capped
  * the circuit breaker trips on >N distinct IPs in a window, and only then
  * a single IP cannot trip the breaker by repeating
  * old observations age out of the window

Run:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

# Make authgate.py importable regardless of invocation directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import authgate  # noqa: E402
from authgate import Gate, is_trusted  # noqa: E402


class TrustedSubnetMixin:
    """Trusted subnets are configuration, so tests must set their own.

    There is no built-in default: `is_trusted` is only meaningful relative to a
    configured network.
    """

    SUBNET = "198.18.0.0/15"      # a range that is not anyone's real LAN

    def setUp(self):
        self._saved = authgate._TRUSTED_NETS
        authgate._TRUSTED_NETS = authgate._subnet_list([self.SUBNET])

    def tearDown(self):
        authgate._TRUSTED_NETS = self._saved


class TestTrustedSubnet(TrustedSubnetMixin, unittest.TestCase):
    def test_in_subnet_is_trusted(self):
        for addr in ("198.18.0.1", "198.18.5.50", "198.19.255.254"):
            self.assertTrue(is_trusted(addr), addr)

    def test_loopback_is_always_trusted_without_configuration(self):
        authgate._TRUSTED_NETS = []      # nothing configured
        for addr in ("127.0.0.1", "127.0.0.5", "::1"):
            self.assertTrue(is_trusted(addr), addr)

    def test_other_ranges_are_not_trusted(self):
        for addr in ("192.168.1.5", "10.0.0.1", "172.16.0.1"):
            self.assertFalse(is_trusted(addr), addr)

    def test_public_addresses_are_not_trusted(self):
        for addr in ("8.8.8.8", "203.0.113.9", "2001:4860::1", ""):
            self.assertFalse(is_trusted(addr), addr)

    def test_malformed_address_is_not_trusted(self):
        self.assertFalse(is_trusted("not-an-ip"))


class TestClientIp(TrustedSubnetMixin, unittest.TestCase):
    def test_xff_used_when_peer_is_trusted(self):
        self.assertEqual(
            Gate.client_ip("203.0.113.9, 198.51.100.4", "198.18.0.9"),
            "198.51.100.4")  # rightmost

    def test_xff_ignored_when_nothing_is_configured(self):
        # With no trusted subnet every client is seen as the proxy itself.
        authgate._TRUSTED_NETS = []
        self.assertEqual(Gate.client_ip("1.2.3.4", "8.8.8.8"), "8.8.8.8")

    def test_xff_ignored_when_peer_is_untrusted(self):
        # A public client must not be able to choose its own ban bucket.
        self.assertEqual(Gate.client_ip("1.2.3.4", "8.8.8.8"), "8.8.8.8")

    def test_peer_used_when_no_xff(self):
        self.assertEqual(Gate.client_ip(None, "8.8.8.8"), "8.8.8.8")

    def test_unknown_when_nothing(self):
        self.assertEqual(Gate.client_ip(None, None), "unknown")


class TestTrustedClientsAreExempt(unittest.TestCase):
    """The LAN must not be rate-limited for a public client's behaviour.

    Exemption is the ONLY thing trust does. It must not skip authentication,
    a session, or any password rule.
    """

    def setUp(self):
        self._saved = authgate._TRUSTED_NETS
        authgate._TRUSTED_NETS = authgate._subnet_list(["198.18.0.0/15"])

    def tearDown(self):
        authgate._TRUSTED_NETS = self._saved

    def test_trusted_ip_is_never_banned(self):
        g = Gate()
        for _ in range(20):
            g.record_failure("198.18.0.10")
        self.assertEqual(g.retry_after("198.18.0.10"), 0)

    def test_trusted_ips_do_not_trip_the_breaker(self):
        g = Gate()
        for i in range(authgate.DISTINCT_IPS_MAX * 3):
            self.assertEqual(g.note_ip("198.18.0.%d" % (i % 250 + 1)), 0)
        self.assertEqual(g.denial_remaining(), 0)
        self.assertEqual(g._ip_seen, {})

    def test_trust_grants_no_session_or_ticket(self):
        # Being exempt from rate limiting must not create any access.
        g = Gate()
        self.assertIsNone(g.take_session("anything", "198.18.0.10"))
        self.assertIsNone(g.take_pending("anything", "198.18.0.10"))

    def test_server_never_consults_trust(self):
        # The change path must not know about trust at all. If this fails,
        # someone has made trust an authorization decision.
        import pathlib
        src = pathlib.Path(__file__).resolve().parent.parent / "server.py"
        text = src.read_text()
        self.assertNotIn("is_trusted", text)
        self.assertNotIn("trusted", text.lower().replace("untrusted", ""))


class TestBanGrowth(unittest.TestCase):
    def test_first_failure_is_the_plain_cooldown(self):
        g = Gate()
        self.assertEqual(g.record_failure("1.1.1.1"),
                         authgate.FIRST_FAIL_COOLDOWN)

    def test_ban_doubles_then_caps(self):
        g = Gate()
        ip = "2.2.2.2"
        # drive to the threshold, then observe the growth
        for _ in range(authgate.FAIL_THRESHOLD):
            g.record_failure(ip)
        seen = []
        for _ in range(12):
            seen.append(g.record_failure(ip))
        self.assertLessEqual(max(seen), authgate.BAN_MAX_SECONDS)
        self.assertTrue(any(b > authgate.FIRST_FAIL_COOLDOWN for b in seen))

    def test_success_clears_the_record(self):
        g = Gate()
        g.record_failure("3.3.3.3")
        self.assertGreater(g.retry_after("3.3.3.3"), 0)
        g.record_success("3.3.3.3")
        self.assertEqual(g.retry_after("3.3.3.3"), 0)


class TestCircuitBreaker(unittest.TestCase):
    def test_trips_only_above_the_threshold(self):
        g = Gate()
        n = authgate.DISTINCT_IPS_MAX
        for i in range(n):
            self.assertEqual(g.note_ip("10.0.0.%d" % i), 0,
                             "tripped too early at %d" % i)
        # the (n+1)th distinct address trips it
        self.assertGreater(g.note_ip("10.0.0.99"), 0)

    def test_one_ip_repeating_never_trips_it(self):
        g = Gate()
        for _ in range(authgate.DISTINCT_IPS_MAX * 5):
            self.assertEqual(g.note_ip("10.0.0.5"), 0)

    def test_old_observations_age_out_of_the_window(self):
        g = Gate()
        for i in range(authgate.DISTINCT_IPS_MAX):
            g.note_ip("10.1.0.%d" % i)
        # age everything past the window
        stale = authgate.DISTINCT_IPS_WINDOW + 1
        g._ip_seen = {k: t - stale for k, t in g._ip_seen.items()}
        self.assertEqual(g.note_ip("10.1.0.99"), 0)

    def test_denial_blocks_everyone_not_just_the_source(self):
        g = Gate()
        for i in range(authgate.DISTINCT_IPS_MAX + 1):
            g.note_ip("10.2.0.%d" % i)
        self.assertGreater(g.denial_remaining(), 0)
        # an unrelated address is refused for the same duration
        self.assertGreater(g.note_ip("10.9.9.9"), 0)


class TestBoundedMemory(unittest.TestCase):
    """The ticket/session stores must not grow with request volume.

    A single address can hammer stage 1 (the circuit breaker keys on DISTINCT
    addresses), so an unbounded `_pending` is a denial-of-service vector.

    The caps are temporarily lowered so the test proves the *behaviour* without
    inserting thousands of entries.
    """

    def setUp(self):
        self._pending_cap = authgate.MAX_PENDING
        self._session_cap = authgate.MAX_SESSIONS
        authgate.MAX_PENDING = 64
        authgate.MAX_SESSIONS = 32

    def tearDown(self):
        authgate.MAX_PENDING = self._pending_cap
        authgate.MAX_SESSIONS = self._session_cap

    def test_pending_store_is_capped(self):
        g = Gate()
        for _ in range(authgate.MAX_PENDING * 6):
            g.start_pending("user", "10.0.0.1")
        self.assertLessEqual(len(g._pending), authgate.MAX_PENDING)

    def test_session_store_is_capped(self):
        g = Gate()
        for _ in range(authgate.MAX_SESSIONS * 6):
            g.start_session("user", "pw", "10.0.0.1")
        self.assertLessEqual(len(g._sessions), authgate.MAX_SESSIONS)

    def test_a_front_running_ticket_is_evicted_but_the_newest_survives(self):
        g = Gate()
        for _ in range(authgate.MAX_PENDING * 6):
            g.start_pending("user", "10.0.0.1")
        newest = g.start_pending("user", "10.0.0.1")
        self.assertIsNotNone(g.take_pending(newest, "10.0.0.1"))


if __name__ == "__main__":
    unittest.main()
