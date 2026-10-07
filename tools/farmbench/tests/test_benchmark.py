from copy import deepcopy
import json

import pytest

from farmbench.benchmark import create_plan, record_result, render_plan
from farmbench.blueprint import blueprint_hash, validate_blueprint


@pytest.fixture
def blueprint():
    return validate_blueprint({
        "format": "retpack-farm-blueprint-v1",
        "name": "bamboo_test",
        "minecraft": "26.2",
        "data_version": 4903,
        "bounds": {"min": [-1, 0, -1], "size": [3, 5, 4]},
        "blocks": [
            {"pos": [0, 0, 0], "state": {"Name": "minecraft:hopper", "Properties": {"facing": "east", "enabled": "true"}}, "role": "farm"},
            {"pos": [1, 0, 0], "state": {"Name": "minecraft:lime_wool"}, "role": "test"},
        ],
        "ports": [{
            "name": "items_out", "kind": "hopper", "position": [0, 0, 0], "direction": "east", "counter": "lime",
            "survival_output": {"Name": "minecraft:barrel", "Properties": {"facing": "up", "open": "false"}},
        }],
        "conditions": {"random_tick_speed": 3, "simulation_distance": 12, "difficulty": "normal", "dimension": "minecraft:overworld"},
        "benchmark": {"counter": "lime", "item": "minecraft:bamboo", "warmup_ticks": 1200, "run_ticks": 12000, "drain_ticks": 200},
        "notes": ["Keep the player nearby and the plant lit."],
    })


@pytest.fixture
def manifest(blueprint):
    return {
        "blueprint_sha256": blueprint_hash(blueprint),
        "variant": "lab",
        "minecraft": blueprint["minecraft"],
        "data_version": blueprint["data_version"],
        "artifact": {"path": "bamboo_test-lab.litematic", "sha256": "a" * 64},
    }


@pytest.fixture
def environment():
    return {
        "source": "installed-files",
        "minecraft": "26.2",
        "fabric": "0.19.5",
        "mods": [{"id": "carpet", "version": "26.2+v260616", "sha256": "b" * 64}],
        "saved_options": {"simulationDistance": "12"},
    }


@pytest.fixture
def plan(blueprint, manifest, environment):
    return create_plan(blueprint, manifest, environment)


def observations(**changes):
    return {"items": 4, "completed": True, "placement_verified": True, "notes": "Four items observed.", **changes}


def test_plan_records_identities_and_declared_not_measured_environment(plan, blueprint, manifest, environment):
    assert plan["format"] == "retpack-farm-benchmark-plan-v1"
    assert plan["blueprint_sha256"] == blueprint_hash(blueprint)
    assert plan["artifact"] == manifest["artifact"]
    assert plan["environment"]["declared"] == environment
    assert plan["environment"]["measured"] == {}
    assert plan["conditions"] == blueprint["conditions"]
    assert plan["command_evidence"]["method"] == "local_mapped_jar_bytecode"
    assert "restores setFrozen(previousIsFrozen)" in " ".join(plan["command_evidence"]["findings"])
    assert len(plan["plan_sha256"]) == 64
    assert json.loads(json.dumps(plan)) == plan


def test_clean_warmup_reset_run_and_growth_disabled_drain_order(plan):
    commands = [command for step in plan["steps"] for command in step["commands"]]
    assert commands == [
        "/tick freeze", "/tick query", "/carpet hopperCounters true", "/difficulty normal",
        "/gamerule random_tick_speed 3", "/tick sprint 1199", "/tick query",
        "/gamerule random_tick_speed 0", "/tick sprint 199", "/tick query",
        "/counter lime reset", "/counter lime", "/gamerule random_tick_speed 3",
        "/tick sprint 11999", "/tick query", "/gamerule random_tick_speed 0",
        "/tick sprint 199", "/tick query", "/counter lime",
    ]
    assert not any("warp" in command or "unfreeze" in command or "realtime" in command for command in commands)
    assert plan["methodology"]["rate_denominator"] == "growth_enabled_run_ticks_only"
    assert plan["methodology"]["frozen_sprint_startup_ticks"] == 1
    assert plan["methodology"]["warmup_drain_ticks"] == plan["benchmark"]["drain_ticks"]


