[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$expected = '7a9ce74cff467ca1bf60a4fcd9f05185acceda4d0f382434d393e17864262c5d'
$url = 'https://raw.githubusercontent.com/FabricMC/fabric-example-mod/1ce1c77a77ddbf7587e0d171ea369051668a67a6/gradle/wrapper/gradle-wrapper.jar'
$destination = [IO.Path]::Combine($PSScriptRoot, 'gradle/wrapper/gradle-wrapper.jar')

function Get-Sha256([string]$path) {
    $stream = [IO.File]::OpenRead($path)
    $digest = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($digest.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $digest.Dispose() }
}

if ([IO.File]::Exists($destination)) {
    if ((Get-Sha256 $destination) -ne $expected) {
        throw 'Gradle wrapper SHA256 mismatch. Remove only gradle/wrapper/gradle-wrapper.jar and retry.'
    }
    exit 0
}

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$temporary = "$destination.$([Guid]::NewGuid().ToString('N')).tmp"
try {
    [Console]::WriteLine('Downloading checksum-pinned Gradle wrapper.')
    $client = [Net.WebClient]::new()
    try { $client.DownloadFile($url, $temporary) } finally { $client.Dispose() }
    if ((Get-Sha256 $temporary) -ne $expected) {
        throw 'Downloaded Gradle wrapper SHA256 mismatch.'
    }
    [IO.File]::Move($temporary, $destination)
} finally {
    if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
}
