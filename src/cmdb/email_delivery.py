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


def _authentication_error_message(error: Exception) -> str:
    """Return a safe, actionable authentication failure without leaking provider data."""

    current: BaseException | None = error
    details: list[str] = []
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        details.append(str(current).lower())
        current = current.__cause__ or current.__context__
    combined = " ".join(details)
    network_markers = (
        "network unreachable",
        "failed to establish a new connection",
        "name or service not known",
        "temporary failure in name resolution",
        "connection refused",
        "connect timeout",
    )
    if any(marker in combined for marker in network_markers):
        return (
            "The CMDB container cannot reach Microsoft identity over HTTPS. "
            "Check container DNS, proxy, and IPv4/IPv6 connectivity, then retry."
        )
    return "Microsoft 365 authentication failed"


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
    public["deploymentHints"] = {
        "managedIdentityAvailable": any(
            os.getenv(name, "").strip()
            for name in ("IDENTITY_ENDPOINT", "MSI_ENDPOINT", "IMDS_ENDPOINT")
        ),
        "certificateConfigured": public["certificateConfigured"],
        "secretStorageConfigured": bool(
            os.getenv("MFA_ENCRYPTION_KEY", "").strip()
            or os.getenv("MFA_ENCRYPTION_KEY_FILE", "").strip()
        ),
    }
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
            raise EmailDeliveryError(_authentication_error_message(error)) from error

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


def _powershell_literal(value: str) -> str:
    """Escape a value for use inside a single-quoted PowerShell literal."""

    return value.replace("'", "''")


