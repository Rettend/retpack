#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Packwiz = 'packwiz',
    [switch]$WorldGen
)

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

if ($WorldGen) {
    $worldGenBuild = Join-Path $dist ('worldgen-build-' + [guid]::NewGuid().ToString('N'))
    try {
        Copy-Item (Join-Path $root 'pack') $worldGenBuild -Recurse
        Copy-Item (Join-Path $root 'optional-worldgen\*.pw.toml') (Join-Path $worldGenBuild 'mods')
        $config = Join-Path $worldGenBuild 'config'
        New-Item -ItemType Directory -Force $config | Out-Null
        Copy-Item (Join-Path $root 'optional-worldgen\config\voxyworldgenv2.json') $config
        Copy-Item (Join-Path $root 'optional-worldgen\java-arguments.txt') $worldGenBuild
        Copy-Item (Join-Path $root 'optional-worldgen\launcher-setup.txt') $worldGenBuild
        $manifest = Join-Path $worldGenBuild 'pack.toml'
        $text = (Get-Content $manifest -Raw) -replace '(?m)^name = "retpack"$', 'name = "retpack worldgen"'
        [IO.File]::WriteAllText($manifest, $text, [Text.UTF8Encoding]::new($false))
        Export-Pack $worldGenBuild (Join-Path $dist 'retpack-worldgen.mrpack')
    } finally {
        if (Test-Path $worldGenBuild) { Remove-Item $worldGenBuild -Recurse -Force }
    }
    Write-Host "Built retpack-worldgen.mrpack in $dist. Before playing, manually add the Java argument from optional-worldgen/java-arguments.txt to this instance's launcher settings; importing the pack does not apply it."
}

Write-Host "Built retpack.mrpack and retpack-lab.mrpack in $dist"
