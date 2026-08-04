"""Detect credential-shaped field names and scalar values at persistence boundaries."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

SENSITIVE_NAME_SUFFIXES = (
    "accesstoken",
    "activationkey",
    "apikey",
    "apitoken",
    "arguments",
    "authorization",
    "authorizationheader",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "commandline",
    "connectionstring",
    "connectionstringencrypted",
    "cookie",
    "cookieheader",
    "credential",
    "credentialreference",
    "credentials",
    "credentialsencrypted",
    "credentialsnonce",
    "databaseurl",
    "databaseurlencrypted",
    "encryptionkey",
    "executable",
    "executablename",
    "executablepath",
    "licensekey",
    "logodataurl",
    "passphrase",
    "password",
    "passworddigest",
    "passwordencrypted",
    "passwordhash",
    "passwordsalt",
    "privatekey",
    "privatekeypem",
    "productkey",
    "refreshtoken",
    "registrationkey",
    "remotecontroluri",
    "remotecontrolurl",
    "secret",
    "secretciphertext",
    "secretencrypted",
    "secretnonce",
    "secretvalue",
    "serviceaccount",
    "setcookie",
    "sharedkey",
    "signingkey",
    "sshkey",
    "token",
    "tokenciphertext",
    "tokendigest",
    "tokenencrypted",
    "tokenhash",
    "tokensalt",
    "tokensecret",
    "tokenvalue",
    "useraccount",
    "webhooksecret",
    "xapikey",
)

_AUTHORIZATION_VALUE = re.compile(r"^\s*(?:basic|bearer)\s+\S+", re.IGNORECASE)
_JWT_VALUE = re.compile(r"^\s*[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\s*$")
_PRIVATE_KEY_VALUE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.IGNORECASE,
)
_CREDENTIAL_URI_VALUE = re.compile(
    r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@",
    re.IGNORECASE,
)
_CONNECTION_SECRET_VALUE = re.compile(
    r"(?:^|;)\s*(?:password|pwd)\s*=",
    re.IGNORECASE,
)


def canonical_sensitive_key(value: object) -> str:
    """Return an NFKC, case- and separator-insensitive field-name token."""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[^a-z0-9]", "", normalized)


def is_sensitive_field_name(value: object) -> bool:
    """Return whether a field name denotes a credential or unsafe execution detail."""

    normalized = canonical_sensitive_key(value)
    return bool(normalized) and normalized.endswith(SENSITIVE_NAME_SUFFIXES)


def is_sensitive_scalar(value: object) -> bool:
    """Return whether a scalar strongly resembles reusable credential material."""

    if not isinstance(value, str):
        return False
    return any(
        pattern.search(value)
        for pattern in (
            _AUTHORIZATION_VALUE,
            _JWT_VALUE,
            _PRIVATE_KEY_VALUE,
            _CREDENTIAL_URI_VALUE,
            _CONNECTION_SECRET_VALUE,
        )
    )


def contains_sensitive_data(value: Any, *, depth: int = 0) -> bool:
    """Recursively detect credential-shaped data and fail closed on extreme depth."""

    if depth > 16:
        return True
    if isinstance(value, Mapping):
        return any(
            is_sensitive_field_name(raw_key) or contains_sensitive_data(child, depth=depth + 1)
            for raw_key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive_data(child, depth=depth + 1) for child in value)
    return is_sensitive_scalar(value)
