"""CLI entry point for mechubbench."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import jsonschema

from . import core, redact, runner

logger = logging.getLogger(__name__)


def _read_token(token_file: str | None, env_var: str) -> str | None:
    """Resolve a bearer token from a file or environment variable.

    Tokens are never accepted as a bare CLI value: process listings (ps,
    /proc, shell history) would leak them. Prefer the file; fall back to
    the environment variable.

    Args:
        token_file: Path to a file containing the token, or None
        env_var: Environment variable name to fall back to

    Returns:
        The token string, or None if neither source provided one

    Raises:
        ValueError: If token_file is given but empty, or cannot be read

    Note:
        token_file is attacker-controllable (a pasted token could end up in
        the path argument by mistake); the error path must never echo it to
        logs or exception messages.
    """
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError:
            logger.error("Failed to read token file")
            raise ValueError("Failed to read token file") from None
        if not token:
            raise ValueError("Token file is empty")
        return token
    return os.environ.get(env_var)


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

    # Fail fast on a scenario declaring a too-wide expected_literals
    # exemption (F4-7), rather than running the whole benchmark and only
    # discovering the redaction config error when writing the manifest.
    for scenario in scenarios:
        for literal in scenario.get("expected_literals") or []:
            try:
                redact.validate_expected_literal(literal)
            except ValueError as e:
                logger.error(
                    f"Scenario '{scenario.get('id', 'unknown')}': {e}"
                )
                return 1

    logger.info(f"Loading tools from {tools_path}")
    tools = core.load_tools(tools_path)
    logger.info(f"Loaded {len(tools)} tool(s)")

    # Validate tools have schema (parameters, inputSchema, or input_schema)
    for tool in tools:
        schema = tool.get("parameters") or tool.get("inputSchema") or tool.get("input_schema")
        if schema is None:
            logger.error(
                f"Tool '{tool.get('name', 'unknown')}' missing schema: "
                "expected 'parameters', 'inputSchema', or 'input_schema'. "
                "Run 'bench export-tools' to get live schemas from MCP server."
            )
            return 1

    # Check mode
    if args.mode == "agentic":
        if not args.mcp_endpoint:
            logger.error(
                "Agentic mode requires --mcp-endpoint (no default: an unset "
                "lab endpoint must never be baked into the tool)"
            )
            return 1

        # Get MCP token from a file or environment (AGENT token - commitless)
        try:
            mcp_token = _read_token(args.mcp_token_file, "RUSTJUNOSMCP_TOKEN")
        except ValueError as e:
            logger.error(str(e))
            return 1
        if not mcp_token:
            logger.error(
                "Agentic mode requires --mcp-token-file (or RUSTJUNOSMCP_TOKEN env var)"
            )
            return 1

        # Get optional setup token (OPERATOR-scoped - can commit)
        try:
            setup_token = _read_token(args.setup_token_file, "RUSTJUNOSMCP_SETUP_TOKEN")
        except ValueError as e:
            logger.error(str(e))
            return 1
        if setup_token:
            logger.info("Setup token provided - scenario fault setup/teardown enabled")
        else:
            logger.info("No setup token - scenarios run against device as-is")

        logger.info(f"Running in AGENTIC mode against real devices")
        logger.info(f"MCP endpoint: {args.mcp_endpoint}")

        # Get device list from MCP
        mcp_client = runner.MCPClient(
            endpoint=args.mcp_endpoint,
            token=mcp_token,
        )

        try:
            # Junos servers expose get_router_list; PAN-OS servers expose
            # list_devices (returns {"devices": [{"name": ...}, ...]}).
            try:
                device_list_result = mcp_client.call_tool("get_router_list", {})
            except runner.MCPError:
                panos_result = mcp_client.call_tool("list_devices", {})
                if isinstance(panos_result, dict):
                    device_list_result = [
                        d.get("name") for d in panos_result.get("devices", [])
                        if isinstance(d, dict) and d.get("name")
                    ]
                else:
                    device_list_result = panos_result

            # Handle both response shapes: bare list (actual) or dict with "routers" key
            if isinstance(device_list_result, list):
                # Bare list of device names: ["br1-fw", "br2-fw", ...]
                device_names = device_list_result
            elif isinstance(device_list_result, dict):
                # Dict shape: {"routers": [...]}
                device_names = device_list_result.get("routers", [])
            else:
                logger.error(f"Unexpected device list format: {type(device_list_result)}")
                return 1
        except runner.MCPError as e:
            logger.error(f"Failed to get device list from MCP: {e}")
            return 1

        # Filter to safe devices (expects list of name strings or dicts with "name" key)
        safe_devices = runner.filter_safe_devices(device_names)
        if not safe_devices:
            logger.error("No safe devices available (all filtered by prod/outpost exclusion)")
            return 1

        # Liveness probe: find first responsive device
        logger.info(f"Probing {len(safe_devices)} safe device(s) for liveness...")
        device = None
        skipped_dead = []

        for candidate in safe_devices:
            candidate_name = candidate if isinstance(candidate, str) else candidate["name"]
            logger.debug(f"Probing {candidate_name}...")

            try:
                # Quick liveness check via gather_device_facts with short timeout
                runner.probe_device_liveness(mcp_client, candidate_name, timeout=10)
                device = candidate_name
                logger.info(f"Selected live device: {device}")
                break
            except runner.MCPError as e:
                logger.warning(f"Device {candidate_name} unreachable: {e}")
                skipped_dead.append(candidate_name)

        if not device:
            logger.error(
                f"No live safe devices found; tried: {', '.join(skipped_dead)}. "
                "Lab devices may be powered off."
            )
            return 1

        manifest = runner.run_all_scenarios_agentic(
            scenarios=scenarios,
            model=args.model,
            tools=tools,
            endpoint=args.endpoint,
            mcp_endpoint=args.mcp_endpoint,
            mcp_token=mcp_token,
            setup_token=setup_token,
            device=device,
            temperature=args.temperature,
            num_predict=args.num_predict,
            keep_alive=args.keep_alive,
            no_teardown=args.no_teardown,
        )
    else:
        # Blind mode (original single-pass)
        logger.info(f"Running in BLIND mode (single-pass, no tool execution)")
        logger.info(f"Endpoint: {args.endpoint}")
        logger.info(f"Temperature: {args.temperature}")
        manifest = runner.run_all_scenarios(
            scenarios, args.model, tools, args.endpoint, args.temperature
        )

    # Write manifest (redacted: model tool arguments are saved verbatim
    # in-memory for scoring, but secret-shaped and identifying values never
    # reach disk). allowed_literals is the union of every loaded scenario's
    # expected_literals, so a value a scorer depends on stays visible.
    allowed_literals: set[str] = set()
    for scenario in scenarios:
        allowed_literals.update(scenario.get("expected_literals") or [])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    redacted_manifest = redact.redact_manifest(
        manifest, allowed_literals=allowed_literals
    )
    output_path.write_text(json.dumps(redacted_manifest, indent=2))
    logger.info(f"Manifest written to {output_path}")

    # Summary
    total = len(manifest["results"])
    passed = sum(1 for r in manifest["results"] if r["pass"])
    logger.info(f"Results: {passed}/{total} passed")

    return 0


def cmd_export_tools(args: argparse.Namespace) -> int:
    """Export live tool schemas from MCP server to eliminate source-extraction drift.

    Args:
        args: Parsed CLI arguments

    Returns:
        Exit code (0 for success)
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    output_path = Path(args.out)

    # Get MCP token from a file or env
    try:
        mcp_token = _read_token(args.mcp_token_file, "RUSTJUNOSMCP_TOKEN")
    except ValueError as e:
        logger.error(str(e))
        return 1
    if not mcp_token:
        logger.error("MCP token required: pass --mcp-token-file or set RUSTJUNOSMCP_TOKEN env var")
        return 1

    # Create MCP client
    try:
        mcp_client = runner.MCPClient(args.mcp_endpoint, mcp_token)
        logger.info(f"Connected to MCP endpoint: {args.mcp_endpoint}")
    except Exception as e:
        logger.error(f"Failed to connect to MCP endpoint: {e}")
        return 1

    # Fetch tools/list from MCP server
    try:
        # MCP protocol: tools/list request
        response = mcp_client._post({
            "jsonrpc": "2.0",
            "id": mcp_client._request_id,
            "method": "tools/list",
            "params": {}
        }, session_id=mcp_client._session_id)

        data = mcp_client._parse_response_body(response)

        if "error" in data:
            logger.error(f"MCP error: {data['error'].get('message', 'Unknown error')}")
            return 1

        result = data.get("result", {})
        tools_list = result.get("tools", [])

        if not tools_list:
            logger.warning("No tools returned from MCP server")
            return 1

        logger.info(f"Fetched {len(tools_list)} tool(s) from MCP server")

        # Note: tools/list may be scope-filtered by token
        # The operator exports with the bench token so the file matches
        # exactly what the agent may call
        logger.info(f"Note: Tool list is scope-filtered by the provided token")

        # Write to output file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(tools_list, indent=2))
        logger.info(f"Exported tools to {output_path}")

        return 0

    except Exception as e:
        logger.error(f"Failed to export tools: {e}")
        return 1


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

            # Check for hardcoded device names without {{device}} placeholder
            prompt = scenario.get("prompt", "")
            setup = scenario.get("setup", "")

            device_patterns = [
                "demo-srx", "demo-pa", "demo-fw", "panosvm",
                r"vsrx-[a-z0-9]+", r"fw-[a-z0-9]+", r"pa-[a-z0-9]+"
            ]

            has_placeholder = "{{device}}" in prompt or "{{device}}" in setup
            has_hardcoded = any(
                pattern in prompt or pattern in setup
                for pattern in ["demo-srx", "demo-pa", "demo-fw", "panosvm"]
            )

            if has_hardcoded and not has_placeholder:
                logger.error(
                    f"  {scenario_file.name}: LINT ERROR - Prompt has hardcoded device name "
                    f"without {{{{device}}}} placeholder"
                )
                errors.append(scenario_file.name)
            else:
                logger.info(f"  {scenario_file.name}: OK")

        except (ValueError, jsonschema.ValidationError) as e:
            logger.error(f"  {scenario_file.name}: INVALID - {e}")
            errors.append(scenario_file.name)

    if errors:
        logger.error(f"{len(errors)} scenario(s) failed validation")
        return 1

    logger.info(f"All {len(scenario_files)} scenario(s) valid")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Split out from main() so tests can inspect defaults (e.g. that --endpoint
    stays on loopback with no flags) without invoking sys.exit.
    """
    parser = argparse.ArgumentParser(
        prog="bench",
        description="Tool-call benchmark corpus and runner for "
        "network-automation agents",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run subcommand
    run_parser = subparsers.add_parser(
        "run", help="Run benchmarks against a model", allow_abbrev=False
    )
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
        default="http://127.0.0.1:11434/v1",
        help="OpenAI-compatible endpoint URL (default: http://127.0.0.1:11434/v1, loopback-only)",
    )
    run_parser.add_argument(
        "--out",
        required=True,
        help="Output manifest path",
    )
    run_parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature (default: 0.0 for deterministic)",
    )
    run_parser.add_argument(
        "--mode",
        choices=["blind", "agentic"],
        default="blind",
        help="Runner mode: 'blind' (single-pass, no execution) or 'agentic' (multi-turn with real tool execution via MCP)",
    )
    run_parser.add_argument(
        "--mcp-endpoint",
        default=None,
        help="MCP endpoint URL, required when --mode agentic (e.g. "
        "http://127.0.0.1:30031/mcp). No default: an unset lab endpoint "
        "must never be baked into the tool.",
    )
    run_parser.add_argument(
        "--mcp-token-file",
        default=None,
        help="Path to file containing the MCP bearer token for agent "
        "(commitless, or set RUSTJUNOSMCP_TOKEN env var). Never pass a token "
        "directly on the command line.",
    )
    run_parser.add_argument(
        "--setup-token-file",
        default=None,
        help="Path to file containing an optional operator-scoped MCP token "
        "for scenario fault setup/teardown (or set RUSTJUNOSMCP_SETUP_TOKEN "
        "env var). Never pass a token directly on the command line.",
    )
    run_parser.add_argument(
        "--num-predict",
        type=int,
        default=None,
        help="Max tokens to predict (Ollama-specific, prevents runaway generations)",
    )
    run_parser.add_argument(
        "--keep-alive",
        default="30m",
        help="Ollama keep-alive duration (default: 30m)",
    )
    run_parser.add_argument(
        "--no-teardown",
        action="store_true",
        help="Skip change-set teardown (leaves staged change-sets live on device for demo use; not for benchmarking)",
    )
    run_parser.set_defaults(func=cmd_run)

    # export-tools subcommand
    export_parser = subparsers.add_parser(
        "export-tools",
        help="Export live tool schemas from MCP server (kills source-extraction drift)",
        allow_abbrev=False,
    )
    export_parser.add_argument(
        "--mcp-endpoint",
        required=True,
        help="MCP endpoint URL (e.g., http://127.0.0.1:30031/mcp)",
    )
    export_parser.add_argument(
        "--mcp-token-file",
        help="Path to file containing the MCP bearer token "
        "(or set RUSTJUNOSMCP_TOKEN env var). Never pass a token directly "
        "on the command line.",
    )
    export_parser.add_argument(
        "--out",
        required=True,
        help="Output path for tools JSON (e.g., tools/junos-tools.json)",
    )
    export_parser.set_defaults(func=cmd_export_tools)

    # lint subcommand
    lint_parser = subparsers.add_parser("lint", help="Validate scenario files")
    lint_parser.add_argument(
        "scenarios",
        help="Directory containing scenario YAML files",
    )
    lint_parser.set_defaults(func=cmd_lint)

    return parser


def main() -> None:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
