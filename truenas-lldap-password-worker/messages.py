#!/usr/bin/env python3
"""User-facing strings, in one place.

Everything a user can see lives here so it can be audited, translated, or
replaced wholesale. Operator-facing text (the messages `ConfigError` carries to
the startup log) is also here, under `OPERATOR`, because it is read by a person
too. Log messages are deliberately NOT here: they stay English and terse.

`ErrorKind` is the vocabulary shared with core: core raises `ChangeError` with a
kind and this module decides what that kind says. No message text is matched by
substring anywhere.
"""

from __future__ import annotations

import enum

__all__ = [
    "ErrorKind",
    "ERRORS",
    "MESSAGES",
    "OPERATOR",
    "error_text",
    "operator_text",
    "text",
]


class ErrorKind(enum.Enum):
    """Why a request failed, independent of how it is worded."""

    INVALID_CREDENTIALS = "invalid_credentials"
    TRANSIENT = "transient"
    POLICY = "policy"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
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
        "Your password was changed on the NAS but not in the directory. Your "
        "directory password is still your previous one; sign in with it and try "
        "again."
    ),
    ErrorKind.UNVERIFIED: (
        "Your password was changed, but it could not be confirmed. Try signing in "
        "with your new password; if that fails, use your previous one."
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
    "unavailable": "The service is temporarily unavailable. Please try again later.",
    "throttled": "Too many failed attempts. Try again in {seconds} seconds.",
    "session_expired": "Session expired; start again.",
    "not_authenticated": "Not authenticated; start again.",
    "bad_request": "Bad request.",
    "not_found": "Not found.",
    "invalid_json": "Request body must be valid JSON.",
    "json_object_required": "Request body must be a JSON object.",
    "unexpected": "An unexpected error occurred.",
}

# Operator-facing explanations, logged at startup. Read by a person fixing a
# deployment, so they are actionable rather than generic.
OPERATOR: dict[str, str] = {
    "no_api_key": "no TrueNAS API key configured; set PW_TN_KEY or PW_TN_KEY_FILE",
    "no_wss_url": "PW_TN_WSS is not set; the TrueNAS API URL is required",
    "no_ldap_uri": "PW_LDAP_URI is not set; the directory endpoint is required",
    "no_ldap_base": "PW_LDAP_BASE is not set; the directory base DN is required",
    "nas_password_set_failed": (
        "nothing was changed: the NAS password could not be set ({e}). "
        "Your current password still works; please try again."
    ),
    "nas_set_but_directory_failed": (
        "the NAS password was set but the directory refused the new password ({e})"
    ),
    "changed_but_unverified": (
        "both stores accepted the new password but the check bind failed"
    ),
}


def error_text(kind: ErrorKind) -> str:
    """The user-facing text for a failure kind."""
    return ERRORS.get(kind, ERRORS[ErrorKind.UNKNOWN])


def text(key: str, **fields: object) -> str:
    """A non-error user message, with named fields substituted."""
    return MESSAGES[key].format(**fields)


def operator_text(key: str, **fields: object) -> str:
    """An operator-facing message: a startup/config fault, or a logged reason."""
    return OPERATOR[key].format(**fields)
