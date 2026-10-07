#!/bin/sh
set -eu
home=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destination="$home/gradle/wrapper/gradle-wrapper.jar"
expected=7a9ce74cff467ca1bf60a4fcd9f05185acceda4d0f382434d393e17864262c5d
url=https://raw.githubusercontent.com/FabricMC/fabric-example-mod/1ce1c77a77ddbf7587e0d171ea369051668a67a6/gradle/wrapper/gradle-wrapper.jar

checksum() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        echo 'Install sha256sum or shasum to verify the Gradle wrapper.' >&2
        exit 1
    fi
}

if [ -f "$destination" ]; then
    [ "$(checksum "$destination")" = "$expected" ] || { echo 'Gradle wrapper SHA256 mismatch.' >&2; exit 1; }
    exit 0
fi

temporary="$destination.$$.tmp"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
echo 'Downloading checksum-pinned Gradle wrapper.' >&2
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 "$url" --output "$temporary"
[ "$(checksum "$temporary")" = "$expected" ] || { echo 'Downloaded Gradle wrapper SHA256 mismatch.' >&2; exit 1; }
mv "$temporary" "$destination"
