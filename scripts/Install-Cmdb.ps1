[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[^@\s]+@[^@\s]+\.[^@\s]+$')]
    [string]$AdminEmail,

    [Parameter(Mandatory = $true)]
    [string]$PublicBaseUrl,

    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,39}$')]
    [string]$InstanceName = 'cmdb',

    [string]$Image,

    [ValidateRange(1, 65535)]
    [int]$Port = 3000,

    [string]$InstanceRoot,

    [ValidateRange(30, 900)]
    [int]$WaitSeconds = 180
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$composeFile = Join-Path $repositoryRoot 'compose.appliance.yml'
$initializer = Join-Path $PSScriptRoot 'Initialize-Appliance.ps1'

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

function Get-ManifestImage {
    $jsonManifestPath = Join-Path $repositoryRoot 'release-manifest.json'
    if (Test-Path -LiteralPath $jsonManifestPath -PathType Leaf) {
        try {
            $manifest = Get-Content -LiteralPath $jsonManifestPath -Raw | ConvertFrom-Json
        }
        catch {
            throw "The release manifest is not valid JSON: $jsonManifestPath"
        }
        $reference = [string]$manifest.image.reference
        if (-not (Test-ImmutableReleaseImage $reference) -or $reference -notmatch '@sha256:') {
            throw "The release manifest does not contain a valid immutable image reference: $jsonManifestPath"
        }
        return $reference
    }

    # Transitional compatibility for release bundles created before manifest v1.
    $manifestPath = Join-Path $repositoryRoot 'release-manifest.txt'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $null
    }
    $container = $null
    $digest = $null
    foreach ($line in Get-Content -LiteralPath $manifestPath) {
        if ($line -match '^Container:\s*(\S+)\s*$') {
            $container = $Matches[1]
        }
        elseif ($line -match '^Digest:\s*(sha256:[0-9a-fA-F]{64})\s*$') {
            $digest = $Matches[1]
        }
    }
    if (-not $container -or -not $digest) {
        throw "The release manifest is present but does not contain a valid Container and Digest: $manifestPath"
    }
    return "$container@$digest"
}

function Assert-DockerComposeV2 {
    try {
        $versionOutput = (& docker compose version --short 2>&1 | Out-String).Trim()
    }
    catch {
        throw 'Docker with the Compose v2 plugin is required.'
    }
    if ($LASTEXITCODE -ne 0 -or $versionOutput -notmatch '^v?(\d+)\.') {
        throw 'Docker with the Compose v2 plugin is required.'
    }
    if ([int]$Matches[1] -lt 2) {
        throw "Docker Compose v2 or newer is required; detected $versionOutput."
    }
}

function Invoke-Docker([string[]]$Arguments) {
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')"
    }
}

function Wait-LocalEndpoint([string]$Path, [int]$TimeoutSeconds) {
    $uri = "http://127.0.0.1:$Port$Path"
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $uri -TimeoutSec 5 -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            # The service can return 503 while migrations or bootstrap are still running.
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for $uri."
}

if (-not $Image) {
    $Image = Get-ManifestImage
    if (-not $Image) {
        throw 'Image is required when release-manifest.json is not present beside the release bundle.'
    }
    Write-Host "Using the immutable container digest from the release manifest: $Image"
}
if (-not (Test-ImmutableReleaseImage $Image)) {
    throw 'Image must use an immutable release tag such as :v1.2.3 or a sha256 digest. Mutable tags such as :latest are not accepted.'
}
$PublicBaseUrl = Get-NormalizedPublicBaseUrl $PublicBaseUrl
$publicUri = [System.Uri]::new($PublicBaseUrl)
if (-not $publicUri.IsLoopback -and $Image -notmatch '@sha256:[0-9a-fA-F]{64}$') {
    throw 'A non-loopback deployment must pin Image by sha256 digest. Version tags are allowed only for explicit loopback evaluation.'
}

if (-not $InstanceRoot) {
    $InstanceRoot = Join-Path $repositoryRoot ".appliance\$InstanceName"
}
$instancePath = [System.IO.Path]::GetFullPath($InstanceRoot)
if (Test-Path -LiteralPath $instancePath) {
    throw "The appliance instance path already exists and its secrets will not be regenerated: $instancePath"
}

if (-not $PSCmdlet.ShouldProcess(
        $instancePath,
        "initialize and start IPT CMDB appliance $InstanceName"
    )) {
    return
}

Assert-DockerComposeV2

$initializeParameters = @{
    AdminEmail   = $AdminEmail
    PublicBaseUrl = $PublicBaseUrl
    InstanceName = $InstanceName
    Image        = $Image
    Port         = $Port
    InstanceRoot = $instancePath
}
& $initializer @initializeParameters

$environmentPath = Join-Path $instancePath '.env.appliance'
$passwordPath = Join-Path $instancePath 'secrets\bootstrap-admin-password.txt'
$composeArguments = @(
    'compose',
    '--env-file',
    $environmentPath,
    '-f',
    $composeFile
)

try {
    Invoke-Docker ([string[]]($composeArguments + @('config', '--quiet')))
    Invoke-Docker ([string[]]($composeArguments + @('pull', 'cmdb', 'postgres')))
    Invoke-Docker (
        [string[]](
            $composeArguments +
            @('up', '-d', '--wait', '--wait-timeout', $WaitSeconds.ToString())
        )
    )
    Wait-LocalEndpoint '/api/live' $WaitSeconds
    Wait-LocalEndpoint '/api/ready' $WaitSeconds
}
catch {
    Write-Warning 'The appliance configuration and secrets were preserved, but startup did not complete.'
    $diagnosticArguments = [string[]]($composeArguments + @('ps'))
    & docker @diagnosticArguments
    Write-Host 'Inspect sanitized runtime diagnostics with:'
    Write-Host "docker compose --env-file `"$environmentPath`" -f `"$composeFile`" logs --tail 200 cmdb postgres"
    Write-Host "Do not rerun the installer. Correct the reported issue and resume with:"
    Write-Host "docker compose --env-file `"$environmentPath`" -f `"$composeFile`" up -d --wait"
    throw
}

Write-Host ''
Write-Host "IPT CMDB is live at http://127.0.0.1:$Port"
Write-Host "Configured public origin: $PublicBaseUrl"
Write-Host "Bootstrap administrator: $($AdminEmail.ToLowerInvariant())"
Write-Host "Bootstrap password file: $passwordPath"
Write-Host 'The password value was not printed. Sign in, enroll MFA, and rotate the bootstrap password.'
Write-Host 'Keep the instance secret directory protected and configure verified off-host backups.'
