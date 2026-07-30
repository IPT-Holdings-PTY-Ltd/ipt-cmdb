[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[^@\s]+@[^@\s]+\.[^@\s]+$')]
    [string]$AdminEmail,

    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl,

    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,39}$')]
    [string]$InstanceName = 'cmdb',

    [Parameter(Mandatory = $true)]
    [string]$Image,

    [ValidateRange(1, 65535)]
    [int]$Port = 3000,

    [string]$InstanceRoot
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Test-ImmutableReleaseImage([string]$Reference) {
    return $Reference -match (
        '^[A-Za-z0-9][A-Za-z0-9._-]*(?::\d+)?' +
        '(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*' +
        '(?:(?::v?\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?)?' +
        '@sha256:[0-9a-fA-F]{64}|' +
        ':v?\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?)$'
    )
}

function Get-NormalizedPublicBaseUrl([string]$Value) {
    try {
        $uri = [System.Uri]::new($Value, [System.UriKind]::Absolute)
    }
    catch {
        throw 'PublicBaseUrl must be an absolute HTTPS origin.'
    }
    $isLoopbackHttp = (
        $uri.Scheme -eq 'http' -and
        $uri.Host -in @('localhost', '127.0.0.1', '::1')
    )
    if ($uri.Scheme -ne 'https' -and -not $isLoopbackHttp) {
        throw 'PublicBaseUrl must use HTTPS. HTTP is allowed only for an explicit loopback origin.'
    }
    if (
        $uri.UserInfo -or
        $uri.Query -or
        $uri.Fragment -or
        $uri.AbsolutePath -notin @('', '/')
    ) {
        throw 'PublicBaseUrl must contain only the public origin, without credentials, a path, query, or fragment.'
    }
    return $Value.TrimEnd('/')
}

if (-not (Test-ImmutableReleaseImage $Image)) {
    throw 'Image must use an immutable release tag such as :v1.2.3 or a sha256 digest. Mutable tags such as :latest are not accepted.'
}
$normalizedPublicBaseUrl = Get-NormalizedPublicBaseUrl $PublicBaseUrl
$publicUri = [System.Uri]::new($normalizedPublicBaseUrl)
if (-not $publicUri.IsLoopback -and $Image -notmatch '@sha256:[0-9a-fA-F]{64}$') {
    throw 'A non-loopback deployment must pin Image by sha256 digest. Version tags are allowed only for explicit loopback evaluation.'
}
$imageDigest = 'unknown'
if ($Image -match '@(sha256:[0-9a-fA-F]{64})$') {
    $imageDigest = $Matches[1].ToLowerInvariant()
}

if (-not $InstanceRoot) {
    $InstanceRoot = Join-Path $repositoryRoot ".appliance\$InstanceName"
}
$instancePath = [System.IO.Path]::GetFullPath($InstanceRoot)
if (Test-Path -LiteralPath $instancePath) {
    throw "The appliance instance path already exists: $instancePath. Existing instance directories and secrets are never replaced."
}

$instanceParent = Split-Path -Parent $instancePath
$instanceLeaf = Split-Path -Leaf $instancePath
New-Item -ItemType Directory -Force -Path $instanceParent | Out-Null
$stagingPath = Join-Path $instanceParent "$instanceLeaf-initializing-$([guid]::NewGuid().ToString('N'))"
$secretPath = Join-Path $stagingPath 'secrets'
$backupPath = Join-Path $stagingPath 'backups'
$environmentPath = Join-Path $stagingPath '.env.appliance'

function New-UrlSafeSecret([int]$ByteCount, [switch]$KeepPadding) {
    $bytes = [byte[]]::new($ByteCount)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $value = [Convert]::ToBase64String($bytes).Replace('+', '-').Replace('/', '_')
    if (-not $KeepPadding) {
        $value = $value.TrimEnd('=')
    }
    return $value
}

$postgresPassword = New-UrlSafeSecret 32
$bootstrapPassword = New-UrlSafeSecret 32
$mfaKey = New-UrlSafeSecret 32 -KeepPadding
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)

