#!/usr/bin/env python3
"""Authentication gate: two-stage login, cooldowns, and IP bans.

DESIGN CONSTRAINTS (all deliberate; do not "simplify" one away)
---------------------------------------------------------------
1. **Stage 1 never fails.** It takes a username and always succeeds. That is the
   whole point: a form that cannot fail cannot be used to enumerate accounts.
2. **Stage 2 is where authentication happens**, and its failure message is
   *identical* for "no such user" and "wrong password". Never differentiate.
3. **Constant-ish timing.** Both stages sleep a random ~1s. Response time must
   not distinguish "user exists" from "user does not exist", nor "bad password"
   from "fine".
4. **Cooldown after every failure; ban after N failures, with exponential
   backoff**, keyed by client IP.
5. **In memory only.** State is intentionally not persisted: a restart clears
   bans. A deliberate trade favouring availability over strictness, since the
   failure mode of the alternative is locking out legitimate users.
   Note it plainly rather than pretending otherwise.
6. **Passwords are held in memory only for the short life of a session**, then
   dropped. Never logged, never written down.

Client identity comes from `X-Forwarded-For`. We take the **rightmost** entry:
a client can prepend arbitrary values, but the proxy appends what it actually
saw, so the rightmost untouched-by-the-client value is the trustworthy one.
Taking the leftmost would let anyone spoof a ban onto an innocent address.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import random
import secrets
import threading
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------- configuration
FAIL_THRESHOLD = int(os.environ.get("PW_FAIL_THRESHOLD", "2"))   # failures before banning
BAN_BASE_SECONDS = float(os.environ.get("PW_BAN_BASE", "5"))     # first ban length
BAN_MAX_SECONDS = float(os.environ.get("PW_BAN_MAX", "900"))     # cap (15 min)
FIRST_FAIL_COOLDOWN = float(os.environ.get("PW_FIRST_COOLDOWN", "3"))
DELAY_MIN = float(os.environ.get("PW_DELAY_MIN", "0.8"))
DELAY_MAX = float(os.environ.get("PW_DELAY_MAX", "1.2"))
SESSION_TTL = float(os.environ.get("PW_SESSION_TTL", "300"))     # 5 min
MAX_TRACKED_IPS = int(os.environ.get("PW_MAX_TRACKED_IPS", "4096"))

# Circuit breaker: more than N distinct client addresses in a window refuses the
# whole service, unlike the per-IP ban. Separate from the ban logic because it
# affects every client.
DISTINCT_IPS_MAX = int(os.environ.get("PW_DISTINCT_IPS_MAX", "10"))
DISTINCT_IPS_WINDOW = float(os.environ.get("PW_DISTINCT_IPS_WINDOW", "60"))
DISTINCT_IPS_COOLDOWN = float(os.environ.get("PW_DISTINCT_IPS_COOLDOWN", "900"))

# Hard caps on the in-memory ticket/session stores. The circuit breaker keys on
# DISTINCT addresses, so one address can hammer stage 1 and inflate `_pending`
# without limit -- expiring entries is not enough when they are all still fresh.
# Exceeding a cap evicts the soonest-to-expire entry.
MAX_PENDING = int(os.environ.get("PW_MAX_PENDING", "4096"))
MAX_SESSIONS = int(os.environ.get("PW_MAX_SESSIONS", "1024"))

# Clients in these subnets are exempt from rate limiting, and X-Forwarded-For is
# believed only from peers inside them. Loopback is always trusted. Empty means
# no subnet is trusted and all clients share the proxy's address.
#   PW_TRUSTED_SUBNETS=10.0.0.0/8,192.168.5.0/24
TRUSTED_SUBNETS = [s.strip() for s in
                   os.environ.get("PW_TRUSTED_SUBNETS", "").split(",")
                   if s.strip()]


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

    Not an authorization check. It grants no capability: a trusted client still
    authenticates, still needs a valid single-use session, and still passes every
    password rule. It exempts the client from rate limiting, and allows its
    X-Forwarded-For to be believed (the reverse proxy is inside the subnet).
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
    """Random delay for the validation phases, ~1s.

    Applied unconditionally and in *both* stages so that neither the existence
    of an account nor the correctness of a password can be inferred from latency.
    """
    return random.uniform(DELAY_MIN, DELAY_MAX)


@dataclass
class FailState:
    count: int = 0
    banned_until: float = 0.0
    last_failure: float = 0.0


@dataclass
class Session:
    username: str
    password: str = field(repr=False)   # never let this reach a log or traceback
    ip: str
    expires: float


@dataclass
class Pending:
    """Stage 1 result: a username collected but NOT yet authenticated."""
    username: str
    ip: str
    expires: float


class Gate:
    """Failures, bans and pending sessions. Thread-safe; all state in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, FailState] = {}
        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, Pending] = {}
        # Circuit breaker: recent distinct IPs, and when we tripped.
        self._ip_seen: dict[str, float] = {}
        self._denied_until: float = 0.0

    # ------------------------------------------------------------ identity
    @staticmethod
    def client_ip(xff: str | None, peer: str | None) -> str:
        """Client IP from X-Forwarded-For, else the socket peer.

        XFF is honoured ONLY when the TCP peer is a trusted subnet (the reverse
        proxy). Otherwise the peer is authoritative: a public client must not be
        able to choose its own ban bucket, nor fake a burst of distinct
        addresses to trip the circuit breaker.

        Rightmost XFF entry when trusted: a client can prepend fake values, but
        the proxy appends what it actually saw, so the rightmost is the one we
        can believe. The leftmost would let anyone project a ban onto an
        innocent address.
        """
        if xff and is_trusted(peer):
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
        return peer or "unknown"

    # ------------------------------------------------- circuit breaker
    def note_ip(self, ip: str) -> float:
        """Record a seen client IP; return seconds of service denial (>0 = down).

        More than DISTINCT_IPS_MAX distinct addresses within DISTINCT_IPS_WINDOW
        seconds refuses EVERY request, regardless of source, for
        DISTINCT_IPS_COOLDOWN seconds.

        Trusted clients return 0 and are not counted, so LAN users arriving
        through the proxy cannot trip an outage against themselves.
        """
        if is_trusted(ip):
            return 0.0
        now = time.monotonic()
        with self._lock:
            # Expire old observations first, so a trickle never accumulates.
            cutoff = now - DISTINCT_IPS_WINDOW
            for k in [k for k, t in self._ip_seen.items() if t < cutoff]:
                self._ip_seen.pop(k, None)
            self._ip_seen[ip] = now

            if len(self._ip_seen) > DISTINCT_IPS_MAX:
                self._denied_until = max(self._denied_until, now + DISTINCT_IPS_COOLDOWN)
                # Clear, so the same burst does not re-arm the trigger the moment
                # the cooldown ends.
                self._ip_seen.clear()
            return max(0.0, self._denied_until - now)

    def denial_remaining(self) -> float:
        """Seconds of service denial remaining, or 0 if accepting requests."""
        now = time.monotonic()
        with self._lock:
            return max(0.0, self._denied_until - now)

    # -------------------------------------------------------------- bans
    def retry_after(self, ip: str) -> float:
        """Seconds the caller must wait, or 0 if not currently banned.

        Trusted-subnet clients are never banned: the ban exists to blunt an
        external attacker, and rate-limiting the LAN's own users (who arrive
        through the proxy with forwarded addresses) would only punish them for
        someone else's mistake.
        """
        if is_trusted(ip):
            return 0.0
        now = time.monotonic()
        with self._lock:
            st = self._fails.get(ip)
            if not st:
                return 0.0
            return max(0.0, st.banned_until - now)

    def record_failure(self, ip: str) -> float:
        """Register a failed authentication; return the cooldown applied.

        Failure 1            -> FIRST_FAIL_COOLDOWN (mandatory cooldown)
        Failure N>=threshold -> BAN_BASE * 2**(N-threshold), capped
        """
        now = time.monotonic()
        with self._lock:
            st = self._fails.get(ip) or FailState()
            st.count += 1
            st.last_failure = now

            if st.count >= FAIL_THRESHOLD:
                over = st.count - FAIL_THRESHOLD
                ban = min(BAN_BASE_SECONDS * (2 ** over), BAN_MAX_SECONDS)
                st.banned_until = now + ban
            else:
                ban = FIRST_FAIL_COOLDOWN
                st.banned_until = now + ban

            self._fails[ip] = st
            self._prune_fails_locked(now)
            return ban

    def record_success(self, ip: str) -> None:
        """Clear the failure record — a successful login resets the backoff."""
        with self._lock:
            self._fails.pop(ip, None)

    def _prune_fails_locked(self, now: float) -> None:
        if len(self._fails) <= MAX_TRACKED_IPS:
            return
        # drop the oldest, keeping anything still banned
        for ip, st in sorted(self._fails.items(), key=lambda kv: kv[1].last_failure):
            if len(self._fails) <= MAX_TRACKED_IPS:
                break
            if st.banned_until > now:
                continue
            self._fails.pop(ip, None)

    @staticmethod
    def _evict_soonest(items: dict, cap: int) -> None:
        """Trim `items` to `cap`, dropping the entries that expire first.

        Evicting back to HALF the cap amortises the sort: it runs once per
        cap/2 insertions instead of on every one, which would be O(n log n) per
        request. An evicted ticket just restarts the flow.
        """
        if len(items) <= cap:
            return
        target = max(1, cap // 2)
        over = len(items) - target
        soonest = sorted(items.items(), key=lambda kv: kv[1].expires)[:over]
        for key, _ in soonest:
            items.pop(key, None)

    # ------------------------------------------------------------ stage 1/2
    def start_pending(self, username: str, ip: str) -> str:
        """Stage 1: record a username and hand back an opaque ticket.

        This CANNOT fail. Any username is accepted, valid or not — that is the
        property that prevents account enumeration.
        """
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._pending[token] = Pending(
                username=username, ip=ip, expires=now + SESSION_TTL)
            # Amortise the expiry sweep: an O(n) scan on every request is itself
            # a denial-of-service at the cap. The cap below bounds memory
            # regardless, so a periodic sweep is sufficient.
            if len(self._pending) % 64 == 0:
                for tok, p in list(self._pending.items()):
                    if p.expires < now:
                        self._pending.pop(tok, None)
            self._evict_soonest(self._pending, MAX_PENDING)
        return token

    def take_pending(self, token: str, ip: str) -> Pending | None:
        """Consume a stage-1 ticket (single use; bound to the client IP)."""
        now = time.monotonic()
        with self._lock:
            p = self._pending.pop(token or "", None)
        if p is None:
            return None
        if p.expires < now or p.ip != ip:
            return None
        return p

    # ---------------------------------------------------------- sessions
    def start_session(self, username: str, password: str, ip: str) -> str:
        """Create a short-lived session holding the VERIFIED credentials."""
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._sessions[token] = Session(
                username=username, password=password, ip=ip,
                expires=now + SESSION_TTL,
            )
            self._prune_sessions_locked(now)
            self._evict_soonest(self._sessions, MAX_SESSIONS)
        return token

    def take_session(self, token: str, ip: str) -> Session | None:
        """Consume a session (single use). Returns None if invalid/expired/wrong IP."""
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
        for tok, s in list(self._sessions.items()):
            if s.expires < now:
                self._sessions.pop(tok, None)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def identical_failure() -> str:
        """The ONE message used for every stage-2 failure.

        Deliberately covers both 'no such user' and 'wrong password'. Do not
        introduce a variant — that would reintroduce account enumeration.
        """
        return "Invalid username or password."

    @staticmethod
    def constant_time_compare(a: str, b: str) -> bool:
        return hmac.compare_digest(a.encode(), b.encode())
