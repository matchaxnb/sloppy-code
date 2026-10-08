#!/usr/bin/env python3
"""HTTP service for the lldap + TrueNAS password-change worker.

Standard library only (http.server / socketserver).  Serves the
change-password page and a single JSON endpoint that delegates to
``core.change_password``.

Security
--------
* ``PW_LISTEN`` is the *in-container* bind address.  ``0.0.0.0``/``::`` is
  normal and expected there: a container's netns has no LAN address, and
  wildcard means "all interfaces of this netns" — it is NOT the exposure
  risk.
* ``PW_BIND`` is the *host-side* publish address (the real exposure control,
  decided by the port mapping).  Defaults to the LAN address so the service
  is never accidentally published on a public interface.  This variable is
  logged at startup for the operator's awareness; the actual host-side bind
  is performed by the container runtime, not by this process.
* Never logs passwords, hashes, or the API key.
* Returns generic messages for unexpected exceptions (no tracebacks).
* ``ChangeError`` messages are already user-safe and returned verbatim.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import authgate
import core

log = logging.getLogger("password_worker")


def scrub(text: str, *secrets: str) -> str:
    """Remove known secret values from a string before it is logged.

    Tracebacks render repr() of an exception's arguments, so a dependency that
    raises with the password among its args would print it. Callers pass the
    values they hold, so the redaction is exact rather than a guess at what a
    secret looks like.
    """
    for s in secrets:
        if s and len(s) >= 3:
            text = text.replace(s, "<redacted>")
    return text


def log_exception(logger, message: str, *secrets: str, **kw) -> None:
    """log.exception() with the given secrets stripped from the traceback."""
    import traceback
    tb = traceback.format_exc()
    logger.error("%s\n%s", message, scrub(tb, *secrets), **kw)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---- defaults (see the README) ---------------------------------------------
# No host addresses are baked in: there is no universally correct value, and
# a wrong guess either fails obscurely or exposes the service. Everything
# deployment-specific comes from the environment.
DEFAULT_LDAP_URI = "ldap://127.0.0.1:389"
DEFAULT_LDAP_BASE = "dc=example,dc=lan"
DEFAULT_LISTEN = "0.0.0.0:8099"      # in-container bind; wildcard is normal
# Host-side publish: loopback by default. Refusing to guess a LAN address
# means a misconfigured deployment is unreachable rather than exposed.
# Set PW_BIND (or PW_LISTEN) to the address clients should reach.
DEFAULT_BIND = "127.0.0.1:8099"
DEFAULT_MIN_LEN = 8
MAX_BODY = 65_536  # reject oversized payloads


# --------------------------------------------------------------------------- #
#  Configuration helpers
# --------------------------------------------------------------------------- #
def parse_listen(addr: str) -> tuple[str, int]:
    """Parse a ``host:port`` string into ``(host, port)``.

    This is used for BOTH ``PW_LISTEN`` (the in-container bind) and ``PW_BIND``
    (the host-side publish).  Wildcard addresses (``0.0.0.0``, ``::``) are
    ALLOWED: for the in-container bind they are normal and correct; the real
    exposure control is the host-side port mapping, surfaced via ``PW_BIND``.
    IPv6 literals should be bracketed, e.g. ``[::1]:8099``.
    """
    addr = (addr or "").strip() or DEFAULT_LISTEN
    if addr.startswith("["):
        # IPv6 literal:  [host]:port
        close = addr.find("]")
        if close < 0:
            raise ValueError(f"malformed IPv6 address: {addr!r}")
        host = addr[1:close]
        tail = addr[close + 1:]
        if not tail:
            port = 8099
        elif tail.startswith(":"):
            port = int(tail[1:])
        else:
            raise ValueError(f"malformed IPv6 address: {addr!r}")
    elif addr.count(":") == 1:
        host, _, port_s = addr.rpartition(":")
        port = int(port_s)
    else:
        # No colon (bare hostname) or multiple colons (bare IPv6): treat as
        # host-only on the default port.
        host = addr
        port = 8099
    return host, port


def build_clients() -> tuple[object, object, bool]:
    """Build the ldap and TrueNAS clients from the environment."""
    ldap_client = core.LdapClient(
        os.environ.get("PW_LDAP_URI", DEFAULT_LDAP_URI),
        os.environ.get("PW_LDAP_BASE", DEFAULT_LDAP_BASE),
    )
    tn_client, is_real = core.build_tn_client()
    return ldap_client, tn_client, is_real


# --------------------------------------------------------------------------- #
#  Optional banners
# --------------------------------------------------------------------------- #
# Two HTML fragments, one per authentication state:
#
#   PW_BANNER_FILE        shown while UNAUTHENTICATED
#   PW_BANNER_FILE_AUTHED shown after sign-in
#
# Read from PW_BANNER_DIR under a fixed filename per slot, so no part of a
# request reaches the path. Each file is a fragment (no <html>/<body>), served
# as-is with the page's security headers and no caching.
BANNER_DIR = os.environ.get("PW_BANNER_DIR", "/etc/pw-worker")
BANNER_FILE = os.environ.get("PW_BANNER_FILE", "banner.html")
BANNER_FILE_AUTHED = os.environ.get("PW_BANNER_FILE_AUTHED", "banner-authed.html")
MAX_BANNER = 64 * 1024


def _read_fragment(name_cfg: str, slot: str) -> bytes:
    """Return one banner fragment, or b'' if unset/absent/invalid."""
    name = os.path.basename(name_cfg or "")
    if not name or name != name_cfg or name.startswith("."):
        log.warning("ignoring invalid banner filename for %s: %r", slot, name_cfg)
        return b""
    path = Path(BANNER_DIR) / name
    try:
        if not path.is_file() or path.stat().st_size > MAX_BANNER:
            return b""
        return path.read_bytes()
    except OSError:
        return b""


def read_banner(authed: bool = False) -> bytes:
    """The banner for the requested authentication state."""
    return _read_fragment(BANNER_FILE_AUTHED if authed else BANNER_FILE,
                          "authed" if authed else "unauthenticated")


# --------------------------------------------------------------------------- #
#  Request handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pw-worker/1.0"

    # -- suppress default per-request stderr noise ---------------------------
    def log_message(self, fmt, *args):
        # We do our own logging; never echo request bodies or passwords.
        log.debug("http %s %s", self.address_string(), fmt % args)

    # -- helpers --------------------------------------------------------------
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

    # -- routes ---------------------------------------------------------------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            path = STATIC_DIR / "index.html"
            if path.is_file():
                self._send_html(path.read_bytes())
            else:
                self._send_not_found()
        elif self.path == "/banner.html":
            # Fixed name, fixed directory, no request input in the path.
            self._send_html(read_banner(authed=False))
        elif self.path == "/banner-authed.html":
            self._send_html(read_banner(authed=True))
        else:
            self._send_not_found()

    def do_POST(self):
        if self.path == "/api/change":
            self._handle_change()
        elif self.path == "/auth/user":
            self._handle_auth_user()
        elif self.path == "/auth/pass":
            self._handle_auth_pass()
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    # -- gate plumbing --------------------------------------------------------
    def _client_ip(self) -> str:
        # Rightmost X-Forwarded-For entry; see authgate.Gate.client_ip.
        return authgate.Gate.client_ip(
            self.headers.get("X-Forwarded-For"), self.client_address[0]
        )

    def _read_json(self) -> dict | None:
        """Parse a JSON object body, or send an error and return None."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad request"})
            return None
        if length <= 0 or length > MAX_BODY:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad request"})
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "request body must be valid JSON"})
            return None
        if not isinstance(data, dict):
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "request body must be a JSON object"})
            return None
        return data

    def _banned(self, ip: str) -> bool:
        """If refused, answer and return True. Always sleeps first.

        Two independent refusals: the circuit breaker (service denial, applies to
        everyone) and the per-IP ban. Both sleep the same uniform delay first so
        timing reveals nothing.
        """
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        denial = srv.gate.note_ip(ip)      # also records the IP for the breaker
        wait = srv.gate.retry_after(ip)
        time.sleep(authgate.auth_delay())          # uniform delay, even when banned

        if denial > 0:
            log.warning("service denied: distinct-IP burst ip=%s wait=%.0fs",
                        ip, denial)
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "error": "The service is temporarily unavailable. "
                             "Please try again later.",
                    "retry_after": int(denial) + 1,
                },
            )
            return True

        if wait > 0:
            log.info("auth throttled ip=%s wait=%.0fs", ip, wait)
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "ok": False,
                    "error": f"Too many failed attempts. Try again in {int(wait) + 1} seconds.",
                    "retry_after": int(wait) + 1,
                },
            )
            return True
        return False

    # -- stage 1: username ----------------------------------------------------
    def _handle_auth_user(self):
        """Collect a username. THIS STAGE CANNOT FAIL, by design.

        Accepting any username (valid or not) is what makes the form useless for
        enumerating accounts: there is no input that produces a different
        outcome. The delay is uniform for the same reason.
        """
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        ip = self._client_ip()
        if self._banned(ip):
            return
        data = self._read_json()
        if data is None:
            return
        username = data.get("username", "")
        if not isinstance(username, str):
            username = ""
        time.sleep(authgate.auth_delay())          # validation phase
        token = srv.gate.start_pending(username.strip(), ip)
        # No "user exists" signal of any kind.
        self._send_json(HTTPStatus.OK, {"ok": True, "ticket": token})

    # -- stage 2: password ----------------------------------------------------
    def _handle_auth_pass(self):
        """Authenticate. Failures here are indistinguishable and throttled."""
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        ip = self._client_ip()
        if self._banned(ip):
            return
        data = self._read_json()
        if data is None:
            return
        ticket = data.get("ticket", "")
        password = data.get("password", "")
        if not isinstance(ticket, str) or not isinstance(password, str):
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "bad request"})
            return

        pending = srv.gate.take_pending(ticket, ip)
        if pending is None:
            time.sleep(authgate.auth_delay())
            # Expired/absent ticket is not an authentication event; do not
            # count it against the ban budget, or a stale page becomes a
            # denial-of-service against legitimate users.
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "session expired; start again"})
            return

        try:
            srv.ldap_client.bind(pending.username, password)
        except core.ChangeError:
            wait = srv.gate.record_failure(ip)
            log.info("auth failed ip=%s cooldown=%.0fs", ip, wait)
            # Same message for both cases, so neither is a signal.
            #
            # The ticket was consumed above, so issue a fresh one: a retry with
            # a used ticket gets "bad request" and restarts the flow over a
            # typo. A ticket is issued for any username, valid or not, so its
            # presence here reveals nothing.
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
            # The traceback may render exception arguments, and the password is
            # in scope for this call -- strip it before logging.
            log_exception(log, f"unexpected error during authentication ip={ip}",
                          password)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "an unexpected error occurred"},
            )
            return

        # Authenticated. Hold the credentials briefly so /api/change does not
        # need them re-sent; the session is single-use and IP-bound.
        srv.gate.record_success(ip)
        token = srv.gate.start_session(pending.username, password, ip)
        self._send_json(HTTPStatus.OK, {"ok": True, "session": token})

    # -- the endpoint ---------------------------------------------------------
    def _handle_change(self):
        """Change the password for an ALREADY AUTHENTICATED session.

        Credentials come from the gate's session, not from the request body, so
        a caller cannot change a password without having authenticated first.
        """
        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        ip = self._client_ip()
        if self._banned(ip):
            return
        data = self._read_json()
        if data is None:
            return

        session_token = data.get("session", "")
        new_password = data.get("new_password", "")
        if not isinstance(session_token, str) or not isinstance(new_password, str):
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "bad request"})
            return

        session = srv.gate.take_session(session_token, ip)
        if session is None:
            time.sleep(authgate.auth_delay())
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": "not authenticated; start again"},
            )
            return

        username = session.username
        old_password = session.password

        try:
            min_len = int(os.environ.get("PW_MIN_LEN", str(DEFAULT_MIN_LEN)))
        except ValueError:
            min_len = DEFAULT_MIN_LEN

        srv: WorkerServer = self.server  # type: ignore[attr-defined]
        try:
            result = core.change_password(
                username,
                old_password,
                new_password,
                ldap_client=srv.ldap_client,
                tn_client=srv.tn_client,
                min_len=min_len,
            )
        except core.ChangeError as e:
            # Log the real reason; show the user a sanitised one.
            log.info("change failed user=%r reason=%s",
                     _safe_username(username), str(e)[:200])
            self._send_json(HTTPStatus.OK, {
                "ok": False,
                "error": user_facing_error(e),
                "partial": "was updated, but" in str(e),
            })
            return
        except Exception:
            # Never leak tracebacks. Generic message only.
            # Both passwords are in scope here, and a traceback renders repr() of
            # exception arguments -- scrub them.
            log_exception(
                log,
                f"unexpected error during password change for "
                f"user={_safe_username(username)!r}",
                old_password, new_password)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": "an unexpected error occurred; please try again "
                    "or contact your administrator",
                },
            )
            return

        log.info("change ok user=%r lldap=%s truenas=%s partial=%s",
                 _safe_username(username), result.lldap, result.truenas,
                 result.partial)
        self._send_json(HTTPStatus.OK, public_result(result))


