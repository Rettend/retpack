import json
from pathlib import Path
import pytest

from farmbench.cli import main


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "bamboo_micro_v1.json"


@pytest.fixture(autouse=True)
def isolated_game_directory(tmp_path, monkeypatch):
    instance = tmp_path / "minecraft"
    instance.mkdir()
    monkeypatch.setattr("farmbench.cli.resolve_instance", lambda _: instance)
    return instance


def test_build_defaults_install_and_preserve_existing_design(tmp_path, monkeypatch, isolated_game_directory):
    monkeypatch.chdir(tmp_path)
    folder = isolated_game_directory / "schematics"
    folder.mkdir()
    old = folder / "bamboo_micro_v1-lab.litematic"
    old.write_bytes(b"previous user design")
    assert main(["build", str(EXAMPLE)]) == 0
    assert old.read_bytes() == b"previous user design"
    assert len(list(folder.glob("*.litematic"))) == 2
    assert len(list((tmp_path / "dist/farmbench").glob("*/build.json"))) == 1


def test_export_only_does_not_resolve_or_modify_instance(tmp_path, monkeypatch):
    def unexpected(_):
        raise AssertionError("Instance must not be resolved for export-only builds")
    monkeypatch.setattr("farmbench.cli.resolve_instance", unexpected)
    assert main(["build", str(EXAMPLE), "--no-install", "--out", str(tmp_path / "export")]) == 0


def environment():
    return {
        "source": "test-fixture",
        "minecraft": "26.2",
        "fabric": "0.19.5",
        "mods": [{"id": "carpet", "version": "26.2+v260616", "sha256": "a" * 64}],
        "saved_options": {"simulationDistance": "12"},
    }


