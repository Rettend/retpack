#requires -Version 5.1
[CmdletBinding()]
param([string]$Packwiz = 'packwiz')

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force $dist | Out-Null

function Export-Pack([string]$Directory, [string]$Output) {
    Push-Location $Directory
    try {
        & $Packwiz refresh
        if ($LASTEXITCODE -ne 0) { throw 'packwiz refresh failed.' }
        & $Packwiz modrinth export --output $Output
        if ($LASTEXITCODE -ne 0) { throw 'packwiz export failed.' }
    } finally {
        Pop-Location
    }
}

Export-Pack (Join-Path $root 'pack') (Join-Path $dist 'retpack.mrpack')
$lab = Join-Path $dist ('lab-build-' + [guid]::NewGuid().ToString('N'))
try {
    Copy-Item (Join-Path $root 'pack') $lab -Recurse
    Copy-Item (Join-Path $root 'optional-lab\*.pw.toml') (Join-Path $lab 'mods')
    $manifest = Join-Path $lab 'pack.toml'
    $text = (Get-Content $manifest -Raw) -replace '(?m)^name = "retpack"$', 'name = "retpack lab"'
    [IO.File]::WriteAllText($manifest, $text, [Text.UTF8Encoding]::new($false))
    Export-Pack $lab (Join-Path $dist 'retpack-lab.mrpack')
} finally {
    if (Test-Path $lab) { Remove-Item $lab -Recurse -Force }
}

Write-Host "Built retpack.mrpack and retpack-lab.mrpack in $dist"