def user_facing_error(exc: Exception) -> str:
    """Translate an internal failure into something safe to show a user.

    The page must not expose infrastructure: no service names, no hostnames, no
    exception class names, no hint that there are two separate password stores
    or where either lives. Messages are generic; the detail goes to the server
    log, where an operator can see it.

    Known, actionable cases are mapped to plain-English equivalents; everything
    else becomes one catch-all sentence.
    """
    text = str(exc or "")
    low = text.lower()

    # Credential failure (already generic upstream, kept for clarity).
    if "invalid credentials" in low:
        return "Invalid username or password."

    # A transient problem talking to a backend; do not name the backend.
    # Transport failures are transient and worth retrying. A dropped
    # websocket surfaces as ClientException / "connection to remote host was
    # lost", so match those and not only name-shaped errors.
    if any(k in low for k in
           ("cannot reach", "connectionrefused", "timed out", "timeout",
            "connection reset", "refused", "unreachable", "connection was lost",
            "clientexception", "websocket", "call failed", "lookup failed")):
        return ("The service is temporarily unavailable. "
                "Please try again in a few moments.")

    # The new password was rejected by policy.
    if "policy" in low or "rejected the new password" in low:
        return ("That password was not accepted. Please choose a different "
                "one, and make it reasonably long.")

    # Partially applied: one store changed and the other did not. The user MUST
    # be told this (they need to retry with their OLD password), but they must
    # not be told what the stores are.
    if "was updated, but" in low:
        return ("Your password was only partially changed. Your previous "
                "password still works -- sign in with it and try again, "
                "choosing a different new password.")

    # Already-authenticated failure.
    if "not authenticated" in low:
        return "Your session has ended. Please sign in again."

    return "Something went wrong. Please try again, or contact your administrator."