def test_cli_build_plan_record_and_preserve_previous_results(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("farmbench.cli.capture_environment", lambda _: environment())
    build = tmp_path / "build"
    plan = tmp_path / "plan"
    output = tmp_path / "result.json"
    assert main(["validate", str(EXAMPLE)]) == 0
    assert main(["build", str(EXAMPLE), "--out", str(build)]) == 0
    assert list(build.glob("*.litematic"))
    assert main(["plan", str(EXAMPLE), "--build", str(build / "build.json"), "--instance", "test-fixture", "--out", str(plan)]) == 0
    assert "/tick sprint" in (plan / "benchmark.md").read_text(encoding="utf-8")
    args = ["record", str(plan / "plan.json"), "--items", "18", "--completed", "--placement-verified", "--out", str(output)]
    assert main(args) == 0
    original = output.read_bytes()
    assert "minecraft:bamboo" in original.decode()
    assert main(args) == 1
    assert output.read_bytes() == original
    assert "farmbench:" in capsys.readouterr().err


def test_plan_rejects_modified_export(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("farmbench.cli.capture_environment", lambda _: environment())
    build = tmp_path / "build"
    assert main(["build", str(EXAMPLE), "--out", str(build)]) == 0
    artifact = next(build.glob("*.litematic"))
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    output = tmp_path / "plan"
    assert main(["plan", str(EXAMPLE), "--build", str(build / "build.json"), "--instance", "unused", "--out", str(output)]) == 1
    assert "checksum" in capsys.readouterr().err
    assert not output.exists()


def test_saved_conditions_must_match_plan(tmp_path, monkeypatch, capsys):
    captured = environment()
    captured["saved_options"]["simulationDistance"] = "8"
    monkeypatch.setattr("farmbench.cli.capture_environment", lambda _: captured)
    build = tmp_path / "build"
    assert main(["build", str(EXAMPLE), "--out", str(build)]) == 0
    assert main(["plan", str(EXAMPLE), "--build", str(build / "build.json"), "--instance", "unused", "--out", str(tmp_path / "plan")]) == 1
    assert "simulation distance" in capsys.readouterr().err


def test_record_rejects_negative_count(tmp_path):
    # The parser accepts a signed integer, but record_result must reject it.
    # End-to-end coverage uses a valid generated plan rather than a mock record.
    from farmbench.benchmark import create_plan
    from farmbench.blueprint import load_blueprint
    from farmbench.compiler import compile_blueprint

    data = load_blueprint(EXAMPLE)
    build = compile_blueprint(data, tmp_path / "build")
    plan = create_plan(data, build, environment())
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    result = tmp_path / "result.json"
    assert main(["record", str(path), "--items", "-1", "--out", str(result)]) == 1
    assert not result.exists()


def test_job_defaults_to_selected_instance_and_warns_without_companion(tmp_path, isolated_game_directory, capsys):
    assert main(["job", str(EXAMPLE), str(EXAMPLE), "--id", "bamboo-ab"]) == 0
    folder = isolated_game_directory / "config/farmbench/jobs/bamboo-ab"
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    assert job["repeats"] == 3 and job["origin"] is None
    assert [design["counter"] for design in job["designs"]] == ["lime", "blue"]
    output = capsys.readouterr().out
    assert str(folder / "job.json") in output
    assert "companion not found" in output and "/farmbench start bamboo-ab" in output
    original = (folder / "job.json").read_bytes()
    assert main(["job", str(EXAMPLE), "--id", "bamboo-ab"]) == 1
    assert (folder / "job.json").read_bytes() == original
    assert "already exists" in capsys.readouterr().err
    assert not (isolated_game_directory / "schematics").exists()


def test_job_existing_requires_origin_and_supports_repeat_at(isolated_game_directory, capsys):
    assert main(["job", str(EXAMPLE), "--id", "missing", "--mode", "existing"]) == 1
    assert "explicit --origin" in capsys.readouterr().err
    assert main(["job", str(EXAMPLE), str(EXAMPLE), "--id", "existing", "--mode", "existing", "--origin", "100", "70", "-20", "--at", "100", "70", "-20", "--at", "120", "70", "-20", "--repeats", "4"]) == 0
    job = json.loads((isolated_game_directory / "config/farmbench/jobs/existing/job.json").read_text(encoding="utf-8"))
    assert job["mode"] == "existing" and job["repeats"] == 4
    assert [design["offset"] for design in job["designs"]] == [[0, 0, 0], [20, 0, 0]]


def test_job_recognizes_installed_companion(isolated_game_directory, capsys):
    import zipfile

    mods = isolated_game_directory / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "companion.jar", "w") as archive:
        archive.writestr("fabric.mod.json", '{"id":"farmbench","version":"fixture"}')
    assert main(["job", str(EXAMPLE), "--id", "installed"]) == 0
    output = capsys.readouterr().out
    assert "companion not found" not in output
    assert "/farmbench start installed" in output


def test_results_prints_json_and_saves_only_new_summary_files(tmp_path, capsys):
    from farmbench.jobs import JOB_FORMAT, RESULT_FORMAT

    result = {
        "format": RESULT_FORMAT, "status": "cancelled",
        "job": {"format": JOB_FORMAT, "id": "one", "repeats": 3, "run_ticks": 72000, "designs": [{"id": "a", "name": "bamboo", "counter": "lime", "item": "minecraft:bamboo"}]},
        "trials": [{"index": 1, "measured_ticks": 72000, "designs": [{"id": "a", "counter": "lime", "item": "minecraft:bamboo", "items": 12}]}],
        "errors": ["operator cancelled"],
    }
    source = tmp_path / "result.json"
    source.write_text(json.dumps(result), encoding="utf-8")
    output = tmp_path / "summaries/summary.json"
    assert main(["results", str(source), "--out", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == json.loads(output.read_text(encoding="utf-8"))
    assert summary["rate_kind"] == "diagnostic_only" and not summary["comparable"]
    assert summary["designs"][0]["n"] == 1
    assert summary["designs"][0]["sample_sd_items_per_hour"] is None
    previous = output.read_bytes()
    assert main(["results", str(source), "--out", str(output)]) == 1
    assert output.read_bytes() == previous
    assert "farmbench:" in capsys.readouterr().err


def test_results_invalid_format_is_a_friendly_cli_error(tmp_path, capsys):
    source = tmp_path / "not-a-result.json"
    source.write_text('{"format":"retpack-farm-benchmark-result-v1"}', encoding="utf-8")
    assert main(["results", str(source)]) == 1
    assert "Unsupported companion result format" in capsys.readouterr().err
