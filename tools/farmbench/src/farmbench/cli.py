"""Command-line entry point for the manual farm lab workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .environment import capture_environment


def _read_json(path: str | Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: dict) -> None:
    content = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="farmbench", description="Compile farm blueprints and record manual Minecraft benchmarks.")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Check a blueprint's geometry, blocks and output ports")
    validate.add_argument("blueprint", type=Path)
    build = commands.add_parser("build", help="Export a lab or survival schematic and materials")
    build.add_argument("blueprint", type=Path)
    build.add_argument("--variant", choices=("lab", "survival"), default="lab")
    build.add_argument("--out", type=Path, required=True, help="New output directory")
    plan = commands.add_parser("plan", help="Write a manual benchmark plan for a compiled lab build")
    plan.add_argument("blueprint", type=Path)
    plan.add_argument("--build", type=Path, required=True, help="The exported build.json")
    plan.add_argument("--instance", type=Path, required=True, help="Retpack game directory with Carpet installed")
    plan.add_argument("--out", type=Path, required=True, help="New plan directory")
    record = commands.add_parser("record", help="Record observations after following a saved plan")
    record.add_argument("plan", type=Path)
    record.add_argument("--items", type=int, required=True, help="Target item count from Carpet, not its hourly estimate")
    record.add_argument("--completed", action="store_true", help="All planned simulation and drain ticks completed")
    record.add_argument("--placement-verified", action="store_true", help="Pasted blocks and output path were checked before testing")
    record.add_argument("--notes", default="")
    record.add_argument("--actual-ticks", type=int, help="Known growth-enabled run ticks if the run differed from the plan")
    record.add_argument("--out", type=Path, required=True, help="New result JSON file")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        # Lazy imports keep --help usable before the rest of the tool is loaded.
        from .blueprint import blueprint_hash, load_blueprint

        if args.command == "validate":
            data = load_blueprint(args.blueprint)
            print(f"Valid: {data['name']} ({len(data['blocks'])} blocks, Minecraft {data['minecraft']})")
            print(f"Blueprint SHA256: {blueprint_hash(data)}")
        elif args.command == "build":
            from .compiler import compile_blueprint

            compile_blueprint(load_blueprint(args.blueprint), args.out, variant=args.variant)
            print(f"Built {args.variant} schematic in {args.out}")
            print(f"Read {args.out / 'placement.md'} before pasting.")
        elif args.command == "plan":
            from .benchmark import create_plan, render_plan

            data = load_blueprint(args.blueprint)
            build = _read_json(args.build)
            # Verify the actual exported file before recording its identity.
            artifact = build["artifact"]
            artifact_path = args.build.parent / artifact["path"]
            actual = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            if actual != artifact["sha256"]:
                raise ValueError("Schematic checksum differs from build.json; rebuild before benchmarking")
            environment = capture_environment(args.instance)
            if environment["minecraft"] != data["minecraft"]:
                raise ValueError("The selected instance runs a different Minecraft version")
            saved_simulation = environment["saved_options"].get("simulationDistance")
            if saved_simulation is not None and int(saved_simulation) != data["conditions"]["simulation_distance"]:
                raise ValueError("Saved simulation distance differs from blueprint conditions; align them before planning")
            plan = create_plan(data, build, environment)
            instructions = render_plan(plan)
            args.out.mkdir(parents=True, exist_ok=False)
            _write_json(args.out / "plan.json", plan)
            (args.out / "benchmark.md").write_text(instructions, encoding="utf-8")
            print(f"Plan saved: {args.out / 'plan.json'}")
            print(f"Follow {args.out / 'benchmark.md'} in your Creative test world.")
        elif args.command == "record":
            from .benchmark import record_result

            observations = {
                "items": args.items,
                "completed": args.completed,
                "placement_verified": args.placement_verified,
                "notes": args.notes,
            }
            if args.actual_ticks is not None:
                observations["actual_ticks"] = args.actual_ticks
            result = record_result(_read_json(args.plan), observations)
            result["recorded_at"] = datetime.now(timezone.utc).isoformat()
            result["farmbench_version"] = __version__
            _write_json(args.out, result)
            print(f"Result saved: {args.out}")
            if result["comparable"]:
                print(f"Delivered output: {result['items_per_hour']:.2f} items/simulated hour")
            else:
                print(f"Diagnostic run ({', '.join(result['flags'])}); do not use it to compare designs.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(f"farmbench: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
