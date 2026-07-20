"""Microsoft Graph email delivery with deployment-friendly credentials."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class EmailConfigurationError(RuntimeError):
    """Report an incomplete or unsafe outbound-email configuration."""


class EmailDeliveryError(RuntimeError):
    """Report a sanitized Microsoft Graph delivery failure."""


@dataclass(frozen=True)
class DeliveryResult:
    """Describe acceptance of an email by the provider."""

    status_code: int
    provider_request_id: str = ""


def valid_email_address(value: str) -> bool:
    """Return whether a value resembles a single deliverable email address."""

    display_name, address = parseaddr(value.strip())
    del display_name
    local, separator, domain = address.rpartition("@")
    return bool(separator and local and "." in domain and " " not in address)


def public_email_connection(connection: dict[str, Any]) -> dict[str, Any]:
    """Remove stored credential material from an API-facing configuration."""

    hidden = {
        "clientSecretEncrypted",
        "clientSecretNonce",
        "certificatePasswordEncrypted",
        "certificatePasswordNonce",
    }
    public = {key: value for key, value in connection.items() if key not in hidden}
    public["hasClientSecret"] = bool(connection.get("clientSecretEncrypted"))
    public["certificateConfigured"] = bool(os.getenv("EMAIL_CERTIFICATE_PATH", "").strip())
    return public


def _secret_file_value(variable: str, file_variable: str) -> str:
    value = os.getenv(variable, "").strip()
    if value:
        return value
    path = os.getenv(file_variable, "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise EmailConfigurationError(f"Cannot read {file_variable}") from error


def build_credential(connection: dict[str, Any], client_secret: str = "") -> Any:
    """Build the selected Azure Identity credential without exposing its secret."""

    try:
        from azure.identity import (
            CertificateCredential,
            ClientSecretCredential,
            ManagedIdentityCredential,
        )
    except ImportError as error:
        raise EmailConfigurationError("Azure Identity support is not installed") from error

    auth_mode = connection.get("authMode", "managed_identity")
    if auth_mode == "managed_identity":
        client_id = str(connection.get("managedIdentityClientId") or "").strip()
        return ManagedIdentityCredential(client_id=client_id or None)

    tenant_id = str(connection.get("tenantId") or "").strip()
    client_id = str(connection.get("clientId") or "").strip()
    if not tenant_id or not client_id:
        raise EmailConfigurationError("Tenant ID and application client ID are required")
    if auth_mode == "client_secret":
        if not client_secret:
            raise EmailConfigurationError("A client secret has not been configured")
        return ClientSecretCredential(tenant_id, client_id, client_secret)
    if auth_mode == "certificate":
        certificate_path = os.getenv("EMAIL_CERTIFICATE_PATH", "").strip()
        if not certificate_path:
            raise EmailConfigurationError("EMAIL_CERTIFICATE_PATH is not configured")
        password = _secret_file_value(
            "EMAIL_CERTIFICATE_PASSWORD", "EMAIL_CERTIFICATE_PASSWORD_FILE"
        )
        return CertificateCredential(
            tenant_id,
            client_id,
            certificate_path=certificate_path,
            password=password or None,
        )
    raise EmailConfigurationError("Unsupported email authentication mode")


def graph_message_payload(message: dict[str, Any], connection: dict[str, Any]) -> dict[str, Any]:
    """Translate a provider-neutral outbox item into a Graph sendMail document."""

    def recipients(key: str) -> list[dict[str, dict[str, str]]]:
        return [
            {"emailAddress": {"address": address}}
            for address in message.get(key, [])
            if valid_email_address(str(address))
        ]

    graph_message: dict[str, Any] = {
        "subject": str(message.get("subject") or "")[:998],
        "body": {
            "contentType": "HTML" if message.get("bodyHtml") else "Text",
            "content": str(message.get("bodyHtml") or message.get("bodyText") or ""),
        },
        "toRecipients": recipients("to"),
    }
    if recipients("cc"):
        graph_message["ccRecipients"] = recipients("cc")
    if recipients("bcc"):
        graph_message["bccRecipients"] = recipients("bcc")
    reply_to = str(connection.get("replyTo") or "").strip()
    if reply_to and valid_email_address(reply_to):
        graph_message["replyTo"] = [{"emailAddress": {"address": reply_to}}]
    return {"message": graph_message, "saveToSentItems": True}


class GraphEmailSender:
    """Send outbox items with Graph application permissions and bounded retries."""

    def __init__(
        self,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.opener = opener
        self.sleeper = sleeper

    def send(
        self,
        connection: dict[str, Any],
        message: dict[str, Any],
        *,
        client_secret: str = "",
    ) -> DeliveryResult:
        """Submit one message and return only after Graph accepts it."""

        if not connection.get("enabled"):
            raise EmailConfigurationError("Outbound email is disabled")
        sender = str(connection.get("senderAddress") or "").strip()
        if not valid_email_address(sender):
            raise EmailConfigurationError("A valid sender mailbox is required")
        if not message.get("to"):
            raise EmailConfigurationError("At least one recipient is required")

        credential = build_credential(connection, client_secret)
        try:
            token = credential.get_token(GRAPH_SCOPE).token
        except Exception as error:
            raise EmailDeliveryError("Microsoft 365 authentication failed") from error

        base_url = str(connection.get("graphBaseUrl") or GRAPH_BASE_URL).rstrip("/")
        url = f"{base_url}/users/{quote(sender, safe='@')}/sendMail"
        body = json.dumps(graph_message_payload(message, connection)).encode("utf-8")
        for attempt in range(3):
            request = Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "IPT-CMDB/0.5",
                },
            )
            try:
                with self.opener(request, timeout=30) as response:
                    status_code = int(response.getcode())
                    if status_code != 202:
                        raise EmailDeliveryError("Microsoft Graph did not accept the message")
                    return DeliveryResult(
                        status_code=status_code,
                        provider_request_id=response.headers.get("request-id", ""),
                    )
            except HTTPError as error:
                if error.code == 429 and attempt < 2:
                    retry_after = error.headers.get("Retry-After", "1")
                    self.sleeper(min(max(float(retry_after), 0.0), 30.0))
                    continue
                if error.code in {401, 403}:
                    raise EmailDeliveryError(
                        "Microsoft Graph rejected the application permission or mailbox scope"
                    ) from error
                raise EmailDeliveryError(
                    f"Microsoft Graph rejected the message (HTTP {error.code})"
                ) from error
            except (URLError, TimeoutError) as error:
                raise EmailDeliveryError("Microsoft Graph could not be reached") from error
        raise EmailDeliveryError("Microsoft Graph throttled the message repeatedly")


def exchange_rbac_script(connection: dict[str, Any]) -> str:
    """Generate a reviewable Exchange Online Application RBAC setup script."""

    app_id = str(connection.get("clientId") or "<APPLICATION_CLIENT_ID>").strip()
    sender = str(connection.get("senderAddress") or "cmdb@example.com").strip()
    scope_name = "IPT-CMDB-Mailbox-Scope"
    role_name = "IPT-CMDB-Mail-Sender"
    return f"""# Review and run in Exchange Online PowerShell as an Exchange administrator.
# This scopes the app to the configured CMDB sender mailbox.
Connect-ExchangeOnline
$AppId = '{app_id}'
$SenderMailbox = '{sender}'
$ScopeName = '{scope_name}'
$AssignmentName = '{role_name}'

New-ManagementScope -Name $ScopeName -RecipientRestrictionFilter \"PrimarySmtpAddress -eq '$SenderMailbox'\"
New-ServicePrincipal -AppId $AppId -ObjectId '<ENTERPRISE_APP_OBJECT_ID>'
$ServicePrincipal = Get-ServicePrincipal | Where-Object {{ $_.AppId -eq $AppId }}
New-ManagementRoleAssignment -Name $AssignmentName -Role 'Application Mail.Send' -App $ServicePrincipal.ObjectId -CustomResourceScope $ScopeName

# Verification can take time to reflect after directory or Exchange changes.
Test-ServicePrincipalAuthorization -Identity $AppId -Resource $SenderMailbox | Format-Table
"""
