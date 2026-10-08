#!/usr/bin/env python3
"""Two-stage login gate: pending tickets, sessions, cooldowns, IP bans.

In-memory only: a restart clears bans and sessions.

Invariants that must not be "simplified" away:
  * stage 1 never fails, so the form cannot enumerate accounts;
  * the stage-2 failure message is identical for every cause;
  * sessions and tickets are single-use and bound to the client IP.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import random
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta

__all__ = ["Gate", "auth_delay", "is_trusted"]


def _seconds(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _count(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


FAIL_THRESHOLD = _count("PW_FAIL_THRESHOLD", 2)
BAN_BASE = timedelta(seconds=_seconds("PW_BAN_BASE", 5))
BAN_MAX = timedelta(seconds=_seconds("PW_BAN_MAX", 900))
FIRST_FAIL_COOLDOWN = timedelta(seconds=_seconds("PW_FIRST_COOLDOWN", 3))
DELAY_BASE = _seconds("PW_DELAY_BASE", 0.8)
DELAY_VARIANCE = _seconds("PW_DELAY_VARIANCE", 0.4)
SESSION_TTL = timedelta(seconds=_seconds("PW_SESSION_TTL", 300))
MAX_TRACKED_IPS = _count("PW_MAX_TRACKED_IPS", 4096)

# More than PW_DISTINCT_IPS_MAX distinct addresses in PW_DISTINCT_IPS_WINDOW
# refuses the whole service for PW_DISTINCT_IPS_COOLDOWN.
DISTINCT_IPS_MAX = _count("PW_DISTINCT_IPS_MAX", 10)
DISTINCT_IPS_WINDOW = timedelta(seconds=_seconds("PW_DISTINCT_IPS_WINDOW", 60))
DISTINCT_IPS_COOLDOWN = timedelta(seconds=_seconds("PW_DISTINCT_IPS_COOLDOWN", 900))

MAX_PENDING = _count("PW_MAX_PENDING", 4096)
MAX_SESSIONS = _count("PW_MAX_SESSIONS", 1024)

# Clients in these subnets are exempt from rate limiting, and X-Forwarded-For is
# believed only from peers inside them. Loopback is always trusted.
TRUSTED_SUBNETS = [
    s.strip() for s in os.environ.get("PW_TRUSTED_SUBNETS", "").split(",") if s.strip()
]


def _subnet_list(spec: list[str]) -> list:
    nets = []
    for part in spec:
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


_TRUSTED_NETS = _subnet_list(TRUSTED_SUBNETS)


def is_trusted(addr: str | None) -> bool:
    """True if `addr` is loopback or inside a configured trusted subnet.

    Not an authorization check. It only exempts the client from rate limiting
    and allows its X-Forwarded-For to be believed.
    """
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return any(ip in net for net in _TRUSTED_NETS)


def auth_delay() -> float:
    """A randomised delay for the validation phases, applied in both stages."""
    return random.uniform(DELAY_BASE, DELAY_BASE + DELAY_VARIANCE)


@dataclass
class FailState:
    count: int = 0
    # Seconds since a fixed origin (time.monotonic), so a float.
    banned_until: float = 0.0
    last_failure: float = 0.0


@dataclass
class Session:
    username: str
    password: str
    ip: str
    expires: float


@dataclass
class Pending:
    """Stage 1 result: a username collected but not yet authenticated."""

    username: str
    ip: str
    expires: float


class Gate:
    """Failures, bans, tickets and sessions. Thread-safe; all state in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, FailState] = {}
        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, Pending] = {}
        self._ip_seen: dict[str, float] = {}
        self._denied_until: float = 0.0

    @staticmethod
    def client_ip(xff: str | None, peer: str | None) -> str:
        """Client IP from X-Forwarded-For, else the socket peer.

        XFF is honoured only when the peer is a trusted subnet. The rightmost
        entry is used: a proxy appends what it saw, a client can prepend anything.
        """
        if xff and is_trusted(peer):
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
        return peer or "unknown"

    def note_ip(self, ip: str) -> float:
        """Record a seen client IP; return seconds of service denial.

        Trusted clients return 0 and are not counted.
        """
        if is_trusted(ip):
            return 0.0
        now = time.monotonic()
        with self._lock:
            cutoff = now - DISTINCT_IPS_WINDOW.total_seconds()
            for k in [k for k, t in self._ip_seen.items() if t < cutoff]:
                self._ip_seen.pop(k, None)
            self._ip_seen[ip] = now

            if len(self._ip_seen) > DISTINCT_IPS_MAX:
                self._denied_until = max(
                    self._denied_until,
                    now + DISTINCT_IPS_COOLDOWN.total_seconds(),
                )
                # Clear, so the same burst does not re-arm the trigger at once.
                self._ip_seen.clear()
            return max(0.0, self._denied_until - now)

    def denial_remaining(self) -> float:
        """Seconds of service denial remaining, or 0 if accepting requests."""
        now = time.monotonic()
        with self._lock:
            return max(0.0, self._denied_until - now)

    def retry_after(self, ip: str) -> float:
        """Seconds the caller must wait, or 0 if not currently banned."""
        if is_trusted(ip):
            return 0.0
        now = time.monotonic()
        with self._lock:
            st = self._fails.get(ip)
            if not st:
                return 0.0
            return max(0.0, st.banned_until - now)

    def record_failure(self, ip: str) -> float:
        """Register a failed authentication; return the cooldown applied."""
        now = time.monotonic()
        with self._lock:
            st = self._fails.get(ip) or FailState()
            st.count += 1
            st.last_failure = now

            if st.count >= FAIL_THRESHOLD:
                over = st.count - FAIL_THRESHOLD
                ban = min(BAN_BASE * (2**over), BAN_MAX)
            else:
                ban = FIRST_FAIL_COOLDOWN
            st.banned_until = now + ban.total_seconds()

            self._fails[ip] = st
            self._prune_fails_locked(now)
            return ban.total_seconds()

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    def _prune_fails_locked(self, now: float) -> None:
        if len(self._fails) <= MAX_TRACKED_IPS:
            return
        for ip, st in sorted(self._fails.items(), key=lambda kv: kv[1].last_failure):
            if len(self._fails) <= MAX_TRACKED_IPS:
                break
            if st.banned_until > now:
                continue
            self._fails.pop(ip, None)

    @staticmethod
    def _evict_soonest(items: dict, cap: int) -> None:
        """Trim `items` to `cap`, dropping the entries that expire first.

        Evicting to half the cap amortises the sort; an evicted ticket just
        restarts the flow.
        """
        if len(items) <= cap:
            return
        target = max(1, cap // 2)
        soonest = sorted(items.items(), key=lambda kv: kv[1].expires)[
            : len(items) - target
        ]
        for key, _ in soonest:
            items.pop(key, None)

    def start_pending(self, username: str, ip: str) -> str:
        """Stage 1: record a username and hand back a ticket.

        Cannot fail: any username is accepted, valid or not.
        """
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._pending[token] = Pending(
                username=username, ip=ip, expires=now + SESSION_TTL.total_seconds()
            )
            if len(self._pending) % 64 == 0:
                for tok, p in list(self._pending.items()):
                    if p.expires < now:
                        self._pending.pop(tok, None)
            self._evict_soonest(self._pending, MAX_PENDING)
        return token

    def take_pending(self, token: str, ip: str) -> Pending | None:
        """Consume a ticket (single use, bound to the client IP)."""
        now = time.monotonic()
        with self._lock:
            p = self._pending.pop(token or "", None)
        if p is None:
            return None
        if p.expires < now or p.ip != ip:
            return None
        return p

    def start_session(self, username: str, password: str, ip: str) -> str:
        """Create a short-lived session holding the verified credentials."""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._sessions[token] = Session(
                username=username,
                password=password,
                ip=ip,
                expires=now + SESSION_TTL.total_seconds(),
            )
            self._prune_sessions_locked(now)
            self._evict_soonest(self._sessions, MAX_SESSIONS)
        return token

    def take_session(self, token: str, ip: str) -> Session | None:
        """Consume a session (single use). None if invalid, expired, or wrong IP."""
        now = time.monotonic()
        with self._lock:
            s = self._sessions.pop(token or "", None)
        if s is None:
            return None
        if s.expires < now or s.ip != ip:
            return None
        return s

    def drop_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def _prune_sessions_locked(self, now: float) -> None:
        for tok in [t for t, s in self._sessions.items() if s.expires < now]:
            self._sessions.pop(tok, None)

    @staticmethod
    def constant_time_compare(a: str, b: str) -> bool:
        return hmac.compare_digest(a.encode(), b.encode())