def test_render_explains_manual_completion_transport_and_denominator(plan):
    rendered = render_plan(plan)
    for text in ("Wait for every sprint", "restores frozen state", "pollute", "placement", "bonemeal",
                 "72000", "Warm-up and drain ticks are excluded", "completed=false", "actual_ticks",
                 "not Carpet's hourly estimate", "does not measure loss percentage", "one startup simulation tick", "SHA256"):
        assert text in rendered
    assert "randomTickSpeed" not in rendered
    assert rendered.endswith("\n")


def test_four_items_in_ten_simulated_minutes_are_24_per_hour(plan):
    result = record_result(plan, observations())
    assert result["status"] == "completed"
    assert result["comparable"] is True
    assert result["ticks"] == 12000
    assert result["items_per_tick"] == pytest.approx(4 / 12000)
    assert result["items_per_hour"] == 24
    assert result["tick_source"] == "planned_ticks_attested_by_completed"
    assert result["methodology"]["loss_percentage"] == "not_measured"


@pytest.mark.parametrize("actual", [{"fabric": "different"}, {"mods": []}])
def test_reported_runtime_version_change_marks_run_noncomparable(plan, actual):
    result = record_result(plan, observations(actual_environment=actual))
    assert result["comparable"] is False
    assert any(flag.endswith("differs_from_plan") for flag in result["flags"])
    assert result["observations"]["notes"] == "Four items observed."
    assert "loss_rate" not in result


def test_zero_output_is_valid_and_does_not_infer_loss(plan):
    result = record_result(plan, observations(items=0))
    assert result["comparable"] is True
    assert result["items_per_hour"] == 0


def test_actual_ticks_not_wallclock_supply_the_denominator(plan):
    result = record_result(plan, observations(actual_ticks=6000, wallclock_seconds=2))
    assert result["ticks"] == 6000
    assert result["items_per_hour"] == 48
    assert result["status"] == "noncomparable"
    assert result["comparable"] is False
    assert result["rate_kind"] == "diagnostic_only"
    assert result["flags"] == ["run_ticks_differ_from_plan"]


@pytest.mark.parametrize("completed,verified,status", [(False, True, "incomplete"), (True, False, "unverified"), (False, False, "incomplete")])
def test_incomplete_or_unverified_never_successful_comparable(plan, completed, verified, status):
    result = record_result(plan, observations(completed=completed, placement_verified=verified))
    assert result["status"] == status
    assert result["comparable"] is False
    assert result["rate_kind"] != "comparable"
    assert bool(result["flags"])
    if not completed:
        assert result["ticks"] is None
        assert result["items_per_hour"] is None


def test_incomplete_actual_ticks_allow_only_diagnostic_rate(plan):
    result = record_result(plan, observations(completed=False, actual_ticks=6000))
    assert result["items_per_hour"] == 48
    assert result["rate_kind"] == "diagnostic_only"
    assert result["comparable"] is False
    empty = record_result(plan, observations(completed=False, items=0, actual_ticks=0))
    assert empty["items_per_hour"] is None


def test_missing_notes_are_allowed(plan):
    observed = observations()
    del observed["notes"]
    assert record_result(plan, observed)["observations"]["notes"] == ""


def test_measured_environment_is_separate_and_mismatches_flagged(plan, environment):
    result = record_result(plan, observations(
        actual_environment={"minecraft": "26.2", "conditions": {"random_tick_speed": 4}},
        environment_notes="Checked live settings.",
    ))
    assert result["environment"]["declared"] == environment
    assert result["environment"]["measured"]["conditions"]["random_tick_speed"] == 4
    assert result["environment"]["measurement_source"] == "operator_reported"
    assert result["environment"]["notes"] == "Checked live settings."
    assert result["flags"] == ["actual_random_tick_speed_differs_from_plan"]
    assert result["comparable"] is False


def test_inputs_and_results_do_not_share_mutable_data(plan, blueprint, manifest, environment):
    snapshots = deepcopy((blueprint, manifest, environment, plan))
    observed = observations(actual_environment={"minecraft": "26.2"})
    saved = deepcopy(observed)
    result = record_result(plan, observed)
    assert observed == saved
    result["environment"]["declared"]["mods"].clear()
    result["benchmark"]["run_ticks"] = 1
    assert (blueprint, manifest, environment, plan) == snapshots


