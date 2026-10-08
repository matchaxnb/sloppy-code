#!/usr/bin/env python3
"""HTTP service for the password-change worker.

Serves the page and three JSON endpoints, delegating the actual change to
`core.change_password`.

Two addresses matter:
  * PW_LISTEN is the in-container bind; wildcard is normal and correct there.
  * PW_BIND is the host-side publish address and the real exposure control. The
    container runtime performs it, not this process; it is logged for awareness.

Passwords never appear in a log, a traceback or a response.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import authgate
import core
import messages
from messages import ErrorKind
from secret_string import SecretString

__all__ = ["Handler", "WorkerServer", "main"]

log = logging.getLogger("password_worker")

STATIC_DIR = Path(__file__).resolve().parent / "static"

DEFAULT_LDAP_URI = "ldap://127.0.0.1:389"
DEFAULT_LDAP_BASE = "dc=example,dc=lan"
DEFAULT_LISTEN = "0.0.0.0:8099"
DEFAULT_BIND = "127.0.0.1:8099"
DEFAULT_MIN_LEN = 8
MAX_BODY = 65_536

BANNER_DIR = os.environ.get("PW_BANNER_DIR", "/etc/pw-worker")
BANNER_FILE = os.environ.get("PW_BANNER_FILE", "banner.html")
BANNER_FILE_AUTHED = os.environ.get("PW_BANNER_FILE_AUTHED", "banner-authed.html")
VALID_BANNER_NAMES = (BANNER_FILE, BANNER_FILE_AUTHED)
MAX_BANNER = 8 * 1024 * 1024


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


MIN_LEN = _int_env("PW_MIN_LEN", DEFAULT_MIN_LEN)


def parse_listen(addr: str) -> tuple[str, int]:
    """Parse ``host:port`` into ``(host, port)``. IPv6 must be bracketed."""
    addr = (addr or "").strip() or DEFAULT_LISTEN
    if addr.startswith("["):
        close = addr.find("]")
        if close < 0:
            raise ValueError(f"malformed IPv6 address: {addr!r}")
        host = addr[1:close]
        tail = addr[close + 1 :]
        if not tail:
            return host, 8099
        if tail.startswith(":"):
            return host, int(tail[1:])
        raise ValueError(f"malformed IPv6 address: {addr!r}")
    if addr.count(":") == 1:
        host, _, port_s = addr.rpartition(":")
        return host, int(port_s)
    return addr, 8099


def scrub(text: str, *secrets: str) -> str:
    """Remove known secret values from a string before it is logged."""
    for s in secrets:
        if s and len(s) >= 3:
            text = text.replace(str(s), "<redacted>")
    return text


def log_exception(logger, message: str, *secrets: str) -> None:
    """Log the active exception with known secrets stripped from the traceback."""
    logger.error("%s\n%s", message, scrub(traceback.format_exc(), *secrets))


class BannerStore:
    """Banner fragments, read from a fixed directory and cached in memory.

    Reloaded on SIGUSR1 so an edit does not need a restart. No request input
    reaches the path: filenames are fixed and validated against a whitelist.
    """

    def __init__(self, directory: Path, names: dict[str, str]):
        self._dir = directory
        self._names = names
        self._cache: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self.reload()

    @property
    def names(self) -> dict[str, str]:
        return dict(self._names)

    def reload(self) -> None:
        fresh: dict[str, bytes] = {}
        for slot, name in self._names.items():
            if name not in VALID_BANNER_NAMES:
                log.warning("ignoring invalid banner filename for %s: %r", slot, name)
                fresh[slot] = b""
                continue
            path = self._dir / name
            try:
                if path.is_file() and path.stat().st_size <= MAX_BANNER:
                    fresh[slot] = path.read_bytes()
                else:
                    fresh[slot] = b""
            except OSError:
                fresh[slot] = b""
        with self._lock:
            self._cache = fresh
        log.info("banners loaded: %s", {k: len(v) for k, v in fresh.items()})

    def get(self, slot: str) -> bytes:
        with self._lock:
            return self._cache.get(slot, b"")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pw-worker/1.0"

    def log_message(self, fmt, *args):
        log.debug("http %s %s", self.address_string(), fmt % args)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, data: bytes):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_not_found(self):
        body = b"<h1>404 Not Found</h1>"
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, kind: ErrorKind):
        self._send_json(status, {"ok": False, "error": messages.error_text(kind)})

    def do_GET(self):
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        if self.path in ("/", "/index.html"):
            path = STATIC_DIR / "index.html"
            if path.is_file():
                self._send_html(path.read_bytes())
            else:
                self._send_not_found()
            return
        for slot, name in srv.banners.names.items():
            if self.path == f"/{name}":
                self._send_html(srv.banners.get(slot))
                return
        self._send_not_found()

    def do_POST(self):
        if self.path == "/api/change":
            self._handle_change()
        elif self.path == "/auth/user":
            self._handle_auth_user()
        elif self.path == "/auth/pass":
            self._handle_auth_pass()
        else:
            self._error(HTTPStatus.NOT_FOUND, ErrorKind.UNKNOWN)

    def _client_ip(self) -> str:
        return authgate.Gate.client_ip(
            self.headers.get("X-Forwarded-For"), self.client_address[0]
        )

    def _read_json(self) -> dict | None:
        """Parse a JSON object body, or answer and return None."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, ErrorKind.UNKNOWN)
            return None
        if length <= 0 or length > MAX_BODY:
            self._error(HTTPStatus.BAD_REQUEST, ErrorKind.UNKNOWN)
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, ErrorKind.UNKNOWN)
            return None
        if not isinstance(data, dict):
            self._error(HTTPStatus.BAD_REQUEST, ErrorKind.UNKNOWN)
            return None
        return data

    def _refuse(self, status: int, **fields) -> bool:
        """Send a refusal and return True, if the client must wait."""
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        ip = self._client_ip()
        denial = srv.gate.note_ip(ip)
        wait = srv.gate.retry_after(ip)
        time.sleep(authgate.auth_delay())

        if denial > 0:
            log.warning(
                "service denied: distinct-IP burst ip=%s wait=%.0fs", ip, denial
            )
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "error": messages.text("unavailable"),
                    "retry_after": int(denial) + 1,
                    **fields,
                },
            )
            return True
        if wait > 0:
            log.info("auth throttled ip=%s wait=%.0fs", ip, wait)
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "ok": False,
                    "error": messages.text("throttled", seconds=int(wait) + 1),
                    "retry_after": int(wait) + 1,
                    **fields,
                },
            )
            return True
        return False

    def _handle_auth_user(self):
        """Collect a username. Never fails."""
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        ip = self._client_ip()
        if self._refuse(HTTPStatus.OK):
            return
        data = self._read_json()
        if data is None:
            return
        username = data.get("username", "")
        if not isinstance(username, str):
            username = ""
        time.sleep(authgate.auth_delay())
        token = srv.gate.start_pending(username.strip(), ip)
        self._send_json(HTTPStatus.OK, {"ok": True, "ticket": token})

    def _handle_auth_pass(self):
        """Authenticate. Failures are indistinguishable and throttled."""
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        ip = self._client_ip()
        if self._refuse(HTTPStatus.OK):
            return
        data = self._read_json()
        if data is None:
            return
        ticket = data.get("ticket", "")
        raw_password = data.get("password", "")
        if not isinstance(ticket, str) or not isinstance(raw_password, str):
            self._error(HTTPStatus.BAD_REQUEST, ErrorKind.UNKNOWN)
            return
        password = SecretString(raw_password)

        pending = srv.gate.take_pending(ticket, ip)
        if pending is None:
            time.sleep(authgate.auth_delay())
            # A stale ticket is invalid but not an authentication failure.
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": messages.text("session_expired")},
            )
            return

        try:
            srv.ldap_client.bind(pending.username, password)
        except core.ChangeError:
            wait = srv.gate.record_failure(ip)
            log.info("auth failed ip=%s cooldown=%.0fs", ip, wait)
            # Re-issue another single-use ticket so a typo can be retyped.
            retry_ticket = srv.gate.start_pending(pending.username, ip)
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "ok": False,
                    "error": srv.gate.identical_failure(),
                    "retry_after": int(wait) + 1,
                    "ticket": retry_ticket,
                },
            )
            return
        except Exception:
            log_exception(log, f"unexpected auth error ip={ip}", password)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorKind.UNKNOWN)
            return

        # Hold the credentials briefly: /api/change then needs only the token.
        srv.gate.record_success(ip)
        token = srv.gate.start_session(pending.username, password, ip)
        self._send_json(HTTPStatus.OK, {"ok": True, "session": token})

    def _handle_change(self):
        """Change the password for an already-authenticated session."""
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        ip = self._client_ip()
        if self._refuse(HTTPStatus.OK):
            return
        data = self._read_json()
        if data is None:
            return

        session_token = data.get("session", "")
        raw_new = data.get("new_password", "")
        if not isinstance(session_token, str) or not isinstance(raw_new, str):
            self._error(HTTPStatus.BAD_REQUEST, ErrorKind.UNKNOWN)
            return
        new_password = SecretString(raw_new)

        session = srv.gate.take_session(session_token, ip)
        if session is None:
            time.sleep(authgate.auth_delay())
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": messages.text("not_authenticated")},
            )
            return

        username = session.username
        old_password = session.password

        try:
            result = core.change_password(
                username,
                old_password,
                new_password,
                ldap_client=srv.ldap_client,
                tn_client=srv.tn_client,
                min_len=MIN_LEN,
            )
        except core.ChangeError as e:
            log.info(
                "change failed user=%r reason=%s",
                _safe_username(username),
                str(e)[:200],
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": False,
                    "error": messages.error_text(e.kind),
                    "partial": e.kind is ErrorKind.PARTIAL,
                },
            )
            return
        except Exception:
            # Give the user a generic error message.
            log_exception(
                log,
                f"unexpected error during password change for "
                f"user={_safe_username(username)!r}",
                old_password,
                new_password,
            )
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, ErrorKind.UNKNOWN)
            return

        log.info(
            "change ok user=%r lldap=%s truenas=%s partial=%s",
            _safe_username(username),
            result.lldap,
            result.truenas,
            result.partial,
        )
        self._send_json(HTTPStatus.OK, public_result(result))


