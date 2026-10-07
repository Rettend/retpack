import hashlib
import json
import zipfile

import pytest

from farmbench.environment import capture_environment, resolve_instance


def test_resolve_legacy_escaped_game_directory(tmp_path, monkeypatch):
    instance = tmp_path / "my instance"
    instance.mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = tmp_path / ".tlauncher/legacy/Minecraft/tl.properties"
    settings.parent.mkdir(parents=True)
    escaped = str(instance).replace("\\", "\\\\").replace(":", "\\:")
    settings.write_text(f"minecraft.gamedir={escaped}\n", encoding="utf-8")
    assert resolve_instance() == instance.resolve()
    assert resolve_instance(instance) == instance.resolve()


def make_instance(tmp_path):
    mods = tmp_path / "mods"
    mods.mkdir()
    jar = mods / "carpet-test.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "carpet", "version": "test"}))
    (tmp_path / ".retpack-installed.json").write_text(json.dumps({
        "minecraft": "26.2", "fabric": "0.19.5",
        "files": [{"path": "mods/carpet-test.jar", "hashFormat": "sha512", "hash": hashlib.sha512(jar.read_bytes()).hexdigest()}],
    }))
    return jar


def test_capture_records_installed_provenance_without_personal_options(tmp_path):
    make_instance(tmp_path)
    (tmp_path / "options.txt").write_text("simulationDistance:12\nlastServer:private.example\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "latest.log").write_text("[main/INFO]: Skipping disabled mod c2me-server-utils\nprivate player chat\n")
    result = capture_environment(tmp_path)
    assert result["mods"][0]["version"] == "test"
    assert result["mods"][0]["matches_install_manifest"] is True
    assert result["saved_options"] == {"simulationDistance": "12"}
    assert result["log_evidence"]["skipped_modules"] == ["c2me-server-utils"]
    assert "private" not in json.dumps(result)


def test_modified_installed_jar_cannot_be_reported_as_pinned(tmp_path):
    jar = make_instance(tmp_path)
    with jar.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="differs"):
        capture_environment(tmp_path)


def test_missing_carpet_is_actionable(tmp_path):
    make_instance(tmp_path).unlink()
    with pytest.raises(ValueError, match="requires Carpet"):
        capture_environment(tmp_path)
