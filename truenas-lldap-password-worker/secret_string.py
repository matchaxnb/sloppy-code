#!/usr/bin/env python3
"""Credential wrapper.

Wrap a password as `SecretString` at the point it is parsed from a request and it
cannot reach a log or a traceback by accident: `repr`, `str`, f-strings and `%s`
all render a placeholder. The value is still available where an API needs the real
string, via `str(...)` or `.value`.

Not a guarantee. `json.dumps`, `+` on a plain str, and `%r` of a *container* that
holds it by a name other than this class's can still expose it, so logging is
still done by passing known secrets explicitly.

Usage:
    password = SecretString(raw)      # as soon as it leaves the request
    ldap_client.bind(user, password)  # callee unwraps what it needs
"""

from __future__ import annotations

__all__ = ["SecretString"]


class SecretString(str):
    __slots__ = ()

    def __repr__(self) -> str:
        return "<secret>"

    def __str__(self) -> str:
        return "<secret>"

    @property
    def value(self) -> str:
        """The real value, as a plain str, for APIs that need it."""
        return self[:]

    def reveal(self) -> str:
        """Synonym for `value`, for readability at call sites."""
        return self[:]