@pytest.mark.parametrize("field,value", [
    ("items", -1), ("items", True), ("items", 1.5), ("items", "4"), ("items", None),
    ("completed", 1), ("completed", "true"), ("placement_verified", 0),
    ("notes", []), ("actual_ticks", -1), ("actual_ticks", True), ("actual_ticks", 1.5),
    ("actual_environment", []), ("environment_notes", None),
])
def test_invalid_observations_rejected(plan, field, value):
    with pytest.raises(ValueError):
        record_result(plan, observations(**{field: value}))


@pytest.mark.parametrize("missing", ["items", "completed", "placement_verified"])
def test_required_observations_rejected_when_missing(plan, missing):
    observed = observations()
    del observed[missing]
    with pytest.raises(ValueError):
        record_result(plan, observed)


@pytest.mark.parametrize("change", [
    {"blueprint_sha256": "0" * 64}, {"variant": "survival"}, {"minecraft": "1.21"},
    {"data_version": 1}, {"artifact": {}}, {"artifact": {"path": "build.litematic", "sha256": "bad"}},
    {"artifact": {"path": "", "sha256": "a" * 64}},
])
def test_invalid_manifests_rejected(blueprint, manifest, environment, change):
    with pytest.raises(ValueError):
        create_plan(blueprint, {**manifest, **change}, environment)


def test_version_mismatched_environment_rejected(blueprint, manifest):
    with pytest.raises(ValueError, match="environment Minecraft"):
        create_plan(blueprint, manifest, {"minecraft": "1.21"})


def test_non_json_environment_rejected(blueprint, manifest):
    with pytest.raises(ValueError, match="JSON values"):
        create_plan(blueprint, manifest, {"tps": float("nan")})


@pytest.mark.parametrize("change", ["ticks", "artifact", "environment", "commands", "hash"])
def test_mutated_plan_rejected_for_render_and_record(plan, change):
    if change == "ticks":
        plan["benchmark"]["run_ticks"] = 1
    elif change == "artifact":
        plan["artifact"]["sha256"] = "c" * 64
    elif change == "environment":
        plan["environment"]["declared"]["mods"] = []
    elif change == "commands":
        plan["steps"][0]["commands"].append("/tick unfreeze")
    else:
        del plan["plan_sha256"]
    with pytest.raises(ValueError, match="SHA256"):
        render_plan(plan)
    with pytest.raises(ValueError, match="SHA256"):
        record_result(plan, observations())


def test_saved_json_plan_roundtrip_retains_hash(plan):
    saved = json.loads(json.dumps(plan))
    assert render_plan(saved) == render_plan(plan)
    assert record_result(saved, observations())["plan_sha256"] == plan["plan_sha256"]


def test_zero_warmup_and_single_tick_phases_have_no_zero_tick_sprint(blueprint, manifest, environment):
    blueprint["benchmark"].update(warmup_ticks=0, run_ticks=1, drain_ticks=1)
    manifest["blueprint_sha256"] = blueprint_hash(blueprint)
    plan = create_plan(blueprint, manifest, environment)
    commands = [command for step in plan["steps"] for command in step["commands"]]
    assert commands.count("/tick step 1") == 3
    assert not any(command.startswith("/tick sprint") for command in commands)
    assert record_result(plan, observations(items=1))["items_per_hour"] == 72000


def test_plan_generation_is_deterministic_and_does_not_mutate_inputs(blueprint, manifest, environment):
    before = deepcopy((blueprint, manifest, environment))
    first = create_plan(blueprint, manifest, environment)
    second = create_plan(blueprint, manifest, environment)
    assert first == second
    first["environment"]["declared"]["mods"].clear()
    first["steps"][0]["commands"].clear()
    assert (blueprint, manifest, environment) == before


@pytest.mark.parametrize("field,value", [("drain_ticks", 0), ("run_ticks", 16777217)])
def test_phase_values_unsafe_for_exact_workflow_rejected(blueprint, manifest, environment, field, value):
    blueprint["benchmark"][field] = value
    manifest["blueprint_sha256"] = blueprint_hash(blueprint)
    with pytest.raises(ValueError):
        create_plan(blueprint, manifest, environment)