def exchange_rbac_script(connection: dict[str, Any], *, create_shared_mailbox: bool = False) -> str:
    """Generate a reviewable, idempotent Exchange Online setup script."""

    app_id = _powershell_literal(
        str(connection.get("clientId") or "<APPLICATION_CLIENT_ID>").strip()
    )
    object_id = _powershell_literal(
        str(connection.get("servicePrincipalObjectId") or "<ENTERPRISE_APP_OBJECT_ID>").strip()
    )
    tenant_id = _powershell_literal(str(connection.get("tenantId") or "").strip())
    sender = _powershell_literal(str(connection.get("senderAddress") or "cmdb@example.com").strip())
    sender_name = _powershell_literal(str(connection.get("senderName") or "IPT CMDB").strip())
    auth_mode = _powershell_literal(str(connection.get("authMode") or "managed_identity").strip())
    scope_name = "IPT-CMDB-Mailbox-Scope"
    role_name = "IPT-CMDB-Mail-Sender"
    create_mailbox = "$true" if create_shared_mailbox else "$false"
    return rf"""#requires -Version 7.2
# Generated by IPT CMDB. Review and run as an Exchange administrator.
# Safe to re-run: existing mailbox, scope, service principal and role are reused.
# On Windows, review this file and run:
#   Unblock-File -LiteralPath .\setup-ipt-cmdb-email.ps1
#   .\setup-ipt-cmdb-email.ps1
# Do not weaken the machine-wide PowerShell execution policy.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$AppId = '{app_id}'
$EnterpriseAppObjectId = '{object_id}'
$TenantId = '{tenant_id}'
$AuthenticationMode = '{auth_mode}'
$SenderMailbox = '{sender}'
$SenderName = '{sender_name}'
$CreateSharedMailbox = {create_mailbox}
$ScopeName = '{scope_name}'
$AssignmentName = '{role_name}'
$ResultPath = Join-Path ($PSScriptRoot ?? (Get-Location).Path) 'ipt-cmdb-email-setup-result.json'

if ($AppId -like '<*' -or $EnterpriseAppObjectId -like '<*') {{
    throw 'Application client ID and enterprise application object ID are required.'
}}

Import-Module ExchangeOnlineManagement -ErrorAction Stop
Connect-ExchangeOnline -ShowBanner:$false
try {{
    $Mailbox = Get-EXOMailbox -Identity $SenderMailbox -ErrorAction SilentlyContinue
    if (-not $Mailbox -and $CreateSharedMailbox) {{
        Write-Host "Creating shared mailbox $SenderMailbox..."
        New-Mailbox -Shared -Name $SenderName -DisplayName $SenderName -PrimarySmtpAddress $SenderMailbox -ErrorAction Stop | Out-Null
    }}

    # Newly created mailboxes can take a short time to replicate across Exchange Online.
    for ($MailboxAttempt = 1; -not $Mailbox -and $MailboxAttempt -le 12; $MailboxAttempt++) {{
        $Mailbox = Get-EXOMailbox -Identity $SenderMailbox -ErrorAction SilentlyContinue
        if (-not $Mailbox -and $MailboxAttempt -lt 12) {{
            Write-Host "Waiting for mailbox replication ($MailboxAttempt/12)..."
            Start-Sleep -Seconds 10
        }}
    }}
    if (-not $Mailbox) {{
        throw "Mailbox $SenderMailbox does not exist. Create it or regenerate the script with mailbox creation enabled."
    }}

    $RecipientFilter = "PrimarySmtpAddress -eq '$SenderMailbox'"
    $Scope = Get-ManagementScope -Identity $ScopeName -ErrorAction SilentlyContinue
    if ($Scope) {{
        Set-ManagementScope -Identity $ScopeName -RecipientRestrictionFilter $RecipientFilter
    }}
    else {{
        New-ManagementScope -Name $ScopeName -RecipientRestrictionFilter $RecipientFilter | Out-Null
    }}

    $ServicePrincipal = Get-ServicePrincipal | Where-Object {{ $_.AppId -eq $AppId }} | Select-Object -First 1
    if ($ServicePrincipal -and $ServicePrincipal.ObjectId -ne $EnterpriseAppObjectId) {{
        throw "Exchange already contains AppId $AppId with a different object ID. Verify the enterprise application before changing the existing registration."
    }}
    for ($PrincipalAttempt = 1; -not $ServicePrincipal -and $PrincipalAttempt -le 4; $PrincipalAttempt++) {{
        try {{
            New-ServicePrincipal -AppId $AppId -ObjectId $EnterpriseAppObjectId -DisplayName $SenderName -ErrorAction Stop | Out-Null
        }}
        catch {{
            if ($PrincipalAttempt -eq 4) {{
                throw "Exchange could not find this Entra service principal after retrying. In Entra ID > Enterprise applications, search for Application ID $AppId and copy that row's Object ID. Do not use the Object ID from App registrations. Supplied Object ID: $EnterpriseAppObjectId. Exchange error: $($_.Exception.Message)"
            }}
            Write-Warning "The Entra service principal is not visible to Exchange yet. Retrying after directory replication ($PrincipalAttempt/4)..."
            Start-Sleep -Seconds 15
        }}
        $ServicePrincipal = Get-ServicePrincipal | Where-Object {{ $_.AppId -eq $AppId }} | Select-Object -First 1
    }}
    if (-not $ServicePrincipal) {{
        throw "Exchange did not return the service principal for Application ID $AppId."
    }}

    $Assignment = Get-ManagementRoleAssignment -Identity $AssignmentName -ErrorAction SilentlyContinue
    if (-not $Assignment) {{
        New-ManagementRoleAssignment -Name $AssignmentName -Role 'Application Mail.Send' -App $ServicePrincipal.ObjectId -CustomResourceScope $ScopeName -ErrorAction Stop | Out-Null
    }}

    $Authorization = Test-ServicePrincipalAuthorization -Identity $AppId -Resource $SenderMailbox
    $Authorization | Format-Table

    [ordered]@{{
        completedAt = (Get-Date).ToUniversalTime().ToString('o')
        authenticationMode = $AuthenticationMode
        tenantId = $TenantId
        applicationClientId = $AppId
        enterpriseApplicationObjectId = $EnterpriseAppObjectId
        senderMailbox = $SenderMailbox
        scopeName = $ScopeName
        role = 'Application Mail.Send'
        inScope = [bool]$Authorization.InScope
    }} | ConvertTo-Json | Set-Content -LiteralPath $ResultPath -Encoding utf8
    Write-Host "Setup result written to $ResultPath"
}}
finally {{
    Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue
}}

# Do not also grant unscoped Microsoft Graph Mail.Send application permission.
# Exchange and Entra application permissions are additive.
"""
