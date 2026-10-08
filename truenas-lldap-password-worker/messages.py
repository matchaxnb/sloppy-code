#!/usr/bin/env python3
"""User-facing strings, in one place.

Everything a user can see lives here so it can be audited, translated, or
replaced wholesale. Log messages are deliberately NOT here: they stay English
and terse, and are read by operators.

`ErrorKind` is the vocabulary shared with core: core raises `ChangeError` with a
kind, and this module decides what that kind says to a user. No message text is
matched by substring anywhere.
"""

from __future__ import annotations

import enum

__all__ = ["ErrorKind", "ERRORS", "MESSAGES", "error_text", "text"]


class ErrorKind(enum.Enum):
    """Why a request failed, independent of how it is worded."""

    INVALID_CREDENTIALS = "invalid_credentials"
    TRANSIENT = "transient"
    POLICY = "policy"
    PARTIAL = "partial"
    NOT_AUTHENTICATED = "not_authenticated"
    VALIDATION = "validation"
    UNKNOWN = "unknown"


# What the user is told, per kind. Deliberately free of backend names,
# hostnames, ports and exception classes.
ERRORS: dict[ErrorKind, str] = {
    ErrorKind.INVALID_CREDENTIALS: "Invalid username or password.",
    ErrorKind.TRANSIENT: (
        "The service is temporarily unavailable. Please try again in a few moments."
    ),
    ErrorKind.POLICY: (
        "That password was not accepted. Please choose a different one, and make "
        "it reasonably long."
    ),
    ErrorKind.PARTIAL: (
        "Your password was only partially changed. Your previous password still "
        "works -- sign in with it and try again, choosing a different new password."
    ),
    ErrorKind.NOT_AUTHENTICATED: "Your session has ended. Please sign in again.",
    ErrorKind.VALIDATION: (
        "That password was not accepted. Please choose a different one."
    ),
    ErrorKind.UNKNOWN: (
        "Something went wrong. Please try again, or contact your administrator."
    ),
}

# Everything else a user can see that is not an error.
MESSAGES: dict[str, str] = {
    "changed": "Your password has been changed.",
    "unavailable": ("The service is temporarily unavailable. Please try again later."),
    "throttled": "Too many failed attempts. Try again in {seconds} seconds.",
    "session_expired": "Session expired; start again.",
    "not_authenticated": "Not authenticated; start again.",
    "bad_request": "Bad request.",
    "not_found": "Not found.",
    "invalid_json": "Request body must be valid JSON.",
    "json_object_required": "Request body must be a JSON object.",
    "unexpected": "An unexpected error occurred.",
}


def error_text(kind: ErrorKind) -> str:
    """The user-facing text for a failure kind."""
    return ERRORS.get(kind, ERRORS[ErrorKind.UNKNOWN])


def text(key: str, **fields: object) -> str:
    """A non-error message, with named fields substituted."""
    return MESSAGES[key].format(**fields)
