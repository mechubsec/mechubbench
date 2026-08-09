"""CLI entry point for mechubbench."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import jsonschema

from . import core, runner

logger = logging.getLogger(__name__)


def cmd_run(args: argparse.Namespace) -> int:
    """Run benchmarks against a model.

    Args:
        args: Parsed CLI arguments

    Returns:
        Exit code (0 for success)
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    scenarios_dir = Path(args.scenarios)
    tools_path = Path(args.tools)
    output_path = Path(args.out)

    if not scenarios_dir.is_dir():
        logger.error(f"Scenarios directory not found: {scenarios_dir}")
        return 1

    if not tools_path.is_file():
        logger.error(f"Tools file not found: {tools_path}")
        return 1

    # Load scenarios and tools
    logger.info(f"Loading scenarios from {scenarios_dir}")
    scenarios = core.load_scenarios(scenarios_dir)
    if not scenarios:
        logger.warning("No scenarios found")
        return 1

    logger.info(f"Loaded {len(scenarios)} scenario(s)")

    logger.info(f"Loading tools from {tools_path}")
    tools = core.load_tools(tools_path)
    logger.info(f"Loaded {len(tools)} tool(s)")

    # Run all scenarios
    logger.info(f"Running against model: {args.model}")
    logger.info(f"Endpoint: {args.endpoint}")
    manifest = runner.run_all_scenarios(scenarios, args.model, tools, args.endpoint)

    # Write manifest
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2))
    logger.info(f"Manifest written to {output_path}")

    # Summary
    total = len(manifest["results"])
    passed = sum(1 for r in manifest["results"] if r["pass"])
    logger.info(f"Results: {passed}/{total} passed")

    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Validate scenario files against schema.

    Args:
        args: Parsed CLI arguments

    Returns:
        Exit code (0 if all valid, 1 if any invalid)
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    scenarios_dir = Path(args.scenarios)
    schema_path = Path(__file__).parent.parent / "scenarios" / "schema.json"

    if not scenarios_dir.is_dir():
        logger.error(f"Scenarios directory not found: {scenarios_dir}")
        return 1

    if not schema_path.is_file():
        logger.error(f"Schema not found: {schema_path}")
        return 1

    logger.info(f"Validating scenarios in {scenarios_dir}")
    scenario_files = list(scenarios_dir.glob("*.y*ml"))
    scenario_files = [f for f in scenario_files if f.suffix in {".yaml", ".yml"}]

    if not scenario_files:
        logger.warning("No scenario files found")
        return 1

    errors = []
    for scenario_file in scenario_files:
        try:
            scenario = core.load_scenario(scenario_file)
            core.validate_scenario(scenario, schema_path)
            logger.info(f"  {scenario_file.name}: OK")
        except (ValueError, jsonschema.ValidationError) as e:
            logger.error(f"  {scenario_file.name}: INVALID - {e}")
            errors.append(scenario_file.name)

    if errors:
        logger.error(f"{len(errors)} scenario(s) failed validation")
        return 1

    logger.info(f"All {len(scenario_files)} scenario(s) valid")
    return 0


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="bench",
        description="Tool-call benchmark corpus and runner for network-automation agents",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run subcommand
    run_parser = subparsers.add_parser("run", help="Run benchmarks against a model")
    run_parser.add_argument(
        "--model",
        required=True,
        help="Model identifier (e.g. llama3.2:3b)",
    )
    run_parser.add_argument(
        "--scenarios",
        required=True,
        help="Directory containing scenario YAML files",
    )
    run_parser.add_argument(
        "--tools",
        default="tools/junos-tools.json",
        help="Path to tools JSON file (default: tools/junos-tools.json)",
    )
    run_parser.add_argument(
        "--endpoint",
        default="http://inference.example.com:11434/v1",
        help="OpenAI-compatible endpoint URL (default: http://inference.example.com:11434/v1)",
    )
    run_parser.add_argument(
        "--out",
        required=True,
        help="Output manifest path",
    )
    run_parser.set_defaults(func=cmd_run)

    # lint subcommand
    lint_parser = subparsers.add_parser("lint", help="Validate scenario files")
    lint_parser.add_argument(
        "scenarios",
        help="Directory containing scenario YAML files",
    )
    lint_parser.set_defaults(func=cmd_lint)

    args = parser.parse_args()
    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
