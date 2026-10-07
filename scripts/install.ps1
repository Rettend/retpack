<#
.SYNOPSIS
Installs Retpack into a standalone Minecraft game directory.
.DESCRIPTION
Reads the pinned packwiz index and metadata without requiring packwiz. Downloads
are checked before installation. Existing settings and other overrides are kept;
only files recorded in .retpack-installed.json may be replaced or removed.
The launcher must download Minecraft, libraries, and Java. This script does not
change launcher settings, accept the EULA, sign into an account, or launch a game.
.PARAMETER Destination
The game directory. Defaults to %APPDATA%\retpack\survival.
.PARAMETER Lab
Also installs the downloads pinned in optional-lab/*.pw.toml.
.PARAMETER WorldGen
Also installs the downloads pinned in optional-worldgen/*.pw.toml. This is opt-in
because save/quit hangs have been reported with WorldGen and C2ME.
.PARAMETER VerifyOnly
Checks installed, managed files against .retpack-installed.json without network
access or changing files. User settings and overrides are not managed files.
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
.EXAMPLE
.\scripts\install.ps1 -Destination 'D:\Minecraft\retpack' -Lab
.EXAMPLE
.\scripts\install.ps1 -Destination 'D:\Minecraft\retpack' -WorldGen
.EXAMPLE
.\scripts\install.ps1 -Destination 'D:\Minecraft\retpack' -VerifyOnly
#>
[CmdletBinding()]
param(
    [string]$Destination = $(if ($env:APPDATA) { Join-Path $env:APPDATA 'retpack\survival' }),
    [switch]$Lab,
    [switch]$WorldGen,
    [switch]$VerifyOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-RetpackRelativePath {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or [IO.Path]::IsPathRooted($Path)) {
        throw "Expected a relative file path, got '$Path'."
    }
    $relative = $Path.Replace('\', '/')
    foreach ($part in $relative.Split('/')) {
        if ($part -eq '' -or $part -eq '.' -or $part -eq '..' -or
            $part.EndsWith('.') -or $part.EndsWith(' ') -or
            $part.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
            throw "Unsafe relative file path '$Path'."
        }
    }
    return $relative
}

function Get-RetpackPath {
    param([string]$Root, [string]$RelativePath)

    $relative = Get-RetpackRelativePath $RelativePath
    $fullPath = [IO.Path]::GetFullPath((Join-Path $Root $relative.Replace('/', [IO.Path]::DirectorySeparatorChar)))
    $prefix = $Root.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path '$RelativePath' leaves '$Root'."
    }

    # Do not follow junctions or symbolic links inside the pack or game directory.
    $current = $Root
    foreach ($part in @('') + $relative.Split('/')) {
        if ($part -ne '') { $current = Join-Path $current $part }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing a symbolic link or junction at '$current'."
            }
            if ($current -ne $fullPath -and -not $item.PSIsContainer) {
                throw "Expected a directory at '$current'."
            }
        }
    }
    return $fullPath
}

function Get-RetpackHashFormat {
    param([string]$Format, [string]$Hash)

    $formatName = $Format.ToLowerInvariant()
    $length = switch ($formatName) {
        'sha1' { 40 }
        'sha256' { 64 }
        'sha512' { 128 }
        default { throw "Unsupported hash format '$Format'; use sha512, sha256, or sha1." }
    }
    if ($Hash -notmatch "\A[0-9a-fA-F]{$length}\z") {
        throw "Invalid $formatName checksum '$Hash'."
    }
    return $formatName
}

function Assert-RetpackHash {
    param([string]$Path, [string]$Format, [string]$Expected)

    $formatName = Get-RetpackHashFormat $Format $Expected
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing file '$Path'."
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm $formatName.ToUpperInvariant()).Hash
    if (-not $actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Checksum mismatch for '$Path'. Expected $formatName $Expected; got $actual. No unverified file will be installed."
    }
}

function Remove-RetpackTomlComment {
    param([string]$Line)

    $quote = [char]0
    $escaped = $false
    for ($i = 0; $i -lt $Line.Length; $i++) {
        $character = $Line[$i]
        if ($quote -eq [char]0) {
            if ($character -eq '#') { return $Line.Substring(0, $i).Trim() }
            if ($character -eq '"' -or $character -eq "'") { $quote = $character }
        } elseif ($quote -eq '"' -and $escaped) {
            $escaped = $false
        } elseif ($quote -eq '"' -and $character -eq '\') {
            $escaped = $true
        } elseif ($character -eq $quote) {
            $quote = [char]0
        }
    }
    return $Line.Trim()
}

function ConvertFrom-RetpackTomlScalar {
    param([string]$Value)

    if ($Value -eq 'true') { return $true }
    if ($Value -eq 'false') { return $false }
    if ($Value -match "\A'([^']*)'\z") { return $Matches[1] }
    if ($Value -match '\A"(?:[^"\\]|\\.)*"\z') {
        # Packwiz's paths, URLs, versions, and hashes use ordinary TOML strings.
        # JSON decoding also handles their escaped quotes, backslashes, and Unicode.
        try { return ConvertFrom-Json -InputObject $Value } catch { }
    }
    throw "Unsupported TOML value '$Value'. Expected a quoted string or boolean."
}

function Read-RetpackToml {
    param([string]$Path, [ValidateSet('pack', 'index', 'metadata')][string]$Kind)

    # Read only the scalar fields used by the installer, not unrelated update or
    # description fields. This deliberately is not a general-purpose TOML parser.
    $wanted = switch ($Kind) {
        'pack' { @{
            '' = @('pack-format')
            'index' = @('file', 'hash-format', 'hash')
            'versions' = @('minecraft', 'fabric')
        } }
        'index' { @{ '' = @('hash-format'); 'files' = @('file', 'hash', 'metafile', 'alias') } }
        'metadata' { @{
            '' = @('filename', 'side')
            'download' = @('url', 'hash-format', 'hash', 'mode')
        } }
    }
    $rootValues = @{}
    $sections = @{}
    $files = @()
    $section = ''
    $target = $rootValues
    $lineNumber = 0
    foreach ($rawLine in [IO.File]::ReadAllLines($Path)) {
        $lineNumber++
        $line = Remove-RetpackTomlComment $rawLine
        if ($line -eq '') { continue }
        if ($line -match '^\[\[\s*([a-zA-Z0-9_.-]+)\s*\]\]$') {
            $section = $Matches[1]
            $target = $null
            if ($Kind -eq 'index' -and $section -eq 'files') {
                $target = @{}
                $files += ,$target
            }
            continue
        }
        if ($line -match '^\[\s*([a-zA-Z0-9_.-]+)\s*\]$') {
            $section = $Matches[1]
            $target = $null
            if ($wanted.ContainsKey($section) -and $section -ne 'files') {
                if ($sections.ContainsKey($section)) { throw "Duplicate [$section] in '$Path'." }
                $target = @{}
                $sections[$section] = $target
            }
            continue
        }
        if ($null -eq $target -or -not $wanted.ContainsKey($section)) { continue }
        if ($line -notmatch '^([a-zA-Z0-9_-]+)\s*=\s*(.+)$') { continue }
        $key = $Matches[1]
        $value = $Matches[2]
        if ($wanted[$section] -notcontains $key) { continue }
        if ($target.ContainsKey($key)) { throw "Duplicate '$key' in '${Path}:$lineNumber'." }
        try { $target[$key] = ConvertFrom-RetpackTomlScalar $value } catch {
            throw "${Path}:$lineNumber`: $($_.Exception.Message)"
        }
    }
    return [pscustomobject]@{ Root = $rootValues; Sections = $sections; Files = $files }
}

function Get-RetpackRequiredString {
    param([hashtable]$Values, [string]$Key, [string]$Source)

    if ($null -eq $Values -or -not $Values.ContainsKey($Key) -or
        $Values[$Key] -isnot [string] -or [string]::IsNullOrWhiteSpace($Values[$Key])) {
        throw "Missing or invalid '$Key' in '$Source'."
    }
    return $Values[$Key]
}

function Read-RetpackManifest {
    param([string]$Root)

    $path = Get-RetpackPath $Root '.retpack-installed.json'
    if (-not (Test-Path -LiteralPath $path)) {
        return [pscustomobject]@{ Exists = $false; Files = @() }
    }
    try { $state = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($path)) } catch {
        throw "Cannot read '$path': $($_.Exception.Message)"
    }
    if ($null -eq $state -or $null -eq $state.PSObject.Properties['schemaVersion'] -or
        $state.schemaVersion -ne 1 -or $null -eq $state.PSObject.Properties['files'] -or
        $null -eq $state.files) {
        throw "Unsupported installed-file manifest '$path'."
    }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $files = @()
    foreach ($entry in @($state.files)) {
        foreach ($key in @('path', 'hashFormat', 'hash', 'kind')) {
            if ($null -eq $entry -or $null -eq $entry.PSObject.Properties[$key] -or $entry.$key -isnot [string]) {
                throw "Invalid file entry in '$path'."
            }
        }
        $relative = Get-RetpackRelativePath $entry.path
        if (($entry.kind -eq 'download' -and $relative -notmatch '^(mods|shaderpacks|resourcepacks)/') -or
            ($entry.kind -eq 'loader' -and $relative -notmatch '^versions/([^/]+)/\1\.json$') -or
            $entry.kind -notin @('download', 'loader') -or -not $seen.Add($relative)) {
            throw "Invalid or duplicate managed path '$relative' in '$path'."
        }
        $format = Get-RetpackHashFormat $entry.hashFormat $entry.hash
        $null = Get-RetpackPath $Root $relative
        $files += [pscustomobject]@{
            path = $relative
            hashFormat = $format
            hash = $entry.hash.ToLowerInvariant()
            kind = $entry.kind
        }
    }
    return [pscustomobject]@{ Exists = $true; Files = $files }
}

function Assert-RetpackManagedFiles {
    param([string]$Root, [object[]]$Files, [switch]$RequirePresent)

    foreach ($file in $Files) {
        $path = Get-RetpackPath $Root $file.path
        if ($RequirePresent -or (Test-Path -LiteralPath $path)) {
            Assert-RetpackHash $path $file.hashFormat $file.hash
        }
    }
}

function Assert-RetpackNoUnmanagedMods {
    param([string]$Root, [object[]]$Files)

    $known = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in $Files) { $null = $known.Add($file.path) }
    $modsPath = Get-RetpackPath $Root 'mods'
    if (-not (Test-Path -LiteralPath $modsPath)) { return }
    if (-not (Test-Path -LiteralPath $modsPath -PathType Container)) { throw "Expected a directory at '$modsPath'." }
    $directories = [Collections.Generic.Queue[string]]::new()
    $directories.Enqueue($modsPath)
    while ($directories.Count -gt 0) {
        foreach ($item in Get-ChildItem -LiteralPath $directories.Dequeue() -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing a symbolic link or junction at '$($item.FullName)'."
            }
            if ($item.PSIsContainer) {
                $directories.Enqueue($item.FullName)
            } elseif ($item.Extension -ieq '.jar') {
                $relative = $item.FullName.Substring($Root.TrimEnd('\', '/').Length + 1).Replace('\', '/')
                if (-not $known.Contains($relative)) {
                    throw "Unmanaged mod '$($item.FullName)'. Choose a fresh game directory or move this jar out before installing; it will not be overwritten or deleted."
                }
            }
        }
    }
}

function Get-RetpackDownload {
    param([string]$MetadataPath, [string]$InstallFolder)

    $metadata = Read-RetpackToml $MetadataPath 'metadata'
    $filename = Get-RetpackRequiredString $metadata.Root 'filename' $MetadataPath
    $filename = Get-RetpackRelativePath $filename
    if ($filename.Contains('/')) { throw "Download filename must not contain directories in '$MetadataPath'." }
    if ($metadata.Root.ContainsKey('side')) {
        if ($metadata.Root['side'] -notin @('client', 'both', 'server')) { throw "Invalid side in '$MetadataPath'." }
        if ($metadata.Root['side'] -eq 'server') { return }
    }
    $download = $metadata.Sections['download']
    $url = Get-RetpackRequiredString $download 'url' $MetadataPath
    $format = Get-RetpackRequiredString $download 'hash-format' $MetadataPath
    $hash = Get-RetpackRequiredString $download 'hash' $MetadataPath
    $format = Get-RetpackHashFormat $format $hash
    if ($download.ContainsKey('mode') -and $download['mode'] -ne 'url') {
        throw "Only direct URL downloads are supported in '$MetadataPath'."
    }
    $uri = $null
    if (-not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne 'https' -or $uri.UserInfo -ne '') {
        throw "Expected an absolute HTTPS download URL in '$MetadataPath'."
    }
    return [pscustomobject]@{
        path = Get-RetpackRelativePath "$InstallFolder/$filename"
        hashFormat = $format
        hash = $hash.ToLowerInvariant()
        kind = 'download'
        url = $url
        stagedPath = $null
    }
}

function Invoke-RetpackDownload {
    param([string]$Url, [string]$Output)

    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Output
    } catch {
        throw "Download failed for '$Url': $($_.Exception.Message)"
    }
}

if ([string]::IsNullOrWhiteSpace($Destination)) {
    throw 'Supply -Destination, or run with APPDATA set so the default game directory can be used.'
}
$destinationRoot = [IO.Path]::GetFullPath($Destination)
$previous = Read-RetpackManifest $destinationRoot
Assert-RetpackManagedFiles $destinationRoot $previous.Files -RequirePresent:$VerifyOnly
Assert-RetpackNoUnmanagedMods $destinationRoot $previous.Files
if ($VerifyOnly) {
    if (-not $previous.Exists) { throw "No .retpack-installed.json in '$destinationRoot'. Install Retpack first." }
    Write-Host "Verified $($previous.Files.Count) managed files in '$destinationRoot'. No files changed; user settings were not checked."
    return
}

$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$packRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot 'pack'))
$packPath = Get-RetpackPath $packRoot 'pack.toml'
if (-not (Test-Path -LiteralPath $packPath -PathType Leaf)) { throw "Missing '$packPath'. Run this script from a Retpack checkout containing the pack directory." }
$pack = Read-RetpackToml $packPath 'pack'
if ($pack.Root.ContainsKey('pack-format') -and $pack.Root['pack-format'] -ne 'packwiz:1.1.0') {
    throw "Unsupported pack format '$($pack.Root['pack-format'])'."
}
$minecraft = Get-RetpackRequiredString $pack.Sections['versions'] 'minecraft' $packPath
$fabric = Get-RetpackRequiredString $pack.Sections['versions'] 'fabric' $packPath
foreach ($version in @($minecraft, $fabric)) {
    if ($version -notmatch '\A[a-zA-Z0-9][a-zA-Z0-9._+-]*\z') { throw "Invalid pinned version '$version'." }
}
$indexValues = $pack.Sections['index']
$indexFile = Get-RetpackRequiredString $indexValues 'file' $packPath
$indexPath = Get-RetpackPath $packRoot $indexFile
$indexFormat = Get-RetpackRequiredString $indexValues 'hash-format' $packPath
$indexHash = Get-RetpackRequiredString $indexValues 'hash' $packPath
Assert-RetpackHash $indexPath $indexFormat $indexHash
$index = Read-RetpackToml $indexPath 'index'
$entryFormat = Get-RetpackRequiredString $index.Root 'hash-format' $indexPath
$downloads = @()
$overrides = @()
$indexedPaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($entry in $index.Files) {
    $relative = Get-RetpackRelativePath (Get-RetpackRequiredString $entry 'file' $indexPath)
    if (-not $indexedPaths.Add($relative)) { throw "Duplicate file '$relative' in '$indexPath'." }
    if ($entry.ContainsKey('alias') -and $entry['alias'] -ne '') { throw "Index aliases are not supported: '$relative'." }
    $source = Get-RetpackPath $packRoot $relative
    $hash = Get-RetpackRequiredString $entry 'hash' $indexPath
    Assert-RetpackHash $source $entryFormat $hash
    if ($entry.ContainsKey('metafile') -and $entry['metafile'] -isnot [bool]) { throw "Invalid metafile flag for '$relative'." }
    if ($entry.ContainsKey('metafile') -and $entry['metafile']) {
        if ($relative -notmatch '^(mods|shaderpacks|resourcepacks)/.+\.pw\.toml$') { throw "Unsupported metadata location '$relative'." }
        $folder = $relative.Substring(0, $relative.LastIndexOf('/'))
        $download = Get-RetpackDownload $source $folder
        if ($null -ne $download) { $downloads += $download }
    } else {
        if ($relative -match '\.pw\.toml$') { throw "Metadata '$relative' must be marked metafile = true in the index." }
        if ($relative -in @('pack.toml', $indexFile.Replace('\', '/'))) { continue }
        if ($relative -eq '.retpack-installed.json' -or $relative -match '^versions/' -or $relative -match '\.jar$') {
            throw "Reserved or embedded-jar override '$relative'. Use pinned download metadata for jars."
        }
        $overrides += [pscustomobject]@{ path = $relative; source = $source; hashFormat = $entryFormat; hash = $hash }
    }
}
if ($Lab) {
    $labRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot 'optional-lab'))
    $null = Get-RetpackPath $labRoot 'check'
    if (-not (Test-Path -LiteralPath $labRoot -PathType Container)) { throw "Missing lab metadata directory '$labRoot'." }
    $labFiles = @(Get-ChildItem -LiteralPath $labRoot -Filter '*.pw.toml' -File | Sort-Object Name)
    if ($labFiles.Count -eq 0) { throw "No pinned lab metadata in '$labRoot'." }
    foreach ($file in $labFiles) {
        $source = Get-RetpackPath $labRoot $file.Name
        $download = Get-RetpackDownload $source 'mods'
        if ($null -ne $download) { $downloads += $download }
    }
}
if ($WorldGen) {
    $worldGenRoot = [IO.Path]::GetFullPath((Join-Path $repositoryRoot 'optional-worldgen'))
    $null = Get-RetpackPath $worldGenRoot 'check'
    if (-not (Test-Path -LiteralPath $worldGenRoot -PathType Container)) { throw "Missing WorldGen metadata directory '$worldGenRoot'." }
    $worldGenFiles = @(Get-ChildItem -LiteralPath $worldGenRoot -Filter '*.pw.toml' -File | Sort-Object Name)
    if ($worldGenFiles.Count -eq 0) { throw "No pinned WorldGen metadata in '$worldGenRoot'." }
    foreach ($file in $worldGenFiles) {
        $source = Get-RetpackPath $worldGenRoot $file.Name
        $download = Get-RetpackDownload $source 'mods'
        if ($null -ne $download) { $downloads += $download }
    }
}

$knownFiles = @{}
foreach ($file in $previous.Files) { $knownFiles[$file.path] = $file }
$plannedPaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($file in @($downloads) + @($overrides)) {
    if (-not $plannedPaths.Add($file.path)) { throw "Multiple pack files would install to '$($file.path)'." }
    $target = Get-RetpackPath $destinationRoot $file.path
    if (Test-Path -LiteralPath $target -PathType Container) { throw "Expected a file, not a directory at '$target'." }
    if ($file.PSObject.Properties['kind'] -and (Test-Path -LiteralPath $target) -and -not $knownFiles.ContainsKey($file.path)) {
        throw "Unmanaged download '$target'. Choose a fresh game directory or move this file out; it will not be overwritten."
    }
}

$stage = Join-Path ([IO.Path]::GetTempPath()) ('retpack-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $stage
try {
    # Windows PowerShell 5.1 may otherwise negotiate an obsolete TLS version.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $number = 0
    foreach ($download in $downloads) {
        $target = Get-RetpackPath $destinationRoot $download.path
        if ($knownFiles.ContainsKey($download.path) -and (Test-Path -LiteralPath $target -PathType Leaf)) {
            $actual = (Get-FileHash -LiteralPath $target -Algorithm $download.hashFormat.ToUpperInvariant()).Hash
            if ($actual.Equals($download.hash, [StringComparison]::OrdinalIgnoreCase)) { continue }
        }
        $number++
        $download.stagedPath = Join-Path $stage "$number.download"
        Write-Host "Downloading $($download.path)..."
        Invoke-RetpackDownload $download.url $download.stagedPath
        Assert-RetpackHash $download.stagedPath $download.hashFormat $download.hash
    }

    $profileUrl = 'https://meta.fabricmc.net/v2/versions/loader/{0}/{1}/profile/json' -f [Uri]::EscapeDataString($minecraft), [Uri]::EscapeDataString($fabric)
    $profileStage = Join-Path $stage 'fabric-profile.json'
    Invoke-RetpackDownload $profileUrl $profileStage
    try { $profile = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($profileStage)) } catch { throw 'Fabric returned an invalid launcher profile.' }
    if ($null -eq $profile -or $null -eq $profile.PSObject.Properties['id'] -or $profile.id -isnot [string] -or
        $null -eq $profile.PSObject.Properties['inheritsFrom'] -or $profile.inheritsFrom -ne $minecraft -or
        $null -eq $profile.PSObject.Properties['libraries'] -or
        @($profile.libraries | Where-Object { $_.name -eq "net.fabricmc:fabric-loader:$fabric" }).Count -eq 0) {
        throw 'Fabric launcher profile does not match the pinned Minecraft and Fabric versions.'
    }
    $profileId = Get-RetpackRelativePath $profile.id
    if ($profileId.Contains('/')) { throw 'Fabric returned an unsafe launcher version id.' }
    $profileRelative = "versions/$profileId/$profileId.json"
    if (-not $plannedPaths.Add($profileRelative)) { throw "Duplicate launcher profile path '$profileRelative'." }
    $profileTarget = Get-RetpackPath $destinationRoot $profileRelative
    if ((Test-Path -LiteralPath $profileTarget) -and -not $knownFiles.ContainsKey($profileRelative)) {
        throw "Unmanaged launcher profile '$profileTarget'; it will not be overwritten."
    }
    $downloads += [pscustomobject]@{
        path = $profileRelative
        hashFormat = 'sha256'
        hash = (Get-FileHash -LiteralPath $profileStage -Algorithm SHA256).Hash.ToLowerInvariant()
        kind = 'loader'
        url = $profileUrl
        stagedPath = $profileStage
    }

    # Recheck after downloading, before replacing or removing any managed file.
    Assert-RetpackManagedFiles $destinationRoot $previous.Files
    Assert-RetpackNoUnmanagedMods $destinationRoot $previous.Files
    foreach ($download in $downloads) {
        $target = Get-RetpackPath $destinationRoot $download.path
        if ((Test-Path -LiteralPath $target) -and -not $knownFiles.ContainsKey($download.path)) { throw "Unmanaged file appeared at '$target'." }
        if ($null -ne $download.stagedPath) { Assert-RetpackHash $download.stagedPath $download.hashFormat $download.hash }
    }
    foreach ($override in $overrides) { Assert-RetpackHash $override.source $override.hashFormat $override.hash }

    foreach ($download in $downloads) {
        $target = Get-RetpackPath $destinationRoot $download.path
        if ($null -ne $download.stagedPath) {
            $null = New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force
            Copy-Item -LiteralPath $download.stagedPath -Destination $target -Force
        }
    }
    foreach ($override in $overrides) {
        $target = Get-RetpackPath $destinationRoot $override.path
        if (-not (Test-Path -LiteralPath $target)) {
            $null = New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force
            Copy-Item -LiteralPath $override.source -Destination $target
        }
    }
    $nextFiles = @($downloads | ForEach-Object {
        [pscustomobject]@{ path = $_.path; hashFormat = $_.hashFormat; hash = $_.hash; kind = $_.kind }
    })
    $nextPaths = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in $nextFiles) { $null = $nextPaths.Add($file.path) }
    foreach ($file in $previous.Files) {
        if (-not $nextPaths.Contains($file.path) -and -not $plannedPaths.Contains($file.path)) {
            $target = Get-RetpackPath $destinationRoot $file.path
            if (Test-Path -LiteralPath $target) {
                Assert-RetpackHash $target $file.hashFormat $file.hash
                Remove-Item -LiteralPath $target -Force
            }
        }
    }
    Assert-RetpackManagedFiles $destinationRoot $nextFiles -RequirePresent
    $manifestPath = Get-RetpackPath $destinationRoot '.retpack-installed.json'
    $manifest = [pscustomobject]@{
        schemaVersion = 1
        minecraft = $minecraft
        fabric = $fabric
        lab = [bool]$Lab
        worldGen = [bool]$WorldGen
        files = $nextFiles
    }
    $manifestStage = Join-Path $stage 'installed.json'
    [IO.File]::WriteAllText($manifestStage, ($manifest | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Copy-Item -LiteralPath $manifestStage -Destination $manifestPath -Force
    Write-Host "Installed Retpack in '$destinationRoot'. Existing settings and saves were kept."
    Write-Host "In Legacy Launcher, set this as the game directory and select '$profileId'. Let the launcher download Minecraft, libraries, and Java."
} finally {
    # Only the unique staging directory created by this invocation is removed.
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
