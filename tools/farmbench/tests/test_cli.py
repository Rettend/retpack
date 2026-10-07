import json
from pathlib import Path

from farmbench.cli import main


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "bamboo_micro_v1.json"


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
