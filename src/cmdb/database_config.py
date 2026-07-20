"""Encrypt database connection settings persisted by the first-start workflow."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CONFIG_VERSION = 1
ASSOCIATED_DATA = b"ipt-cmdb:database-config:v1"


class DatabaseConfigError(RuntimeError):
    """Report a safe, operator-actionable database configuration error."""


def encryption_key(value: str | None = None, key_file: str | None = None) -> bytes:
    """Load the dedicated 256-bit database configuration encryption key."""

    configured = value if value is not None else os.getenv("DATABASE_CONFIG_ENCRYPTION_KEY", "")
    configured_file = (
        key_file if key_file is not None else os.getenv("DATABASE_CONFIG_ENCRYPTION_KEY_FILE", "")
    )
    if not configured and configured_file:
        try:
            configured = Path(configured_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise DatabaseConfigError(
                "The database configuration encryption key file cannot be read"
            ) from error
    if not configured:
        raise DatabaseConfigError(
            "Configure DATABASE_CONFIG_ENCRYPTION_KEY or "
            "DATABASE_CONFIG_ENCRYPTION_KEY_FILE before saving database settings"
        )
    try:
        decoded = base64.b64decode(configured.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as error:
        raise DatabaseConfigError(
            "DATABASE_CONFIG_ENCRYPTION_KEY must be URL-safe base64"
        ) from error
    if len(decoded) != 32:
        raise DatabaseConfigError("DATABASE_CONFIG_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return decoded


def encrypt_database_url(database_url: str, key: bytes | None = None) -> dict[str, int | str]:
    """Encrypt and authenticate a PostgreSQL URL for local persistence."""

    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key or encryption_key()).encrypt(
        nonce,
        database_url.encode("utf-8"),
        ASSOCIATED_DATA,
    )
    return {
        "version": CONFIG_VERSION,
        "algorithm": "AES-256-GCM",
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }


def decrypt_database_url(document: dict, key: bytes | None = None) -> str:
    """Decrypt an authenticated database configuration document."""

    if document.get("version") != CONFIG_VERSION or document.get("algorithm") != "AES-256-GCM":
        raise DatabaseConfigError("The saved database configuration format is unsupported")
    try:
        nonce = base64.b64decode(
            str(document["nonce"]).encode("ascii"), altchars=b"-_", validate=True
        )
        ciphertext = base64.b64decode(
            str(document["ciphertext"]).encode("ascii"), altchars=b"-_", validate=True
        )
        plaintext = AESGCM(key or encryption_key()).decrypt(
            nonce,
            ciphertext,
            ASSOCIATED_DATA,
        )
    except (InvalidTag, KeyError, binascii.Error, UnicodeEncodeError, ValueError) as error:
        raise DatabaseConfigError(
            "The saved database configuration cannot be decrypted with the configured key"
        ) from error
    return plaintext.decode("utf-8")


def write_encrypted_database_url(path: Path, database_url: str, key: bytes | None = None) -> None:
    """Atomically persist an encrypted database URL with restrictive permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    document = encrypt_database_url(database_url, key)
    try:
        temporary_path.write_text(json.dumps(document), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(OSError):
            temporary_path.unlink()
