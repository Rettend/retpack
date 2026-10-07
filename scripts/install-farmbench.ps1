#requires -Version 5.1
<#
.SYNOPSIS
Installs a locally built Farmbench companion into a closed Minecraft instance.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Jar,
    [Parameter(Mandatory = $true)][string]$Destination
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Read-ModId([string]$Path) {
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $archive.GetEntry('fabric.mod.json')
        if ($null -eq $entry) { return $null }
        $reader = [IO.StreamReader]::new($entry.Open())
        try { return ($reader.ReadToEnd() | ConvertFrom-Json).id } finally { $reader.Dispose() }
    } finally { $archive.Dispose() }
}

$source = (Resolve-Path -LiteralPath $Jar).Path
$root = (Resolve-Path -LiteralPath $Destination).Path
if ((Read-ModId $source) -ne 'farmbench') { throw 'The supplied JAR is not the Farmbench companion.' }
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^javaw?\.exe$' -and $_.CommandLine -match 'KnotClient|net.minecraft.client.main.Main'
})
if ($running.Count -gt 0) { throw 'Close Minecraft before installing the companion.' }

$mods = Join-Path $root 'mods'
$null = New-Item -ItemType Directory -Path $mods -Force
$target = Join-Path $mods 'farmbench.jar'
foreach ($file in Get-ChildItem -LiteralPath $mods -Filter '*.jar' -File) {
    if ($file.FullName -ne $target -and (Read-ModId $file.FullName) -eq 'farmbench') {
        throw "Another companion JAR exists: '$($file.FullName)'. Move it out of mods before installing."
    }
}
if ((Test-Path $target) -and (Read-ModId $target) -ne 'farmbench') {
    throw 'mods/farmbench.jar belongs to a different mod; it will not be replaced.'
}
$hash = (Get-FileHash $source -Algorithm SHA256).Hash.ToLowerInvariant()
$manifestPath = Join-Path $root '.retpack-installed.json'
$manifest = $null
if (Test-Path $manifestPath) {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1) { throw 'Unsupported Retpack installed-file manifest.' }
    $manifest.files = @($manifest.files | Where-Object { $_.path -ne 'mods/farmbench.jar' }) + @(
        [pscustomobject]@{ path = 'mods/farmbench.jar'; hashFormat = 'sha256'; hash = $hash; kind = 'download' }
    )
}
$backup = Join-Path $root ('farmbench-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fffffff'))
$null = New-Item -ItemType Directory -Path $backup
$hadJar = Test-Path $target
if ($hadJar) { Copy-Item -LiteralPath $target -Destination $backup }
if ($null -ne $manifest) { Copy-Item -LiteralPath $manifestPath -Destination $backup }
try {
    if ($source -ne $target) { Copy-Item -LiteralPath $source -Destination $target -Force }
    if ((Get-FileHash $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hash) { throw 'Installed JAR checksum mismatch.' }
    if ($null -ne $manifest) {
        [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    }
} catch {
    if ($hadJar) { Copy-Item (Join-Path $backup 'farmbench.jar') $target -Force }
    elseif (Test-Path $target) { Remove-Item -LiteralPath $target }
    if ($null -ne $manifest) { Copy-Item (Join-Path $backup '.retpack-installed.json') $manifestPath -Force }
    throw
}
Write-Host "Installed Farmbench in '$root'. Backup: $backup"
Write-Host 'Restart Minecraft to load it. Reinstall the companion after a standard Retpack update removes it.'
