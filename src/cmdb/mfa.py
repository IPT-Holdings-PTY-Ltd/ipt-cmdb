"""Standards-based TOTP enrollment and encrypted-secret helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

import pyotp
import qrcode  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class MfaConfigurationError(RuntimeError):
    """Report a missing or malformed server-side MFA encryption key."""


def encryption_key(value: str | None = None) -> bytes:
    """Decode the 256-bit URL-safe base64 key used for TOTP seed encryption."""

    configured = value if value is not None else os.getenv("MFA_ENCRYPTION_KEY", "")
    if value is None and not configured:
        key_file = os.getenv("MFA_ENCRYPTION_KEY_FILE", "").strip()
        if key_file:
            try:
                configured = Path(key_file).read_text(encoding="utf-8").strip()
            except OSError as error:
                raise MfaConfigurationError(
                    "TOTP enrollment cannot read the configured MFA key file"
                ) from error
    if not configured:
        raise MfaConfigurationError(
            "TOTP enrollment is unavailable until an MFA encryption key is configured"
        )
    try:
        decoded = base64.urlsafe_b64decode(configured.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise MfaConfigurationError("MFA_ENCRYPTION_KEY must be URL-safe base64") from error
    if len(decoded) != 32:
        raise MfaConfigurationError("MFA_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return decoded


def encrypt_secret(secret: str, user_id: str, key: bytes | None = None) -> tuple[str, str]:
    """Encrypt a TOTP seed with the user identifier bound as associated data."""

    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key or encryption_key()).encrypt(
        nonce, secret.encode("ascii"), user_id.encode("utf-8")
    )
    return (
        base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        base64.urlsafe_b64encode(nonce).decode("ascii"),
    )


def decrypt_secret(
    encrypted_secret: str, nonce: str, user_id: str, key: bytes | None = None
) -> str:
    """Decrypt and authenticate a stored TOTP seed."""

    try:
        plaintext = AESGCM(key or encryption_key()).decrypt(
            base64.urlsafe_b64decode(nonce.encode("ascii")),
            base64.urlsafe_b64decode(encrypted_secret.encode("ascii")),
            user_id.encode("utf-8"),
        )
    except (InvalidTag, ValueError) as error:
        raise MfaConfigurationError(
            "Stored MFA material cannot be decrypted with the configured key"
        ) from error
    return plaintext.decode("ascii")


def new_totp_secret() -> str:
    """Generate a 160-bit base32 TOTP seed."""

    return pyotp.random_base32(length=32)


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """Create a standard otpauth URI accepted by common authenticator apps."""

    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def qr_data_uri(value: str) -> str:
    """Render a provisioning URI as an inline PNG without contacting a third party."""

    image = qrcode.make(value)
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def verify_totp(
    secret: str,
    code: str,
    *,
    last_counter: int | None = None,
    at_time: datetime | None = None,
    valid_window: int = 1,
) -> int | None:
    """Return the accepted RFC 6238 counter while rejecting replayed time steps."""

    normalized = "".join(character for character in code if character.isdigit())
    if len(normalized) != 6:
        return None
    current_time = at_time or datetime.now(UTC)
    totp = pyotp.TOTP(secret)
    current_counter = int(current_time.timestamp()) // totp.interval
    for offset in range(-valid_window, valid_window + 1):
        counter = current_counter + offset
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(totp.generate_otp(counter), normalized):
            return counter
    return None


def recovery_codes(count: int = 10) -> list[str]:
    """Generate high-entropy, human-readable one-use recovery codes."""

    values = []
    for _ in range(count):
        encoded = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
        values.append("-".join(encoded[index : index + 4] for index in range(0, 16, 4)))
    return values


def opaque_token_hash(token: str) -> str:
    """Hash bearer material before it is persisted."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
