"""Read installed versions without copying personal game data into results."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path


def capture_environment(instance: str | Path) -> dict:
    root = Path(instance)
    manifest = root / ".retpack-installed.json"
    if not manifest.is_file():
        raise ValueError("Instance must contain .retpack-installed.json from the Retpack installer")
    installed = json.loads(manifest.read_text(encoding="utf-8-sig"))
    minecraft = installed.get("minecraft")
    fabric = installed.get("fabric")
    if not isinstance(minecraft, str) or not isinstance(fabric, str):
        raise ValueError("Installed manifest is missing Minecraft or Fabric versions")
    expected = {entry["path"]: entry for entry in installed.get("files", [])}
    mods = []
    mods_dir = root / "mods"
    if not mods_dir.is_dir():
        raise ValueError("Instance has no mods directory")
    for jar in sorted(mods_dir.glob("*.jar")):
        data = jar.read_bytes()
        entry = expected.get(f"mods/{jar.name}")
        matches = None
        if entry:
            algorithm = entry["hashFormat"].lower()
            if algorithm not in {"sha1", "sha256", "sha512"}:
                raise ValueError(f"Unsupported installed checksum for {jar.name}")
            matches = hashlib.new(algorithm, data).hexdigest() == entry["hash"].lower()
            if not matches:
                raise ValueError(f"Installed mod differs from its manifest: {jar.name}")
        with zipfile.ZipFile(jar) as archive:
            try:
                metadata = json.loads(archive.read("fabric.mod.json"))
            except KeyError:
                metadata = {}
        mods.append({
            "file": jar.name,
            "id": metadata.get("id"),
            "version": metadata.get("version"),
            "sha256": hashlib.sha256(data).hexdigest(),
            "matches_install_manifest": matches,
        })
    if not any(mod["id"] == "carpet" for mod in mods):
        raise ValueError("This benchmark requires Carpet in the selected instance")
    for relative in expected:
        if relative.startswith("mods/") and not (root / relative).is_file():
            raise ValueError(f"Managed mod is missing: {relative}")

    settings = {}
    options = root / "options.txt"
    if options.is_file():
        for line in options.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator and key in {"simulationDistance", "renderDistance", "maxFps"}:
                settings[key] = value
    config_hashes = {}
    for name in ("c2me.toml", "voxyworldgenv2.json", "voxy-config.json"):
        path = root / "config" / name
        if path.is_file():
            config_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    # A log can be from an earlier launch. Record this as evidence, not a claim
    # that the installed inventory is the set of currently loaded modules.
    log_evidence = None
    log = root / "logs" / "latest.log"
    if log.is_file():
        data = log.read_bytes()
        skipped = re.findall(r"Skipping disabled mod ([a-z0-9_.-]+)", data.decode("utf-8", errors="replace"))
        log_evidence = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "skipped_modules": sorted(set(skipped)),
            "note": "Latest saved log; may describe a previous launch.",
        }
    return {
        "source": "installed-files",
        "minecraft": minecraft,
        "fabric": fabric,
        "mods": mods,
        "saved_options": settings,
        "config_sha256": config_hashes,
        "log_evidence": log_evidence,
        "note": "Installed files and saved settings, not live world verification. Nested modules are represented by their containing JAR.",
    }
