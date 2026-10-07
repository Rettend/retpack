"""Plans and self-reported results for manual, simulation-tick farm tests."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from .blueprint import blueprint_hash, validate_blueprint


PLAN_FORMAT = "retpack-farm-benchmark-plan-v1"
RESULT_FORMAT = "retpack-farm-benchmark-result-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

# Checked with javap -c -p against the local mapped 26.2 client and Carpet
# v260616 JARs. This is bytecode evidence, not a claim of an in-game test.
_COMMAND_EVIDENCE = {
    "minecraft": "26.2",
    "method": "local_mapped_jar_bytecode",
    "client_jar_sha256": "40896ee9f1e2bec3c934daac7e93d41e9e3d9c2f8ae0ca366d52ffbfd1afa290",
    "carpet_jar_sha256": "f6ada912af65c91536d4b0d80adf26cc438253252ddaf259f2c6617ae471311c",
    "carpet_version": "26.2+v260616",
    "classes": [
        "net.minecraft.server.MinecraftServer",
        "net.minecraft.server.ServerTickRateManager",
        "net.minecraft.world.TickRateManager",
        "net.minecraft.server.commands.TickCommand",
        "net.minecraft.server.commands.GameRuleCommand$1",
        "net.minecraft.world.level.gamerules.GameRules",
        "carpet.commands.CounterCommand",
    ],
    "findings": [
        "requestGameToSprint saves isFrozen as previousIsFrozen and calls setFrozen(false).",
        "finishTickSprint restores setFrozen(previousIsFrozen).",
        "From settled freeze, checkShouldSprintThisTick first sees runGameElements=false and does not decrement.",
        "MinecraftServer then calls TickRateManager.tick before ticking the world, enabling one startup tick.",
        "A frozen-start sprint of N therefore advances N+1 world ticks; this plan requests total_ticks-1.",
        "Issuing tick freeze during a sprint stops it; wait for natural completion instead.",
        "The 26.2 random tick gamerule is random_tick_speed (also accepts minecraft:random_tick_speed).",
        "Carpet registers counter <color> and counter <color> reset.",
    ],
}


def _json_copy(value: dict, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain JSON values") from exc


def _hash(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA256 digest")
    return value


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _step(title: str, instructions: str, *commands: str) -> dict:
    return {"title": title, "instructions": instructions, "commands": list(commands)}


def _phase_command(ticks: int) -> str:
    # A settled frozen -> sprint transition enables one tick before the sprint
    # countdown is first decremented. A step stays frozen and has no such edge.
    return "/tick step 1" if ticks == 1 else f"/tick sprint {ticks - 1}"


def create_plan(blueprint: dict, build_manifest: dict, environment: dict) -> dict:
    """Create instructions only; do not issue commands or inspect a live world.

    The manifest uses blueprint_sha256, variant, minecraft, data_version and
    artifact: {path, sha256}. The CLI checks the actual artifact bytes before
    calling this function. Environment is recorded as declared evidence, not
    as measured world settings or proof that installed mods were loaded.
    """
    blueprint = validate_blueprint(blueprint)
    build = _json_copy(build_manifest, "build_manifest")
    declared = _json_copy(environment, "environment")
    digest = blueprint_hash(blueprint)
    if build.get("blueprint_sha256") != digest:
        raise ValueError("Build manifest does not match the blueprint SHA256")
    if build.get("variant") != "lab":
        raise ValueError("Benchmarking requires a lab build with counter wool")
    _integer(build.get("data_version"), "build_manifest.data_version", 1)
    if build.get("minecraft") != blueprint["minecraft"] or build.get("data_version") != blueprint["data_version"]:
        raise ValueError("Build manifest Minecraft version or DataVersion does not match the blueprint")
    if blueprint["minecraft"] != "26.2":
        raise ValueError("Manual benchmark commands have only been checked for Minecraft 26.2")
    if "minecraft" in declared and declared["minecraft"] != blueprint["minecraft"]:
        raise ValueError("Declared environment Minecraft version does not match the blueprint")
    if "data_version" in declared and declared["data_version"] != blueprint["data_version"]:
        raise ValueError("Declared environment DataVersion does not match the blueprint")
    artifact = build.get("artifact")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str) or not artifact["path"].strip():
        raise ValueError("Build manifest must include artifact.path and artifact.sha256")
    _sha256(artifact.get("sha256"), "artifact.sha256")

    benchmark = deepcopy(blueprint["benchmark"])
    if benchmark["item"] != "minecraft:bamboo":
        raise ValueError("The first benchmark workflow supports natural bamboo growth only")
    ports = blueprint["ports"]
    if len(ports) != 1 or ports[0]["counter"] != benchmark["counter"]:
        raise ValueError("The first benchmark workflow requires exactly one matching counter output")
    warmup = _integer(benchmark["warmup_ticks"], "warmup_ticks")
    run = _integer(benchmark["run_ticks"], "run_ticks", 1)
    drain = _integer(benchmark["drain_ticks"], "drain_ticks", 1)
    # TimeArgument parses a float. All integers through 2**24 are exact.
    if max(warmup, run, drain) > 2**24:
        raise ValueError("Sprint phases must not exceed 16777216 ticks; split into a new shorter plan")
    conditions = deepcopy(blueprint["conditions"])
    counter = benchmark["counter"]
    growth = f"/gamerule random_tick_speed {conditions['random_tick_speed']}"
    no_growth = "/gamerule random_tick_speed 0"
    check_frozen = "/tick query"

    steps = [
        _step(
            "Prepare and verify placement",
            f"Use a separate Creative lab world with cheats. Freeze before placing the lab schematic. "
            f"Verify the pasted blocks and states against the artifact, including the output hopper facing {counter} wool. "
            f"Start with empty collection inventories and no old dropped items in the collection path. "
            f"Use {conditions['dimension']}, simulation distance {conditions['simulation_distance']}, and keep the player nearby "
            f"with the bamboo lit. Only this farm may feed the {counter} counter. Do not use bonemeal or other item sources. "
            "Record the original gamerule and tick state before changing them. Check the loaded Minecraft/Carpet versions "
            "and any disabled mod modules; installed files alone do not establish the live environment.",
            "/tick freeze", check_frozen, "/carpet hopperCounters true",
            f"/difficulty {conditions['difficulty']}", growth,
        ),
        _step(
            "Warm up",
            "Wait for the sprint completion message, then confirm the world is frozen. Discard warm-up counts. "
            "Do not issue any commands during a sprint or unfreeze between phases.",
            *([_phase_command(warmup)] if warmup else []), check_frozen,
        ),
        _step(
            "Drain warm-up items",
            "Stop natural bamboo growth, then let pending harvests and transport finish. After the sprint, check that "
            "collection inventories and the transport path are empty and the harvesting mechanism has settled. "
            "If transport backlog remains, stop: choose a longer drain in a new plan and repeat. "
            "Resetting just the counter would leave warm-up items to pollute the measured run.",
            no_growth, _phase_command(drain), check_frozen,
        ),
        _step(
            "Reset and start clean",
            "While still frozen, reset only this counter and confirm zero items. Restore the planned growth speed. "
            "Leave the warmed-up standing bamboo in place; do not add or remove items after the reset.",
            f"/counter {counter} reset", f"/counter {counter}", growth,
        ),
        _step(
            "Run",
            f"Run exactly {run} growth-enabled simulation ticks. Wait for natural completion and confirm frozen state. "
            "An interrupted sprint, unloaded farm, or extra unfrozen ticks makes the run noncomparable. "
            "Do not send /tick freeze during the sprint: it cancels the remaining ticks.",
            _phase_command(run), check_frozen,
        ),
        _step(
            "Drain and read the count",
            "While frozen, stop bamboo growth before the final drain. Wait for completion and confirm frozen state. "
            "Check the same empty transport/settled harvest condition as before the reset. Read the raw bamboo item "
            "count, not Carpet's hourly estimate or realtime display. Counts include run output delivered during "
            "the growth-disabled drain. If transport backlog remains, record completed=false and repeat with a new "
            "longer-drain plan. Note stray uncollected items separately; this count does not measure loss percentage.",
            no_growth, _phase_command(drain), check_frozen, f"/counter {counter}",
        ),
        _step(
            "Record and restore",
            "Record items, completed, placement_verified, and notes. Set completed=true only if every phase finished "
            "and both transport checks passed without extra growth ticks. Optional actual_ticks means growth-enabled "
            "run ticks only, excluding warm-up and both drains. Record deviations and live environment checks in notes "
            "or actual_environment. After recording, restore the original gamerule, Carpet rule, difficulty and tick "
            "state if needed. Use /tick unfreeze only after recording, and only if the world was originally running.",
        ),
    ]
    plan = {
        "format": PLAN_FORMAT,
        "name": blueprint["name"],
        "blueprint_sha256": digest,
        "artifact": {"path": artifact["path"], "sha256": artifact["sha256"]},
        "variant": "lab",
        "minecraft": blueprint["minecraft"],
        "data_version": blueprint["data_version"],
        "conditions": conditions,
        "benchmark": benchmark,
        "environment": {
            "declared": declared,
            "measured": {},
            "assumptions": [
                "The operator checks the live world settings and loaded mods before running.",
                "Bamboo grows only through natural random ticks; no bonemeal or other production sources.",
            ],
        },
        "methodology": {
            "rate_denominator": "growth_enabled_run_ticks_only",
            "nominal_ticks_per_second": 20,
            "drain_random_tick_speed": 0,
            "frozen_sprint_startup_ticks": 1,
            "sprint_argument": "phase_ticks_minus_one_from_settled_freeze",
            "warmup_drain_ticks": drain,
            "final_drain_ticks": drain,
            "count": "raw_target_items_delivered_after_reset_through_final_drain",
            "measurement": "manual_operator_observations",
            "loss_percentage": "not_measured",
        },
        "command_evidence": deepcopy(_COMMAND_EVIDENCE),
        "steps": steps,
        "notes": deepcopy(blueprint.get("notes", [])),
    }
    plan["plan_sha256"] = _hash(plan)
    return plan


def _validated_plan(plan: dict) -> dict:
    plan = _json_copy(plan, "plan")
    if plan.get("format") != PLAN_FORMAT:
        raise ValueError("Unsupported benchmark plan format")
    digest = _sha256(plan.pop("plan_sha256", None), "plan_sha256")
    if _hash(plan) != digest:
        raise ValueError("Plan SHA256 mismatch; regenerate the plan instead of editing it")
    try:
        _sha256(plan["blueprint_sha256"], "blueprint_sha256")
        _sha256(plan["artifact"]["sha256"], "artifact.sha256")
        _integer(plan["benchmark"]["run_ticks"], "run_ticks", 1)
        _integer(plan["benchmark"]["warmup_ticks"], "warmup_ticks")
        _integer(plan["benchmark"]["drain_ticks"], "drain_ticks", 1)
        if plan["methodology"]["rate_denominator"] != "growth_enabled_run_ticks_only":
            raise ValueError("Unsupported rate denominator")
        if plan["methodology"]["nominal_ticks_per_second"] != 20:
            raise ValueError("Unsupported nominal tick conversion")
        if plan["methodology"]["frozen_sprint_startup_ticks"] != 1:
            raise ValueError("Unsupported frozen sprint startup convention")
        _json_copy(plan["environment"], "environment")
    except (KeyError, TypeError) as exc:
        raise ValueError("Malformed benchmark plan") from exc
    plan["plan_sha256"] = digest
    return plan


def render_plan(plan: dict) -> str:
    """Render a saved, intact plan as a short manual checklist."""
    plan = _validated_plan(plan)
    benchmark = plan["benchmark"]
    lines = [
        f"# Benchmark: {plan['name']}", "",
        f"Minecraft {plan['minecraft']} (DataVersion {plan['data_version']}); lab artifact `{plan['artifact']['path']}`.",
        f"Warm-up: {benchmark['warmup_ticks']} ticks. Run: {benchmark['run_ticks']} ticks. "
        f"Each growth-disabled drain: {benchmark['drain_ticks']} ticks.", "",
        "Run these commands manually, one phase at a time. Wait for every sprint to finish before the next command. "
        "A sprint started frozen restores frozen state in 26.2; keep it frozen between phases to avoid idle growth.", "",
        "Starting from a settled freeze adds one startup simulation tick before the sprint countdown. "
        "The commands below request one fewer sprint tick so each phase advances exactly its listed total. "
        "Wait for the frozen query response before starting each phase. For a one-tick phase, /tick step 1 is used; "
        "let the server finish that step before sending the next command.", "",
        "The rate is raw items × 72000 / growth-enabled run ticks (a simulated hour at 20 ticks/s). "
        "Warm-up and drain ticks are excluded. Sprint wall-clock duration and Carpet's displayed hourly rate are not used.", "",
        "The environment in plan.json describes installed or declared versions, not verified live settings. "
        "Record live checks or deviations with the result. Incomplete or placement-unverified runs are not comparable.", "",
    ]
    for number, step in enumerate(plan["steps"], 1):
        lines.extend([f"## {number}. {step['title']}", "", step["instructions"], ""])
        if step["commands"]:
            lines.extend(["```mcfunction", *step["commands"], "```", ""])
    if plan["notes"]:
        lines.extend(["## Blueprint notes", "", *(f"- {note}" for note in plan["notes"]), ""])
    lines.extend([
        "## Identity", "",
        f"- Blueprint SHA256: `{plan['blueprint_sha256']}`",
        f"- Artifact SHA256: `{plan['artifact']['sha256']}`",
        f"- Plan SHA256: `{plan['plan_sha256']}`", "",
        "Command behavior was checked in local mapped Minecraft 26.2 and Carpet v260616 bytecode; "
        "see command_evidence in plan.json. No game commands are sent by this tool.", "",
    ])
    return "\n".join(lines)


def record_result(plan: dict, observations: dict) -> dict:
    """Record self-reported counts with an explicit simulation-tick denominator."""
    plan = _validated_plan(plan)
    observed = _json_copy(observations, "observations")
    items = _integer(observed.get("items"), "items")
    for key in ("completed", "placement_verified"):
        if type(observed.get(key)) is not bool:
            raise ValueError(f"{key} must be a boolean")
    observed["notes"] = _text(observed.get("notes", ""), "notes")
    if "environment_notes" in observed:
        _text(observed["environment_notes"], "environment_notes")
    measured = _json_copy(observed.get("actual_environment", {}), "actual_environment")
    planned_ticks = plan["benchmark"]["run_ticks"]
    if "actual_ticks" in observed:
        ticks = _integer(observed["actual_ticks"], "actual_ticks")
        tick_source = "operator_reported_actual_ticks"
    elif observed["completed"]:
        ticks = planned_ticks
        tick_source = "planned_ticks_attested_by_completed"
    else:
        ticks = None
        tick_source = "unknown_incomplete_run"

    flags = []
    if not observed["completed"]:
        flags.append("incomplete")
    if not observed["placement_verified"]:
        flags.append("placement_unverified")
    if ticks is not None and ticks != planned_ticks:
        flags.append("run_ticks_differ_from_plan")
    for key in ("minecraft", "data_version"):
        if key in measured and measured[key] != plan[key]:
            flags.append(f"actual_{key}_differs_from_plan")
    for key in ("fabric", "mods"):
        if key in measured and key in plan["environment"]["declared"]:
            if measured[key] != plan["environment"]["declared"][key]:
                flags.append(f"actual_{key}_differs_from_plan")
    if "conditions" in measured:
        actual_conditions = _json_copy(measured["conditions"], "actual_environment.conditions")
        for key, value in actual_conditions.items():
            if key in plan["conditions"] and value != plan["conditions"][key]:
                flags.append(f"actual_{key}_differs_from_plan")
    comparable = not flags
    status = "completed" if comparable else "noncomparable"
    if not observed["completed"]:
        status = "incomplete"
    elif not observed["placement_verified"]:
        status = "unverified"
    rate_available = ticks is not None and ticks > 0
    return {
        "format": RESULT_FORMAT,
        "name": plan["name"],
        "plan_sha256": plan["plan_sha256"],
        "blueprint_sha256": plan["blueprint_sha256"],
        "artifact": deepcopy(plan["artifact"]),
        "minecraft": plan["minecraft"],
        "data_version": plan["data_version"],
        "conditions": deepcopy(plan["conditions"]),
        "benchmark": deepcopy(plan["benchmark"]),
        "methodology": deepcopy(plan["methodology"]),
        "environment": {
            "declared": deepcopy(plan["environment"]["declared"]),
            "measured": measured,
            "measurement_source": "operator_reported" if measured else "not_measured",
            "assumptions": deepcopy(plan["environment"]["assumptions"]),
            "notes": observed.get("environment_notes", ""),
        },
        "observations": observed,
        "status": status,
        "comparable": comparable,
        "flags": flags,
        "ticks": ticks,
        "tick_source": tick_source,
        "items_per_tick": items / ticks if rate_available else None,
        "items_per_hour": items * 72000 / ticks if rate_available else None,
        "rate_kind": "comparable" if comparable else "diagnostic_only" if rate_available else "unavailable",
        "warning": "Self-reported output count; not a measured loss rate or proof of the live mod environment.",
    }
