[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[^@\s]+@[^@\s]+\.[^@\s]+$')]
    [string]$AdminEmail,

    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,39}$')]
    [string]$InstanceName = 'cmdb',

    [string]$Image = 'ghcr.io/ipt-holdings-pty-ltd/ipt-cmdb:latest',

    [ValidateRange(1, 65535)]
    [int]$Port = 3000,

    [string]$InstanceRoot,

    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $InstanceRoot) {
    $InstanceRoot = Join-Path $repositoryRoot ".appliance\$InstanceName"
}
$instancePath = [System.IO.Path]::GetFullPath($InstanceRoot)
$secretPath = Join-Path $instancePath 'secrets'
$backupPath = Join-Path $instancePath 'backups'
$environmentPath = Join-Path $instancePath '.env.appliance'

if ((Test-Path $environmentPath) -and -not $Force) {
    throw "An appliance configuration already exists at $environmentPath. Use -Force only when deliberately replacing an unused configuration."
}

New-Item -ItemType Directory -Force -Path $secretPath, $backupPath | Out-Null

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

$secretForCompose = $secretPath.Replace('\', '/')
$backupForCompose = $backupPath.Replace('\', '/')
$environment = @(
    "COMPOSE_PROJECT_NAME=cmdb-$InstanceName"
    "CMDB_IMAGE=$Image"
    'CMDB_BIND_ADDRESS=127.0.0.1'
    "CMDB_PORT=$Port"
    "CMDB_SECRET_DIRECTORY=$secretForCompose"
    "CMDB_BACKUP_DIRECTORY=$backupForCompose"
    "BOOTSTRAP_ADMIN_EMAIL=$($AdminEmail.ToLowerInvariant())"
    'AUTH_MODE=local'
    'LOCAL_MFA_POLICY=all'
    'CMDB_APP_CPU_LIMIT=1.0'
    'CMDB_APP_MEMORY_LIMIT=1g'
    'CMDB_POSTGRES_CPU_LIMIT=1.0'
    'CMDB_POSTGRES_MEMORY_LIMIT=1g'
) -join [Environment]::NewLine
[System.IO.File]::WriteAllText($environmentPath, $environment, $utf8WithoutBom)

Write-Host "Appliance configuration created: $environmentPath"
Write-Host "Bootstrap administrator: $($AdminEmail.ToLowerInvariant())"
Write-Host "Bootstrap password file: $(Join-Path $secretPath 'bootstrap-admin-password.txt')"
Write-Host "The first login requires authenticator enrollment. Do not publish port $Port directly."
Write-Host "Start with: docker compose --env-file `"$environmentPath`" -f compose.appliance.yml up -d"