def public_result(result) -> dict:
    """The response body: only what the page needs to render.

    Internal booleans (`lldap`, `truenas`, `truenas_applicable`) are dropped:
    they describe the backend layout and mean nothing to a user.
    """
    if not result.ok:
        kind = ErrorKind.PARTIAL if result.partial else ErrorKind.UNKNOWN
        return {
            "ok": False,
            "partial": bool(result.partial),
            "error": messages.error_text(kind),
        }
    return {"ok": True, "partial": False, "message": messages.text("changed")}


def _safe_username(username: str) -> str:
    return username[:64] if isinstance(username, str) else "?"


class WorkerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ldap_client, tn_client, banners):
        super().__init__(addr, Handler)
        self.ldap_client = ldap_client
        self.tn_client = tn_client
        self.banners = banners
        self.gate = authgate.Gate()


def build_clients():
    """Build the ldap and TrueNAS clients from the environment."""
    ldap_client = core.LdapClient(
        os.environ.get("PW_LDAP_URI", DEFAULT_LDAP_URI),
        os.environ.get("PW_LDAP_BASE", DEFAULT_LDAP_BASE),
        dn_template=os.environ.get(
            "PW_LDAP_DN_TEMPLATE", "uid={username},ou=people,{base_dn}"
        ),
    )
    tn_client, _ = core.build_tn_client()
    return ldap_client, tn_client


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    listen_host, listen_port = parse_listen(os.environ.get("PW_LISTEN", DEFAULT_LISTEN))
    bind_host, bind_port = parse_listen(os.environ.get("PW_BIND", DEFAULT_BIND))
    ldap_client, tn_client = build_clients()

    banners = BannerStore(
        Path(BANNER_DIR),
        {"unauth": BANNER_FILE, "authed": BANNER_FILE_AUTHED},
    )
    signal.signal(signal.SIGUSR1, lambda *_: banners.reload())

    log.info("TrueNAS: %s", "real" if tn_client else "none")
    log.info("listen %s:%d (in-container)", listen_host, listen_port)
    log.info("publish %s:%d (host-side)", bind_host, bind_port)
    if bind_host in ("0.0.0.0", "::"):
        log.warning(
            "PW_BIND is a wildcard (%s); publishes on all host interfaces", bind_host
        )

    server = WorkerServer((listen_host, listen_port), ldap_client, tn_client, banners)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        log.info("stopped")


if __name__ == "__main__":
    main()