try {
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    if ($IsWindows) {
        $icacls = Get-Command icacls.exe -ErrorAction SilentlyContinue
        if (-not $icacls) {
            throw 'icacls.exe is required to protect appliance secrets on Windows.'
        }
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        & $icacls.Source $stagingPath /inheritance:r /grant:r `
            "*$identity`:(OI)(CI)F" `
            '*S-1-5-18:(OI)(CI)F' `
            '*S-1-5-32-544:(OI)(CI)F' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to restrict the appliance instance directory.'
        }
    }
    New-Item -ItemType Directory -Path $secretPath, $backupPath | Out-Null

    [System.IO.File]::WriteAllText(
        (Join-Path $secretPath 'postgres-password.txt'),
        $postgresPassword,
        $utf8WithoutBom
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $secretPath 'database-url.txt'),
        "postgresql://cmdb:$postgresPassword@postgres:5432/cmdb?sslmode=disable",
        $utf8WithoutBom
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $secretPath 'bootstrap-admin-password.txt'),
        $bootstrapPassword,
        $utf8WithoutBom
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $secretPath 'mfa-encryption-key.txt'),
        $mfaKey,
        $utf8WithoutBom
    )

    $finalSecretPath = (Join-Path $instancePath 'secrets').Replace('\', '/')
    $finalBackupPath = (Join-Path $instancePath 'backups').Replace('\', '/')
    $environment = @(
        "COMPOSE_PROJECT_NAME=cmdb-$InstanceName"
        "CMDB_IMAGE=$Image"
        "CMDB_IMAGE_DIGEST=$imageDigest"
        'CMDB_BIND_ADDRESS=127.0.0.1'
        "CMDB_PORT=$Port"
        'CMDB_MIGRATION_LOCK_TIMEOUT_MS=60000'
        'CMDB_MIGRATION_STATEMENT_TIMEOUT_MS=900000'
        "PUBLIC_BASE_URL=$normalizedPublicBaseUrl"
        "CMDB_SECRET_DIRECTORY=$finalSecretPath"
        "CMDB_BACKUP_DIRECTORY=$finalBackupPath"
        "BOOTSTRAP_ADMIN_EMAIL=$($AdminEmail.ToLowerInvariant())"
        'AUTH_MODE=local'
        'FORWARDED_ALLOW_IPS='
        'LOCAL_MFA_POLICY=all'
        'LOCAL_LOGIN_IDENTIFIER_LIMIT=5'
        'LOCAL_LOGIN_IDENTIFIER_WINDOW_SECONDS=900'
        'LOCAL_LOGIN_SOURCE_LIMIT=20'
        'LOCAL_LOGIN_SOURCE_WINDOW_SECONDS=900'
        'LOCAL_LOGIN_PENDING_TTL_SECONDS=120'
        'LOCAL_LOGIN_THROTTLE_AUDIT_SECONDS=300'
        'CMDB_APP_CPU_LIMIT=1.0'
        'CMDB_APP_MEMORY_LIMIT=1g'
        'CMDB_POSTGRES_CPU_LIMIT=1.0'
        'CMDB_POSTGRES_MEMORY_LIMIT=1g'
    ) -join [Environment]::NewLine
    [System.IO.File]::WriteAllText($environmentPath, $environment, $utf8WithoutBom)

    if (-not $IsWindows) {
        $chmod = Get-Command chmod -ErrorAction SilentlyContinue
        if (-not $chmod) {
            throw 'chmod is required to protect appliance secrets on this host.'
        }
        & $chmod.Source 700 $stagingPath $secretPath $backupPath
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to restrict the appliance instance directories.'
        }
        $secretFiles = @(
            Get-ChildItem -LiteralPath $secretPath -File | Select-Object -ExpandProperty FullName
        )
        & $chmod.Source 600 $environmentPath
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to restrict the appliance environment file.'
        }
        & $chmod.Source 444 @secretFiles
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to restrict the appliance secret files.'
        }
    }

    if (Test-Path -LiteralPath $instancePath) {
        throw "The appliance instance path appeared while initialization was in progress: $instancePath"
    }
    Move-Item -LiteralPath $stagingPath -Destination $instancePath
}
finally {
    if (Test-Path -LiteralPath $stagingPath) {
        Remove-Item -LiteralPath $stagingPath -Recurse -Force
    }
}

$finalEnvironmentPath = Join-Path $instancePath '.env.appliance'
Write-Host "Appliance configuration created: $finalEnvironmentPath"
Write-Host "Bootstrap administrator: $($AdminEmail.ToLowerInvariant())"
Write-Host "Bootstrap password file: $(Join-Path $instancePath 'secrets\bootstrap-admin-password.txt')"
Write-Host "The first login requires authenticator enrollment. Do not publish port $Port directly."
Write-Host "Start with: docker compose --env-file `"$finalEnvironmentPath`" -f compose.appliance.yml up -d --wait"
