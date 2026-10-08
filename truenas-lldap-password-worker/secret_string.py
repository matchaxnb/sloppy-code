#!/usr/bin/env python3
"""Credential wrapper, and the JSON encoder that understands it.

Wrap a password as `SecretString` the moment it is parsed from a request and it
cannot reach a log or a traceback by accident: `repr`, `str`, f-strings and `%s`
all render a placeholder. The value is still available where an API needs the
real string, via `.value` or `reveal()`.

Serialisation is handled by `json_response`, which refuses to emit secrets: any
`SecretString` reaching JSON without an explicit `reveal()` is the caller's
mistake, and the placeholder makes it visible rather than silent. Use that
serializer instead of `json.dumps` for anything derived from a request.

    password = SecretString(raw)      # as soon as it leaves the request
    ldap_client.bind(user, password)  # callee unwraps what it needs
    json_response({"ok": True})       # never contains a secret
"""

from __future__ import annotations

import json

__all__ = ["SecretString", "json_response"]


class SecretString(str):
    __slots__ = ()

    def __repr__(self) -> str:
        return "<secret>"

    def __str__(self) -> str:
        return "<secret>"

    def __format__(self, spec: str) -> str:
        # str.__format__ formats at the C level and would bypass __str__.
        return "<secret>"

    @property
    def value(self) -> str:
        """The real value, as a plain str, for APIs that need it."""
        return self[:]

    def reveal(self) -> str:
        """Synonym for `value`, for readability at call sites."""
        return self[:]


def json_response(payload: dict) -> bytes:
    """Serialize a response body, with secrets rendered as a placeholder.

    A `SecretString` in the payload means something forgot to unwrap it. Emitting
    the placeholder makes that a visible bug rather than a leak.

    The walk is explicit because `json.dumps(default=...)` is not enough:
    `SecretString` subclasses `str`, so the encoder serializes it natively and
    never calls `default`.
    """
    return json.dumps(_scrub(payload)).encode("utf-8")


def _scrub(obj: object) -> object:
    """Replace any SecretString with a placeholder, recursively."""
    if isinstance(obj, SecretString):
        return "<secret>"
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj
