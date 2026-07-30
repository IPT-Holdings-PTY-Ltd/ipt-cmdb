#Requires -Version 7.2

<#
.SYNOPSIS
Checks for or applies a verified IPT CMDB container update.

.DESCRIPTION
Check is the default and never changes the running instance. Apply requires exact
ApproveVersion and ApproveManifestSha256 values, an immutable image digest, and either
a verified appliance backup or operator-supplied external PostgreSQL recovery-point
evidence.

The updater never follows a mutable container tag and never pulls or recreates
PostgreSQL. Once the target migration command starts, failures are deliberately left
for reviewed recovery because reverting an image cannot reverse forward-only schema
migrations.
#>

[CmdletBinding()]
param(
    [ValidateSet('Check', 'Apply')]
    [string]$Action = 'Check',

    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,39}$')]
    [string]$InstanceName = 'cmdb',

    [string]$InstanceRoot,

    [string]$ManifestPath,

    [switch]$WorkerSplit,

    [ValidateRange(30, 900)]
    [int]$WaitSeconds = 180,

    [string]$ApproveVersion,

    [ValidatePattern('^[0-9a-f]{64}$')]
    [string]$ApproveManifestSha256,

    [ValidateLength(0, 512)]
    [string]$RecoveryPointEvidence = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$releaseApi = 'https://api.github.com/repos/IPT-Holdings-PTY-Ltd/ipt-cmdb/releases/latest'
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)

function Get-RequiredProperty {
    param(
        [Parameter(Mandatory = $true)]
        [object]$InputObject,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Context
    )

    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        throw "$Context is missing the required '$Name' value."
    }
    return $property.Value
}

function Assert-SafeDownloadUri {
    param([Parameter(Mandatory = $true)][string]$Value)

    try {
        $uri = [System.Uri]::new($Value, [System.UriKind]::Absolute)
    }
    catch {
        throw 'The GitHub release returned an invalid asset URL.'
    }
    if (
        $uri.Scheme -ne 'https' -or
        $uri.UserInfo -or
        $uri.Host -notin @(
            'github.com',
            'objects.githubusercontent.com',
            'release-assets.githubusercontent.com'
        )
    ) {
        throw 'The GitHub release returned an untrusted asset URL.'
    }
    return $uri.AbsoluteUri
}

function Assert-ManifestChecksum {
    param(
        [Parameter(Mandatory = $true)][string]$Manifest,
        [Parameter(Mandatory = $true)][string]$ChecksumFile
    )

    $matchingHashes = @()
    foreach ($line in Get-Content -LiteralPath $ChecksumFile) {
        if ($line -match '^(?<hash>[0-9a-fA-F]{64})\s+[ *]?(?<name>.+?)\s*$') {
            $name = $Matches.name.Replace('\', '/').TrimStart('.', '/')
            if ($name -eq 'release-manifest.json') {
                $matchingHashes += $Matches.hash.ToLowerInvariant()
            }
        }
    }
    if ($matchingHashes.Count -ne 1) {
        throw 'SHA256SUMS must contain exactly one checksum for release-manifest.json.'
    }
    $actual = (Get-FileHash -LiteralPath $Manifest -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -cne $matchingHashes[0]) {
        throw 'The downloaded release manifest failed SHA-256 verification.'
    }
}

function Get-ReleaseManifestFile {
    if ($ManifestPath) {
        $candidate = [System.IO.Path]::GetFullPath($ManifestPath)
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $candidate = Join-Path $candidate 'release-manifest.json'
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "The local release manifest does not exist: $candidate"
        }
        $checksumPath = Join-Path (Split-Path -Parent $candidate) 'SHA256SUMS'
        $checksumVerified = $false
        if (Test-Path -LiteralPath $checksumPath -PathType Leaf) {
            Assert-ManifestChecksum -Manifest $candidate -ChecksumFile $checksumPath
            $checksumVerified = $true
        }
        return [pscustomobject]@{
            Path             = $candidate
            Source           = "local file $candidate"
            ExpectedTag      = $null
            ReleaseUrl       = $null
            ChecksumVerified = $checksumVerified
            ManifestSha256   = (
                Get-FileHash -LiteralPath $candidate -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            TemporaryPath    = $null
        }
    }

    $temporaryPath = Join-Path (
        [System.IO.Path]::GetTempPath()
    ) "ipt-cmdb-update-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $temporaryPath | Out-Null
    try {
        $headers = @{
            Accept               = 'application/vnd.github+json'
            'User-Agent'         = 'ipt-cmdb-safe-updater'
            'X-GitHub-Api-Version' = '2022-11-28'
        }
        if ($env:GITHUB_TOKEN) {
            $headers.Authorization = "Bearer $($env:GITHUB_TOKEN)"
        }
        $release = Invoke-RestMethod -Uri $releaseApi -Headers $headers -TimeoutSec 30
        if (
            [bool](Get-RequiredProperty $release 'draft' 'GitHub release') -or
            [bool](Get-RequiredProperty $release 'prerelease' 'GitHub release')
        ) {
            throw 'The GitHub latest-release endpoint returned a draft or prerelease.'
        }

        $assets = @(Get-RequiredProperty $release 'assets' 'GitHub release')
        $manifestAssets = @($assets | Where-Object { $_.name -eq 'release-manifest.json' })
        $checksumAssets = @($assets | Where-Object { $_.name -eq 'SHA256SUMS' })
        if ($manifestAssets.Count -ne 1 -or $checksumAssets.Count -ne 1) {
            throw 'The GitHub release must contain one release-manifest.json and one SHA256SUMS.'
        }

        $downloadedManifest = Join-Path $temporaryPath 'release-manifest.json'
        $downloadedChecksums = Join-Path $temporaryPath 'SHA256SUMS'
        $manifestUri = Assert-SafeDownloadUri ([string]$manifestAssets[0].browser_download_url)
        $checksumUri = Assert-SafeDownloadUri ([string]$checksumAssets[0].browser_download_url)
        Invoke-WebRequest -Uri $checksumUri -Headers $headers -OutFile $downloadedChecksums `
            -TimeoutSec 60
        Invoke-WebRequest -Uri $manifestUri -Headers $headers -OutFile $downloadedManifest `
            -TimeoutSec 60
        Assert-ManifestChecksum -Manifest $downloadedManifest -ChecksumFile $downloadedChecksums

        return [pscustomobject]@{
            Path             = $downloadedManifest
            Source           = 'the latest stable GitHub release'
            ExpectedTag      = [string](Get-RequiredProperty $release 'tag_name' 'GitHub release')
            ReleaseUrl       = [string](Get-RequiredProperty $release 'html_url' 'GitHub release')
            ChecksumVerified = $true
            ManifestSha256   = (
                Get-FileHash -LiteralPath $downloadedManifest -Algorithm SHA256
            ).Hash.ToLowerInvariant()
            TemporaryPath    = $temporaryPath
        }
    }
    catch {
        Remove-Item -LiteralPath $temporaryPath -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Read-ValidatedManifest {
    param([Parameter(Mandatory = $true)][pscustomobject]$ManifestFile)

    try {
        $document = Get-Content -LiteralPath $ManifestFile.Path -Raw | ConvertFrom-Json -Depth 20
    }
    catch {
        throw 'release-manifest.json is not valid JSON.'
    }

    $manifestVersion = Get-RequiredProperty $document 'manifestVersion' 'Release manifest'
    if ([int]$manifestVersion -ne 1) {
        throw "Unsupported release manifest version: $manifestVersion"
    }

    $version = [string](Get-RequiredProperty $document 'version' 'Release manifest')
    if ($version -cnotmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$') {
        throw 'The release manifest version is not valid semantic version text.'
    }
    $tag = [string](Get-RequiredProperty $document 'tag' 'Release manifest')
    if ($tag -cne "v$version") {
        throw 'The release manifest tag does not match its version.'
    }
    if ($ManifestFile.ExpectedTag -and $ManifestFile.ExpectedTag -cne $tag) {
        throw 'The GitHub release tag does not match the downloaded manifest.'
    }
    $prereleaseValue = Get-RequiredProperty $document 'prerelease' 'Release manifest'
    if ($prereleaseValue -isnot [bool]) {
        throw 'The release manifest prerelease value must be Boolean.'
    }
    $prerelease = [bool]$prereleaseValue
    if ($ManifestFile.ExpectedTag -and $prerelease) {
        throw 'The GitHub latest stable release contains a prerelease manifest.'
    }
    $commit = [string](Get-RequiredProperty $document 'commit' 'Release manifest')
    if ($commit -cnotmatch '^[0-9a-f]{40}$') {
        throw 'The release manifest commit is not a full lowercase SHA.'
    }

    $image = Get-RequiredProperty $document 'image' 'Release manifest'
    $imageName = [string](Get-RequiredProperty $image 'name' 'Release manifest image')
    $imageDigest = [string](Get-RequiredProperty $image 'digest' 'Release manifest image')
    $imageReference = [string](Get-RequiredProperty $image 'reference' 'Release manifest image')
    if (
        $imageName -cnotmatch '^[a-z0-9](?:[a-z0-9._:/-]*[a-z0-9])?$' -or
        $imageName.Contains('//') -or
        $imageName.Contains('..') -or
        $imageName.Contains('@')
    ) {
        throw 'The release manifest image name is not a valid lowercase registry path.'
    }
    if ($imageDigest -cnotmatch '^sha256:[0-9a-f]{64}$') {
        throw 'The release manifest image digest is not a lowercase sha256 digest.'
    }
    if ($imageReference -cne "$imageName@$imageDigest") {
        throw 'The release manifest exact image reference does not match its name and digest.'
    }
    if ($imageReference -match '(?i):latest(?:@|$)') {
        throw 'Mutable latest container references are never accepted.'
    }

    $database = Get-RequiredProperty $document 'database' 'Release manifest'
    $postgresqlMajor = [int](
        Get-RequiredProperty $database 'postgresqlMajor' 'Release manifest database'
    )
    if ($postgresqlMajor -ne 16) {
        throw "This updater supports PostgreSQL major 16; the manifest requires $postgresqlMajor."
    }
    $schemaVersion = [string](
        Get-RequiredProperty $database 'schemaVersion' 'Release manifest database'
    )
    if ($schemaVersion -cnotmatch '^\d{4}\.\d{2}\.\d{2}\.\d+$') {
        throw 'The release manifest schema version is invalid.'
    }
    $historyHash = [string](
        Get-RequiredProperty $database 'schemaHistorySha256' 'Release manifest database'
    )
    if ($historyHash -cnotmatch '^[0-9a-f]{64}$') {
        throw 'The release manifest schema-history checksum is invalid.'
    }
    $change = [string](
        Get-RequiredProperty $database 'changeClassification' 'Release manifest database'
    )
    if ($change -notin @('none', 'expand-only-compatible', 'restore-required')) {
        throw 'The release manifest database change classification is invalid.'
    }
    $rollback = [string](
        Get-RequiredProperty $database 'rollbackPolicy' 'Release manifest database'
    )
    if ($rollback -notin @('previous-image-compatible', 'database-restore-required')) {
        throw 'The release manifest rollback policy is invalid.'
    }
    $rollbackBoundary = [string](
        Get-RequiredProperty $database 'rollbackBoundary' 'Release manifest database'
    )
    if ($rollbackBoundary -cne 'schema-version-change-requires-database-restore') {
        throw 'The release manifest rollback boundary is invalid.'
    }
    $migrationPolicy = [string](
        Get-RequiredProperty $database 'migrationPolicy' 'Release manifest database'
    )
    if ($migrationPolicy -cne 'forward-only') {
        throw 'The release manifest migration policy is invalid.'
    }
    $backupRequiredValue = Get-RequiredProperty $database 'backupRequired' `
        'Release manifest database'
    if ($backupRequiredValue -isnot [bool]) {
        throw 'The release manifest backupRequired value must be Boolean.'
    }
    $backupRequired = [bool]$backupRequiredValue
    if ($backupRequired -ne ($change -ne 'none')) {
        throw 'The release manifest backup requirement contradicts its database classification.'
    }
    if ($change -ne 'none' -and $rollback -ne 'database-restore-required') {
        throw 'Every schema-changing release must require database restore for rollback.'
    }

    return [pscustomobject]@{
        Version              = $version
        Tag                  = $tag
        Prerelease           = $prerelease
        Commit               = $commit
        ImageName            = $imageName
        ImageDigest          = $imageDigest
        ImageReference       = $imageReference
        SchemaVersion        = $schemaVersion
        SchemaHistorySha256  = $historyHash
        PostgreSqlMajor      = $postgresqlMajor
        ChangeClassification = $change
        BackupRequired       = $backupRequired
        RollbackPolicy       = $rollback
        RollbackBoundary     = $rollbackBoundary
        MigrationPolicy      = $migrationPolicy
    }
}

function ConvertFrom-DotEnvValue {
    param([Parameter(Mandatory = $true)][string]$Value)

    $trimmed = $Value.Trim()
    if (
        $trimmed.Length -ge 2 -and
        (
            ($trimmed[0] -eq '"' -and $trimmed[-1] -eq '"') -or
            ($trimmed[0] -eq "'" -and $trimmed[-1] -eq "'")
        )
    ) {
        return $trimmed.Substring(1, $trimmed.Length - 2)
    }
    return $trimmed
}

function Read-EnvironmentDocument {
    param([Parameter(Mandatory = $true)][string]$Path)

    $text = [System.IO.File]::ReadAllText($Path)
    $values = [System.Collections.Generic.Dictionary[string, string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($line in ($text -split '\r?\n')) {
        if ($line -match '^\s*(?!#)(?:export\s+)?(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?<value>.*)$') {
            $key = $Matches.key
            if ($values.ContainsKey($key)) {
                throw "The environment file contains duplicate $key entries."
            }
            $values.Add($key, (ConvertFrom-DotEnvValue $Matches.value))
        }
    }
    return [pscustomobject]@{ Text = $text; Values = $values }
}

function Get-RequiredEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [System.Collections.Generic.Dictionary[string, string]]$Values,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not $Values.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace($Values[$Name])) {
        throw "The instance environment must define $Name."
    }
    return $Values[$Name]
}

function Get-ImageRepository {
    param([Parameter(Mandatory = $true)][string]$Reference)

    $withoutDigest = ($Reference -split '@', 2)[0]
    $lastSlash = $withoutDigest.LastIndexOf('/')
    $lastColon = $withoutDigest.LastIndexOf(':')
    if ($lastColon -gt $lastSlash) {
        return $withoutDigest.Substring(0, $lastColon)
    }
    return $withoutDigest
}

function Get-InstanceConfiguration {
    if (-not $InstanceRoot) {
        $script:InstanceRoot = Join-Path $repositoryRoot ".appliance\$InstanceName"
    }
    $instancePath = [System.IO.Path]::GetFullPath($InstanceRoot)
    if (-not (Test-Path -LiteralPath $instancePath -PathType Container)) {
        throw "The instance directory does not exist: $instancePath"
    }

    $applianceEnvironment = Join-Path $instancePath '.env.appliance'
    $productionEnvironment = Join-Path $instancePath '.env.production'
    if (Test-Path -LiteralPath $applianceEnvironment -PathType Leaf) {
        $mode = 'appliance'
        $environmentPath = $applianceEnvironment
        $composePath = Join-Path $repositoryRoot 'compose.appliance.yml'
    }
    elseif (Test-Path -LiteralPath $productionEnvironment -PathType Leaf) {
        $mode = 'external'
        $environmentPath = $productionEnvironment
        $composePath = Join-Path $repositoryRoot 'compose.production.yml'
    }
    else {
        throw 'InstanceRoot must contain .env.appliance or .env.production.'
    }
    if (-not (Test-Path -LiteralPath $composePath -PathType Leaf)) {
        throw "The required Compose file does not exist: $composePath"
    }
    $workerPath = Join-Path $repositoryRoot 'compose.worker.yml'
    if ($WorkerSplit -and -not (Test-Path -LiteralPath $workerPath -PathType Leaf)) {
        throw "The worker Compose overlay does not exist: $workerPath"
    }

    $environment = Read-EnvironmentDocument $environmentPath
    $currentImage = Get-RequiredEnvironmentValue $environment.Values 'CMDB_IMAGE'
    if ($currentImage -notmatch '@(?<digest>sha256:[0-9a-fA-F]{64})$') {
        throw 'Current CMDB_IMAGE must be pinned by digest before this updater can manage it.'
    }
    $imageDigestFromReference = $Matches.digest.ToLowerInvariant()
    $digestMetadataPresent = (
        $environment.Values.ContainsKey('CMDB_IMAGE_DIGEST') -and
        -not [string]::IsNullOrWhiteSpace($environment.Values['CMDB_IMAGE_DIGEST'])
    )
    if ($digestMetadataPresent) {
        $currentDigest = $environment.Values['CMDB_IMAGE_DIGEST']
        if ($currentDigest -cnotmatch '^sha256:[0-9a-f]{64}$') {
            throw 'Current CMDB_IMAGE_DIGEST must be a lowercase sha256 digest.'
        }
        if ($imageDigestFromReference -cne $currentDigest) {
            throw 'Current CMDB_IMAGE and CMDB_IMAGE_DIGEST do not identify the same image.'
        }
    }
    else {
        # Older external-PostgreSQL environment templates did not persist this
        # informational value. The immutable CMDB_IMAGE remains authoritative;
        # Apply adds the metadata without changing any other setting.
        $currentDigest = $imageDigestFromReference
    }
    $portText = Get-RequiredEnvironmentValue $environment.Values 'CMDB_PORT'
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw 'CMDB_PORT must be an integer between 1 and 65535.'
    }
    if ($mode -eq 'appliance' -and $environment.Values.ContainsKey('POSTGRES_IMAGE')) {
        $postgresImage = $environment.Values['POSTGRES_IMAGE']
        if ($postgresImage -notmatch '(?:^|/)postgres:16(?:-|$)') {
            throw 'The appliance POSTGRES_IMAGE must remain on PostgreSQL major 16.'
        }
    }

    $composeArguments = @(
        'compose',
        '--env-file',
        $environmentPath,
        '-f',
        $composePath
    )
    if ($WorkerSplit) {
        $composeArguments += @('-f', $workerPath)
    }
    return [pscustomobject]@{
        InstancePath     = $instancePath
        Mode             = $mode
        EnvironmentPath  = $environmentPath
        Environment      = $environment
        ComposeArguments = [string[]]$composeArguments
        CurrentImage     = $currentImage
        CurrentDigest    = $currentDigest
        DigestMetadataPresent = $digestMetadataPresent
        Port             = $port
    }
}

function Assert-DockerComposeV2 {
    try {
        $output = (& docker compose version --short 2>&1 | Out-String).Trim()
    }
    catch {
        throw 'Docker with the Compose v2 plugin is required.'
    }
    if ($LASTEXITCODE -ne 0 -or $output -notmatch '^v?(?<major>\d+)\.') {
        throw 'Docker with the Compose v2 plugin is required.'
    }
    if ([int]$Matches.major -lt 2) {
        throw "Docker Compose v2 or newer is required; detected $output."
    }
}

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')"
    }
}

function Invoke-DockerCapture {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = @(& docker @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')"
    }
    return ($output | ForEach-Object { [string]$_ }) -join "`n"
}

function Get-ComposeProjectName {
    param([Parameter(Mandatory = $true)][pscustomobject]$Instance)

    if (
        $Instance.Environment.Values.ContainsKey('COMPOSE_PROJECT_NAME') -and
        -not [string]::IsNullOrWhiteSpace(
            $Instance.Environment.Values['COMPOSE_PROJECT_NAME']
        )
    ) {
        $projectName = $Instance.Environment.Values['COMPOSE_PROJECT_NAME']
    }
    else {
        # Compose derives the default project from the first Compose file's directory.
        $projectName = (Split-Path -Leaf $repositoryRoot).ToLowerInvariant()
        $projectName = $projectName -replace '^[^a-z0-9]+', ''
        $projectName = $projectName -replace '[^a-z0-9_-]', ''
    }
    if ($projectName -cnotmatch '^[a-z0-9][a-z0-9_-]{0,127}$') {
        throw 'The resolved Compose project name is invalid; set COMPOSE_PROJECT_NAME explicitly.'
    }
    return $projectName
}

function Get-ComposeServiceRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectName,
        [Parameter(Mandatory = $true)][ValidateSet('cmdb', 'worker')][string]$Service
    )

    $rawIds = Invoke-DockerCapture (
        [string[]]@(
            'ps',
            '-a',
            '--filter',
            "label=com.docker.compose.project=$ProjectName",
            '--filter',
            "label=com.docker.compose.service=$Service",
            '--format',
            '{{.ID}}'
        )
    )
    $containerIds = @(
        $rawIds -split '\r?\n' |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -cmatch '^[0-9a-f]{12,64}$' }
    )
    if ($containerIds.Count -gt 1) {
        throw (
            "The updater supports one $Service container, but found " +
            "$($containerIds.Count) in Compose project $ProjectName."
        )
    }
    if ($containerIds.Count -eq 0) {
        return [pscustomobject]@{
            Service     = $Service
            Exists      = $false
            ContainerId = $null
            Running     = $false
            Health      = 'absent'
        }
    }

    $containerId = $containerIds[0]
    $stateText = Invoke-DockerCapture (
        [string[]]@(
            'inspect',
            '--format',
            '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}',
            $containerId
        )
    )
    $stateLine = ($stateText -split '\r?\n' | Select-Object -Last 1).Trim().ToLowerInvariant()
    if ($stateLine -cnotmatch '^(?<running>true|false)\|(?<health>healthy|unhealthy|starting|none)$') {
        throw "Docker returned an invalid runtime state for the $Service container."
    }
    return [pscustomobject]@{
        Service     = $Service
        Exists      = $true
        ContainerId = $containerId
        Running     = $Matches.running -eq 'true'
        Health      = $Matches.health
    }
}

function Get-ComposeRuntimeTopology {
    param([Parameter(Mandatory = $true)][pscustomobject]$Instance)

    $projectName = Get-ComposeProjectName $Instance
    return [pscustomobject]@{
        ProjectName = $projectName
        Cmdb        = Get-ComposeServiceRuntime -ProjectName $projectName -Service cmdb
        Worker      = Get-ComposeServiceRuntime -ProjectName $projectName -Service worker
    }
}

function Get-RuntimeMetadata {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [ValidateSet('health', 'ready')][string]$Endpoint = 'health',
        [int]$TimeoutSeconds = 5
    )

    return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/$Endpoint" `
        -TimeoutSec $TimeoutSeconds
}

function Compare-SchemaVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    $pattern = '^(?<year>\d{4})\.(?<month>\d{2})\.(?<day>\d{2})\.(?<revision>\d+)$'
    if ($Left -cnotmatch $pattern) {
        throw "Runtime schema version '$Left' is invalid."
    }
    $leftParts = @(
        [int]$Matches.year,
        [int]$Matches.month,
        [int]$Matches.day,
        [int]$Matches.revision
    )
    if ($Right -cnotmatch $pattern) {
        throw "Target schema version '$Right' is invalid."
    }
    $rightParts = @(
        [int]$Matches.year,
        [int]$Matches.month,
        [int]$Matches.day,
        [int]$Matches.revision
    )
    for ($index = 0; $index -lt $leftParts.Count; $index += 1) {
        if ($leftParts[$index] -lt $rightParts[$index]) {
            return -1
        }
        if ($leftParts[$index] -gt $rightParts[$index]) {
            return 1
        }
    }
    return 0
}

function Test-ExpectedReadiness {
    param(
        [Parameter(Mandatory = $true)][object]$Response,
        [Parameter(Mandatory = $true)][pscustomobject]$Manifest
    )

    try {
        return (
            [string](Get-RequiredProperty $Response 'status' 'Readiness response') -eq 'ready' -and
            [bool](Get-RequiredProperty $Response 'schemaCurrent' 'Readiness response') -and
            [string](Get-RequiredProperty $Response 'applicationVersion' 'Readiness response') `
                -ceq $Manifest.Version -and
            [string](Get-RequiredProperty $Response 'imageDigest' 'Readiness response') `
                -ceq $Manifest.ImageDigest -and
            [string](Get-RequiredProperty $Response 'schemaVersion' 'Readiness response') `
                -ceq $Manifest.SchemaVersion -and
            [string](Get-RequiredProperty $Response 'expectedSchemaVersion' 'Readiness response') `
                -ceq $Manifest.SchemaVersion -and
            [string](
                Get-RequiredProperty $Response 'schemaHistorySha256' 'Readiness response'
            ) -ceq $Manifest.SchemaHistorySha256
        )
    }
    catch {
        return $false
    }
}

function Assert-InstalledTargetHealthy {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Instance,
        [Parameter(Mandatory = $true)][pscustomobject]$Manifest,
        [Parameter(Mandatory = $true)][pscustomobject]$Topology
    )

    $repair = (
        'The configured digest matches the target, but the installed target is not ' +
        'verified healthy. Inspect the CMDB container and /api/ready, then repair or ' +
        'recreate the service with the exact manifest image; do not treat this as success.'
    )
    if (
        -not $Topology.Cmdb.Exists -or
        -not $Topology.Cmdb.Running -or
        $Topology.Cmdb.Health -ne 'healthy'
    ) {
        throw $repair
    }
    try {
        $ready = Get-RuntimeMetadata -Port $Instance.Port -Endpoint ready -TimeoutSeconds 5
    }
    catch {
        throw $repair
    }
    if (-not (Test-ExpectedReadiness -Response $ready -Manifest $Manifest)) {
        throw $repair
    }
    return $ready
}

function Wait-ForExpectedReadiness {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][pscustomobject]$Manifest,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Get-RuntimeMetadata -Port $Port -Endpoint ready -TimeoutSeconds 5
            if (Test-ExpectedReadiness -Response $response -Manifest $Manifest) {
                return $response
            }
        }
        catch {
            # Startup can refuse requests or return 503 until migrations and bootstrap settle.
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw (
        "Timed out waiting for /api/ready to report version $($Manifest.Version), " +
        "digest $($Manifest.ImageDigest), and schema $($Manifest.SchemaVersion)."
    )
}

function Set-EnvironmentValues {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)]
        [System.Collections.Generic.Dictionary[string, string]]$ExistingValues,
        [Parameter(Mandatory = $true)][hashtable]$Updates
    )

    $result = $Text
    $newline = if ($Text.Contains("`r`n")) { "`r`n" } else { "`n" }
    foreach ($key in $Updates.Keys) {
        $value = [string]$Updates[$key]
        if ($value.Contains("`r") -or $value.Contains("`n")) {
            throw "The replacement value for $key is invalid."
        }
        if ($ExistingValues.ContainsKey($key)) {
            $pattern = "(?m)^(?<prefix>[ `t]*(?:export[ `t]+)?$([regex]::Escape($key))[ `t]*=).*$"
            $matches = [regex]::Matches($result, $pattern)
            if ($matches.Count -ne 1) {
                throw "The environment file does not contain exactly one editable $key entry."
            }
            $replacement = [System.Text.RegularExpressions.MatchEvaluator]{
                param($match)
                return $match.Groups['prefix'].Value + $value
            }
            $result = [regex]::Replace($result, $pattern, $replacement)
        }
        else {
            if ($result.Length -gt 0 -and -not $result.EndsWith("`n")) {
                $result += $newline
            }
            $result += "$key=$value$newline"
        }
    }
    return $result
}

function Replace-EnvironmentAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Text
    )

    $directory = Split-Path -Parent $Path
    $identifier = "$PID-$([guid]::NewGuid().ToString('N'))"
    $temporary = Join-Path $directory ".cmdb-update-$identifier.tmp"
    $backup = Join-Path $directory ".cmdb-update-$identifier.previous"
    try {
        [System.IO.File]::WriteAllText($temporary, $Text, $utf8WithoutBom)
        [System.IO.File]::Replace($temporary, $Path, $backup, $true)
        return $backup
    }
    catch {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        throw 'The environment file could not be replaced atomically.'
    }
}

function Restore-EnvironmentAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BackupPath
    )

    $failedCopy = "$BackupPath.failed-target"
    [System.IO.File]::Replace($BackupPath, $Path, $failedCopy, $true)
    Remove-Item -LiteralPath $failedCopy -Force -ErrorAction SilentlyContinue
}

function Enter-UpdateLock {
    param([Parameter(Mandatory = $true)][string]$InstancePath)

    $lockPath = Join-Path $InstancePath '.cmdb-update.lock'
    try {
        $stream = [System.IO.FileStream]::new(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch {
        throw 'Another CMDB update appears to be active for this instance.'
    }
    $payload = [System.Text.Encoding]::UTF8.GetBytes(
        "pid=$PID started=$([DateTime]::UtcNow.ToString('O'))"
    )
    $stream.SetLength(0)
    $stream.Write($payload, 0, $payload.Length)
    $stream.Flush($true)
    return $stream
}

function Assert-SafeRecoveryEvidence {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw 'External PostgreSQL apply requires non-empty RecoveryPointEvidence.'
    }
    if ($Value -match '[\x00-\x1f\x7f]') {
        throw 'RecoveryPointEvidence must be one printable line without control characters.'
    }
    if (
        $Value -match (
            '(?i)(?:postgres(?:ql)?://|authorization\s*[:=]|bearer\s+|' +
            '(?:password|secret|token|private[_ -]?key)\s*[:=])'
        )
    ) {
        throw 'RecoveryPointEvidence must contain only a non-secret ticket or recovery reference.'
    }
}

function Get-TextSha256 {
    param([Parameter(Mandatory = $true)][string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    return [Convert]::ToHexString(
        [System.Security.Cryptography.SHA256]::HashData($bytes)
    ).ToLowerInvariant()
}

function ConvertTo-PowerShellLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)

    return "'$($Value.Replace("'", "''"))'"
}

function Write-UpdateHistory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Record
    )

    $temporary = "$Path.$PID-$([guid]::NewGuid().ToString('N')).tmp"
    try {
        $json = $Record | ConvertTo-Json -Depth 8
        [System.IO.File]::WriteAllText($temporary, "$json`n", $utf8WithoutBom)
        [System.IO.File]::Move($temporary, $Path, $true)
    }
    catch {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        throw 'The sanitized update-history record could not be persisted.'
    }
}

function Invoke-VerifiedApplianceBackup {
    param([Parameter(Mandatory = $true)][pscustomobject]$Instance)

    $backupDirectory = Get-RequiredEnvironmentValue `
        $Instance.Environment.Values 'CMDB_BACKUP_DIRECTORY'
    $backupDirectory = [System.IO.Path]::GetFullPath($backupDirectory)
    if (-not (Test-Path -LiteralPath $backupDirectory -PathType Container)) {
        throw "The configured appliance backup directory does not exist: $backupDirectory"
    }
    $started = [DateTime]::UtcNow.AddSeconds(-2)
    Invoke-Docker (
        [string[]](
            $Instance.ComposeArguments +
            @('--profile', 'tools', 'run', '--no-deps', '--pull', 'never', '--rm', 'backup')
        )
    ) | Out-Host
    $candidates = @(
        Get-ChildItem -LiteralPath $backupDirectory -File -Filter 'cmdb-*.dump' |
            Where-Object { $_.LastWriteTimeUtc -ge $started } |
            Sort-Object LastWriteTimeUtc -Descending
    )
    if ($candidates.Count -lt 1) {
        throw 'The backup container completed without producing a new CMDB dump.'
    }
    $dump = $candidates[0]
    if ($dump.Name -cnotmatch '^cmdb-\d{8}T\d{6}Z\.dump$') {
        throw 'The backup container produced an unexpected archive filename.'
    }
    $checksumPath = "$($dump.FullName).sha256"
    if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
        throw 'The new appliance backup does not have its checksum file.'
    }
    $checksumLines = @(Get-Content -LiteralPath $checksumPath)
    if (
        $checksumLines.Count -ne 1 -or
        $checksumLines[0] -cnotmatch '^(?<hash>[0-9a-f]{64})\s+[ *]?(?<name>[^/\\]+)$' -or
        $Matches.name -cne $dump.Name
    ) {
        throw 'The new appliance backup checksum file is invalid.'
    }
    $actual = (Get-FileHash -LiteralPath $dump.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -cne $Matches.hash) {
        throw 'The new appliance backup failed host-side SHA-256 verification.'
    }
    Write-Host "Verified final appliance backup: $($dump.Name)"
    return [pscustomobject]@{
        FileName = $dump.Name
        Sha256   = $actual
    }
}

function Start-InstanceServices {
    param(
        [Parameter(Mandatory = $true)][pscustomobject]$Instance,
        [Parameter(Mandatory = $true)][bool]$StartWorker
    )

    Invoke-Docker (
        [string[]](
            $Instance.ComposeArguments +
            @('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'cmdb')
        )
    )
    if ($StartWorker) {
        Invoke-Docker (
            [string[]](
                $Instance.ComposeArguments +
                @('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'worker')
            )
        )
    }
}

$manifestFile = $null
$updateLock = $null
$environmentBackup = $null
$workerStopped = $false
$cmdbStopped = $false
$migrationStarted = $false
$instance = $null
$historyPath = $null
$historyRecord = $null
$updatePhase = 'validation'

try {
    $manifestFile = Get-ReleaseManifestFile
    $manifest = Read-ValidatedManifest $manifestFile
    $instance = Get-InstanceConfiguration

    if (
        (Get-ImageRepository $instance.CurrentImage).ToLowerInvariant() -cne
        $manifest.ImageName.ToLowerInvariant()
    ) {
        throw 'The target manifest uses a different container repository than this instance.'
    }

    Assert-DockerComposeV2
    Invoke-Docker ([string[]]($instance.ComposeArguments + @('config', '--quiet')))
    $topology = Get-ComposeRuntimeTopology $instance
    if ($topology.Worker.Exists -and -not $WorkerSplit) {
        throw (
            "Compose project $($topology.ProjectName) has a worker container. " +
            'Re-run with -WorkerSplit so every writer is included.'
        )
    }
    if ($WorkerSplit -and -not $topology.Worker.Exists) {
        throw (
            "WorkerSplit was selected, but Compose project $($topology.ProjectName) " +
            'has no worker container. Confirm the deployed topology before continuing.'
        )
    }
    $initialWorkerRunning = [bool]$topology.Worker.Running

    $runtime = $null
    try {
        $runtime = Get-RuntimeMetadata -Port $instance.Port -Endpoint health
    }
    catch {
        if (
            $Action -eq 'Apply' -and
            $instance.CurrentDigest -cne $manifest.ImageDigest
        ) {
            throw (
                'Apply requires valid current /api/health schema metadata before changing ' +
                'the image. Restore current service health, then re-run Check.'
            )
        }
        Write-Warning (
            'The current /api/health endpoint is unavailable; this read-only check ' +
            'cannot establish downgrade safety.'
        )
    }
    if ($runtime) {
        try {
            $currentExpectedSchema = [string](
                Get-RequiredProperty $runtime 'expectedSchemaVersion' 'Health response'
            )
            $schemaComparison = Compare-SchemaVersion `
                -Left $currentExpectedSchema `
                -Right $manifest.SchemaVersion
        }
        catch {
            if (
                $Action -eq 'Apply' -and
                $instance.CurrentDigest -cne $manifest.ImageDigest
            ) {
                throw (
                    'Apply requires a valid expectedSchemaVersion from current /api/health ' +
                    'before changing the image. Repair the current service, then re-run Check.'
                )
            }
            Write-Warning (
                'The current /api/health response has invalid schema metadata; this read-only ' +
                'check cannot establish downgrade safety.'
            )
            $runtime = $null
        }
        if ($runtime -and $schemaComparison -gt 0) {
            throw (
                "The running application expects schema $currentExpectedSchema, which is newer " +
                "than target schema $($manifest.SchemaVersion). Refusing a downgrade."
            )
        }
    }

    Write-Host ''
    Write-Host "IPT CMDB update $Action"
    Write-Host "Instance: $InstanceName ($($instance.Mode))"
    Write-Host "Manifest source: $($manifestFile.Source)"
    Write-Host "Manifest checksum verified: $($manifestFile.ChecksumVerified)"
    Write-Host "Manifest SHA-256: $($manifestFile.ManifestSha256)"
    Write-Host "Current image digest: $($instance.CurrentDigest)"
    if (-not $instance.DigestMetadataPresent) {
        Write-Warning (
            'CMDB_IMAGE_DIGEST is absent; the current digest was derived from the exact ' +
            'CMDB_IMAGE reference. Apply will add the metadata value.'
        )
    }
    if ($runtime) {
        Write-Host (
            "Current runtime: version $([string]$runtime.applicationVersion), " +
            "schema $([string]$runtime.expectedSchemaVersion)"
        )
    }
    Write-Host "Target version: $($manifest.Version)"
    Write-Host "Target image: $($manifest.ImageReference)"
    Write-Host "Target schema: $($manifest.SchemaVersion)"
    Write-Host "Database change: $($manifest.ChangeClassification)"
    Write-Host "Rollback policy: $($manifest.RollbackPolicy)"
    Write-Host (
        "Compose runtime: project $($topology.ProjectName), web " +
        "$($topology.Cmdb.Health), worker " +
        $(if ($topology.Worker.Exists) {
            if ($topology.Worker.Running) { 'running' } else { 'stopped' }
        }
        else {
            'not deployed'
        })
    )
    if ($manifestFile.ReleaseUrl) {
        Write-Host "Release: $($manifestFile.ReleaseUrl)"
    }

    if ($Action -eq 'Check') {
        if ($instance.CurrentDigest -ceq $manifest.ImageDigest) {
            $verifiedReady = Assert-InstalledTargetHealthy `
                -Instance $instance `
                -Manifest $manifest `
                -Topology $topology
            Write-Host (
                "Result: target is installed and healthy (version " +
                "$($verifiedReady.applicationVersion), schema $($verifiedReady.schemaVersion))."
            )
        }
        else {
            Write-Host 'Result: a different manifest image digest is available.'
        }
        if ($instance.Mode -eq 'appliance') {
            Write-Host 'Apply will stop application writes and create a verified final backup.'
        }
        else {
            Write-Host 'Apply requires RecoveryPointEvidence for the external PostgreSQL service.'
        }
        $applyCommand = (
            '.\scripts\Update-Cmdb.ps1 -Action Apply ' +
            "-InstanceName $(ConvertTo-PowerShellLiteral $InstanceName) " +
            "-InstanceRoot $(ConvertTo-PowerShellLiteral $instance.InstancePath) " +
            "-ApproveVersion $(ConvertTo-PowerShellLiteral $manifest.Version) " +
            "-ApproveManifestSha256 " +
            "$(ConvertTo-PowerShellLiteral $manifestFile.ManifestSha256)"
        )
        if ($ManifestPath) {
            $applyCommand += (
                " -ManifestPath $(ConvertTo-PowerShellLiteral $manifestFile.Path)"
            )
        }
        if ($WorkerSplit) {
            $applyCommand += ' -WorkerSplit'
        }
        if ($instance.Mode -eq 'external') {
            $applyCommand += (
                " -RecoveryPointEvidence '<non-secret-ticket-or-PITR-reference>'"
            )
        }
        Write-Host 'Reviewed Apply command:'
        Write-Host $applyCommand
        return
    }

    if (-not $manifestFile.ChecksumVerified) {
        throw (
            'Apply requires release-manifest.json to be verified by an adjacent ' +
            'SHA256SUMS file. No update has been made.'
        )
    }
    if ($ApproveVersion -cne $manifest.Version) {
        throw (
            "Apply requires -ApproveVersion '$($manifest.Version)' exactly. " +
            'No update has been made.'
        )
    }
    if ($ApproveManifestSha256 -cne $manifestFile.ManifestSha256) {
        throw (
            "Apply requires -ApproveManifestSha256 '$($manifestFile.ManifestSha256)' exactly. " +
            'Re-run Check and review the manifest before applying.'
        )
    }
    if (
        $instance.Mode -eq 'external' -and
        [string]::IsNullOrWhiteSpace($RecoveryPointEvidence)
    ) {
        throw 'External PostgreSQL apply requires non-empty RecoveryPointEvidence.'
    }
    if ($instance.Mode -eq 'external') {
        Assert-SafeRecoveryEvidence $RecoveryPointEvidence
    }
    if ($instance.CurrentDigest -ceq $manifest.ImageDigest) {
        $verifiedReady = Assert-InstalledTargetHealthy `
            -Instance $instance `
            -Manifest $manifest `
            -Topology $topology
        Write-Host (
            "The approved target is already installed and healthy: version " +
            "$($verifiedReady.applicationVersion), digest $($verifiedReady.imageDigest), " +
            "schema $($verifiedReady.schemaVersion)."
        )
        return
    }
    if (-not $topology.Cmdb.Exists -or -not $topology.Cmdb.Running) {
        throw (
            'Apply requires the current CMDB web container to be running so its topology ' +
            'and initial service state are known. Start or repair the current instance first.'
        )
    }

    $updateLock = Enter-UpdateLock $instance.InstancePath
    $historyDirectory = Join-Path $instance.InstancePath 'update-history'
    New-Item -ItemType Directory -Path $historyDirectory -Force | Out-Null
    $historyTimestamp = [DateTime]::UtcNow
    $historyPath = Join-Path (
        $historyDirectory
    ) (
        "$($historyTimestamp.ToString('yyyyMMddTHHmmssZ'))-$($manifest.Version)-" +
        "$([guid]::NewGuid().ToString('N').Substring(0, 8)).json"
    )
    $historyRecord = [ordered]@{
        recordVersion            = 1
        startedAt                = $historyTimestamp.ToString('O')
        completedAt              = $null
        status                   = 'in-progress'
        phase                    = 'pre-pull'
        instance                 = $InstanceName
        deploymentMode           = $instance.Mode
        workerSplit              = [bool]$WorkerSplit
        composeProject           = $topology.ProjectName
        cmdbInitiallyRunning     = [bool]$topology.Cmdb.Running
        cmdbInitialHealth        = $topology.Cmdb.Health
        workerTopologyDetected   = [bool]$topology.Worker.Exists
        workerInitiallyRunning   = $initialWorkerRunning
        workerInitialHealth      = $topology.Worker.Health
        fromImage                = $instance.CurrentImage
        fromDigest               = $instance.CurrentDigest
        targetVersion            = $manifest.Version
        targetCommit             = $manifest.Commit
        targetImage              = $manifest.ImageReference
        targetDigest             = $manifest.ImageDigest
        targetSchema             = $manifest.SchemaVersion
        schemaHistorySha256      = $manifest.SchemaHistorySha256
        databaseChange           = $manifest.ChangeClassification
        rollbackPolicy           = $manifest.RollbackPolicy
        rollbackBoundary         = $manifest.RollbackBoundary
        manifestSource           = $manifestFile.Source
        manifestSha256           = $manifestFile.ManifestSha256
        manifestChecksumVerified = [bool]$manifestFile.ChecksumVerified
        externalRecoveryEvidencePresent = $instance.Mode -eq 'external'
        externalRecoveryEvidenceSha256  = if ($instance.Mode -eq 'external') {
            Get-TextSha256 $RecoveryPointEvidence
        }
        else {
            $null
        }
        externalRecoveryEvidenceLength = if ($instance.Mode -eq 'external') {
            $RecoveryPointEvidence.Length
        }
        else {
            0
        }
        backupFile               = $null
        backupSha256             = $null
        targetImagePulled        = $false
        workerStopped            = $false
        cmdbStopped              = $false
        environmentUpdated       = $false
        migrationStarted         = $false
        migrationCompleted       = $false
        readinessVerified        = $false
        workerRunning            = $false
        targetWorkerStoppedAfterFailure = $null
        targetCmdbStoppedAfterFailure   = $null
        preMigrationRollback     = 'not-required'
        failureType              = $null
    }
    Write-UpdateHistory -Path $historyPath -Record $historyRecord
    Write-Host "Sanitized update history: $historyPath"

    # Pre-pull only the exact target image while the old application remains available.
    $updatePhase = 'pre-pull'
    Invoke-Docker ([string[]]@('pull', $manifest.ImageReference))
    $historyRecord.targetImagePulled = $true
    $historyRecord.phase = 'target-image-pulled'
    Write-UpdateHistory -Path $historyPath -Record $historyRecord

    if ($WorkerSplit -and $initialWorkerRunning) {
        $updatePhase = 'stop-worker'
        $workerStopped = $true
        Invoke-Docker ([string[]]($instance.ComposeArguments + @('stop', 'worker')))
        $historyRecord.workerStopped = $true
        $historyRecord.phase = 'worker-stopped'
        Write-UpdateHistory -Path $historyPath -Record $historyRecord
    }
    $updatePhase = 'stop-web'
    $cmdbStopped = $true
    Invoke-Docker ([string[]]($instance.ComposeArguments + @('stop', 'cmdb')))
    $historyRecord.cmdbStopped = $true
    $historyRecord.phase = 'web-stopped'
    Write-UpdateHistory -Path $historyPath -Record $historyRecord

    # The final recovery point is taken after every application writer is stopped.
    $updatePhase = 'recovery-point'
    if ($instance.Mode -eq 'appliance') {
        $backup = Invoke-VerifiedApplianceBackup $instance
        $historyRecord.backupFile = $backup.FileName
        $historyRecord.backupSha256 = $backup.Sha256
    }
    else {
        Write-Host 'External PostgreSQL recovery-point evidence was supplied and not printed.'
    }
    $historyRecord.phase = 'recovery-point-verified'
    Write-UpdateHistory -Path $historyPath -Record $historyRecord

    $updatePhase = 'environment-update'
    $updatedEnvironment = Set-EnvironmentValues `
        -Text $instance.Environment.Text `
        -ExistingValues $instance.Environment.Values `
        -Updates @{
            CMDB_IMAGE        = $manifest.ImageReference
            CMDB_IMAGE_DIGEST = $manifest.ImageDigest
        }
    $environmentBackup = Replace-EnvironmentAtomically `
        -Path $instance.EnvironmentPath `
        -Text $updatedEnvironment
    $historyRecord.environmentUpdated = $true
    $historyRecord.phase = 'environment-updated'
    Write-UpdateHistory -Path $historyPath -Record $historyRecord

    # Validate the target environment before touching the database.
    $updatePhase = 'target-compose-validation'
    Invoke-Docker ([string[]]($instance.ComposeArguments + @('config', '--quiet')))

    # From this point onward an automatic image rollback is prohibited. Even a failed
    # command may have reached PostgreSQL, and migrations are intentionally forward-only.
    $updatePhase = 'migration'
    $migrationStarted = $true
    $historyRecord.migrationStarted = $true
    $historyRecord.phase = $updatePhase
    Write-UpdateHistory -Path $historyPath -Record $historyRecord
    Invoke-Docker (
        [string[]](
            $instance.ComposeArguments +
            @(
                'run',
                '--no-deps',
                '--pull',
                'never',
                '--rm',
                'cmdb',
                'python',
                'scripts/migrate_postgres.py'
            )
        )
    )
    $historyRecord.migrationCompleted = $true
    $historyRecord.phase = 'migration-completed'
    Write-UpdateHistory -Path $historyPath -Record $historyRecord

    $updatePhase = 'start-web'
    Invoke-Docker (
        [string[]](
            $instance.ComposeArguments +
            @('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'cmdb')
        )
    )
    $updatePhase = 'readiness'
    $ready = Wait-ForExpectedReadiness `
        -Port $instance.Port `
        -Manifest $manifest `
        -TimeoutSeconds $WaitSeconds
    $historyRecord.readinessVerified = $true
    $historyRecord.phase = 'readiness-verified'
    Write-UpdateHistory -Path $historyPath -Record $historyRecord
    if ($WorkerSplit -and $initialWorkerRunning) {
        $updatePhase = 'start-worker'
        Invoke-Docker (
            [string[]](
                $instance.ComposeArguments +
                @('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'worker')
            )
        )
        $updatedWorkerState = Get-ComposeServiceRuntime `
            -ProjectName $topology.ProjectName `
            -Service worker
        if (-not $updatedWorkerState.Exists -or -not $updatedWorkerState.Running) {
            throw 'The target CMDB worker was not running after its requested restart.'
        }
        $historyRecord.workerRunning = $true
        $historyRecord.phase = 'worker-running'
        Write-UpdateHistory -Path $historyPath -Record $historyRecord
    }

    Remove-Item -LiteralPath $environmentBackup -Force -ErrorAction SilentlyContinue
    $environmentBackup = $null
    $historyRecord.status = 'succeeded'
    $historyRecord.phase = 'complete'
    $historyRecord.completedAt = [DateTime]::UtcNow.ToString('O')
    Write-UpdateHistory -Path $historyPath -Record $historyRecord
    Write-Host ''
    Write-Host (
        "Update complete: version $($ready.applicationVersion), " +
        "digest $($ready.imageDigest), schema $($ready.schemaVersion)."
    )
}
catch {
    $caughtError = $_
    if ($historyRecord -and $historyPath) {
        $historyRecord.status = 'failed'
        $historyRecord.phase = $updatePhase
        $historyRecord.failureType = $_.Exception.GetType().Name
        try {
            Write-UpdateHistory -Path $historyPath -Record $historyRecord
        }
        catch {
            Write-Warning 'The initial update failure could not be written to update history.'
        }
    }
    if (
        $Action -eq 'Apply' -and
        ($workerStopped -or $cmdbStopped) -and
        -not $migrationStarted
    ) {
        Write-Warning 'The update failed before migration began; restoring the previous environment.'
        $canRestartPrevious = $true
        if ($environmentBackup -and (Test-Path -LiteralPath $environmentBackup -PathType Leaf)) {
            try {
                Restore-EnvironmentAtomically `
                    -Path $instance.EnvironmentPath `
                    -BackupPath $environmentBackup
                $environmentBackup = $null
            }
            catch {
                $canRestartPrevious = $false
                Write-Warning (
                    'Automatic environment restoration failed. Keep the database stopped and ' +
                    'restore the protected previous environment file manually.'
                )
            }
        }
        if ($canRestartPrevious) {
            try {
                Start-InstanceServices `
                    -Instance $instance `
                    -StartWorker $initialWorkerRunning
                Write-Warning 'The previous application image was restarted.'
            }
            catch {
                $canRestartPrevious = $false
                Write-Warning 'The previous application image could not be restarted automatically.'
            }
        }
        if ($historyRecord -and $historyPath) {
            $historyRecord.preMigrationRollback = if ($canRestartPrevious) {
                'previous-image-restarted'
            }
            else {
                'manual-recovery-required'
            }
            $historyRecord.status = if ($canRestartPrevious) {
                'failed-pre-migration-recovered'
            }
            else {
                'failed-manual-recovery-required'
            }
        }
    }
    elseif ($Action -eq 'Apply' -and $migrationStarted) {
        if ($WorkerSplit) {
            try {
                Invoke-Docker ([string[]]($instance.ComposeArguments + @('stop', 'worker')))
                $historyRecord.targetWorkerStoppedAfterFailure = $true
            }
            catch {
                $historyRecord.targetWorkerStoppedAfterFailure = $false
                Write-Warning (
                    'The target worker could not be stopped automatically. Stop it before ' +
                    'reviewing or restoring the database.'
                )
            }
        }
        try {
            Invoke-Docker ([string[]]($instance.ComposeArguments + @('stop', 'cmdb')))
            $historyRecord.targetCmdbStoppedAfterFailure = $true
        }
        catch {
            $historyRecord.targetCmdbStoppedAfterFailure = $false
            Write-Warning (
                'The target web container could not be stopped automatically. Stop it before ' +
                'reviewing or restoring the database.'
            )
        }
        Write-Warning (
            'The update failed after migration began. The updater will not restore the old image. ' +
            'Target application writers were stopped where possible; follow the reviewed ' +
            'backup/PITR recovery procedure.'
        )
        if ($historyRecord -and $historyPath) {
            $historyRecord.status = 'failed-manual-recovery-required'
            $historyRecord.preMigrationRollback = 'prohibited-after-migration-start'
        }
    }
    if ($historyRecord -and $historyPath) {
        $historyRecord.completedAt = [DateTime]::UtcNow.ToString('O')
        try {
            Write-UpdateHistory -Path $historyPath -Record $historyRecord
        }
        catch {
            Write-Warning 'The final update failure state could not be written to update history.'
        }
    }
    throw $caughtError
}
finally {
    if ($updateLock) {
        $updateLock.Dispose()
    }
    if ($manifestFile -and $manifestFile.TemporaryPath) {
        Remove-Item -LiteralPath $manifestFile.TemporaryPath -Recurse -Force `
            -ErrorAction SilentlyContinue
    }
}