def public_result(result) -> dict:
    """The response body: only what the page needs to render.

    Returns only what the page needs to render. Internal booleans
    (`lldap`, `truenas`, `truenas_applicable`) are dropped -- they describe the
    backend layout and mean nothing to a user.
    """
    if not result.lldap or (result.truenas_applicable and not result.truenas):
        return {"ok": False, "partial": bool(result.partial),
                "error": "Your password was only partially changed. Your previous "
                         "password still works -- sign in with it and try again, "
                         "choosing a different new password."
                         if result.partial else
                         "Something went wrong. Please try again, or contact "
                         "your administrator."}
    return {
        "ok": True,
        "partial": False,
        "message": "Your password has been changed.",
    }


def _safe_username(username: str) -> str:
    """Return a username safe for logging (it is not a secret)."""
    return username[:64] if isinstance(username, str) else "?"


# --------------------------------------------------------------------------- #
#  Server
# --------------------------------------------------------------------------- #
class WorkerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ldap_client, tn_client, is_real):
        super().__init__(addr, Handler)
        self.ldap_client = ldap_client
        self.tn_client = tn_client
        self.is_real = is_real
        self.gate = authgate.Gate()


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    listen_host, listen_port = parse_listen(
        os.environ.get("PW_LISTEN", DEFAULT_LISTEN)
    )
    bind_host, bind_port = parse_listen(
        os.environ.get("PW_BIND", DEFAULT_BIND)
    )
    ldap_client, tn_client, is_real = build_clients()

    if not is_real:
        bar = "=" * 72
        log.warning(bar)
        log.warning(
            "WARNING: TrueNAS client is a STUB (no API key configured)."
        )
        log.warning(
            "Password changes will NOT update the NAS."
        )
        log.warning(
            "Set PW_TN_KEY or PW_TN_KEY_FILE to enable real TrueNAS updates."
        )
        log.warning(bar)
    else:
        log.info("TrueNAS client: real (API key configured)")

    server = WorkerServer((listen_host, listen_port), ldap_client, tn_client, is_real)
    # Log both clearly so nobody "fixes" one by breaking the other.
    log.info(
        "in-container bind: %s:%d (PW_LISTEN) — this is the socket the process "
        "opens inside the container netns",
        listen_host, listen_port,
    )
    log.info(
        "host-side publish: %s:%d (PW_BIND) — the operator must map the "
        "container port to THIS host address to avoid exposing the service on "
        "a public interface",
        bind_host, bind_port,
    )
    if bind_host in ("0.0.0.0", "::"):
        log.warning(
            "PW_BIND is a wildcard (%s); this publishes on ALL host "
            "interfaces, including any public address. Set PW_BIND to the LAN "
            "address unless you intentionally want this.",
            bind_host,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        log.info("stopped")


if __name__ == "__main__":
    main()
