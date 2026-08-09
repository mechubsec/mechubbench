"""Runner: drives scenarios through LLM with tool-calling, scores results.

Network and LLM interactions isolated here, injectable for testing.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from . import core, scoring

logger = logging.getLogger(__name__)


class MCPError(Exception):
    """MCP protocol or tool execution error."""
    pass


class MCPClient:
    """Client for rust-junosmcp MCP server via streamable-HTTP bearer auth."""

    def __init__(self, endpoint: str, token: str, timeout: int = 120):
        """Initialize MCP client.

        Args:
            endpoint: MCP endpoint URL (e.g. http://198.51.100.194:30031/mcp)
            token: Bearer token for authentication
            timeout: Request timeout in seconds
        """
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self._request_id = 0

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        """Execute a tool via MCP protocol.

        Args:
            tool_name: Name of tool to call
            arguments: Tool arguments

        Returns:
            Tool result dict (parsed from JSON in text content)

        Raises:
            MCPError: If MCP returns error or result format is unexpected
        """
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        headers = {"Authorization": f"Bearer {self.token}"}

        try:
            response = httpx.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise MCPError(f"HTTP error calling {tool_name}: {e}")

        data = response.json()

        # Check for JSON-RPC error
        if "error" in data:
            error_msg = data["error"].get("message", "Unknown error")
            raise MCPError(f"MCP error: {error_msg}")

        # Extract result from content
        result = data.get("result", {})
        content = result.get("content", [])
        if not content:
            raise MCPError(f"No content in MCP result for {tool_name}")

        # Parse text from first content item
        text_content = content[0].get("text", "{}")
        try:
            return json.loads(text_content)
        except json.JSONDecodeError as e:
            raise MCPError(f"Failed to parse tool result JSON: {e}")


def filter_safe_devices(devices: list[dict]) -> list[dict]:
    """Filter device list to exclude prod/outpost devices (safety rail).

    Args:
        devices: List of device dicts with 'name' key

    Returns:
        Filtered list excluding devices with 'prod' or 'outpost' in name (case-insensitive)
    """
    forbidden_patterns = ["prod", "outpost"]
    safe_devices = []

    for device in devices:
        name_lower = device["name"].lower()
        if not any(pattern in name_lower for pattern in forbidden_patterns):
            safe_devices.append(device)

    return safe_devices


class AgenticRunner:
    """Agentic loop runner: executes tool calls against real devices via MCP."""

    # Hard-coded safety rail: tools that must never be executed
    FORBIDDEN_MUTATING_TOOLS = {
        "approve_panos_change_set",
        "apply_panos_change_set",
        "commit_panos_candidate",
        "load_and_commit_config",
    }

    def __init__(
        self,
        llm_client: LLMClient,
        mcp_client: MCPClient,
        device: str,
        max_turns: int = 12,
        forbidden_tools: set[str] | None = None,
    ):
        """Initialize agentic runner.

        Args:
            llm_client: LLM client for tool-calling
            mcp_client: MCP client for tool execution
            device: Device name to use for tool calls
            max_turns: Maximum conversation turns (default 12)
            forbidden_tools: Additional forbidden tools beyond hard-coded set
        """
        self.llm_client = llm_client
        self.mcp_client = mcp_client
        self.device = device
        self.max_turns = max_turns

        # Merge hard-coded forbidden tools with any extras
        self.forbidden_tools = self.FORBIDDEN_MUTATING_TOOLS.copy()
        if forbidden_tools:
            self.forbidden_tools.update(forbidden_tools)

    def run_scenario(
        self,
        scenario: dict,
        model: str,
        tools: list[dict],
        temperature: float = 0.0,
    ) -> dict:
        """Run scenario through agentic loop with tool execution.

        Args:
            scenario: Scenario dict
            model: Model identifier
            tools: Tool definitions
            temperature: Sampling temperature

        Returns:
            Result dict with id, pass, reason, started, finished, transcript
        """
        started = datetime.now(timezone.utc).isoformat()
        transcript = []

        messages = [{"role": "user", "content": scenario["prompt"]}]
        openai_tools = convert_tools_to_openai_format(tools)

        try:
            for turn in range(self.max_turns):
                logger.debug(f"Turn {turn + 1}/{self.max_turns}")

                # Get LLM response
                response = self.llm_client.complete_with_tools(
                    model, messages, openai_tools, temperature
                )

                choice = response.get("choices", [{}])[0]
                finish_reason = choice.get("finish_reason")
                message = choice.get("message", {})

                # If model stopped without tool calls, we're done
                if finish_reason == "stop" and not message.get("tool_calls"):
                    logger.debug("Model stopped")
                    break

                # Extract and execute tool calls
                tool_calls = extract_tool_calls(response)
                if not tool_calls:
                    logger.debug("No tool calls, stopping")
                    break

                # Append assistant message with tool calls to conversation
                messages.append(message)

                # Execute each tool call and collect results
                for call in tool_calls:
                    tool_name = call["tool"]
                    tool_args = call["args"]

                    # Add to transcript
                    transcript.append({"tool": tool_name, "args": tool_args})

                    # Safety rail: never execute forbidden tools
                    if tool_name in self.forbidden_tools:
                        logger.warning(f"Blocked forbidden tool: {tool_name}")
                        tool_result = {
                            "error": f"Tool {tool_name} is forbidden in benchmark mode"
                        }
                    else:
                        # Execute via MCP
                        try:
                            tool_result = self.mcp_client.call_tool(tool_name, tool_args)
                        except MCPError as e:
                            logger.error(f"MCP error executing {tool_name}: {e}")
                            tool_result = {"error": str(e)}

                    # Append tool result to conversation
                    tool_message = {
                        "role": "tool",
                        "content": json.dumps(tool_result),
                        "tool_call_id": f"call_{len(transcript)}",
                    }
                    messages.append(tool_message)

                # If no more tool calls pending, we're done
                if finish_reason != "tool_calls":
                    break

            else:
                # Hit max_turns without stopping
                logger.warning(f"Scenario {scenario['id']} hit max_turns limit")
                finished = datetime.now(timezone.utc).isoformat()
                score_result = scoring.score_scenario(scenario, transcript)
                return {
                    "id": scenario["id"],
                    "pass": False,
                    "reason": f"incomplete: hit {self.max_turns}-turn limit, {score_result['reason']}",
                    "started": started,
                    "finished": finished,
                    "transcript": transcript,
                }

        except Exception as e:
            logger.error(f"Scenario {scenario['id']} failed: {e}")
            finished = datetime.now(timezone.utc).isoformat()
            return {
                "id": scenario["id"],
                "pass": False,
                "reason": f"runner_error: {e}",
                "started": started,
                "finished": finished,
                "transcript": transcript,
            }

        # Score the realized transcript
        score_result = scoring.score_scenario(scenario, transcript)
        finished = datetime.now(timezone.utc).isoformat()

        return {
            "id": scenario["id"],
            "pass": score_result["pass"],
            "reason": score_result["reason"],
            "started": started,
            "finished": finished,
            "transcript": transcript,
        }


class OllamaHealthProbe:
    """Health probe for Ollama serving stack to detect degradation."""

    def __init__(self, base_url: str):
        """Initialize health probe.

        Args:
            base_url: Ollama base URL (e.g. http://inference.example.com:11434)
        """
        self.base_url = base_url.rstrip("/")

    def get_running_models(self) -> list[dict]:
        """Query /api/ps for currently loaded models.

        Returns:
            List of running model dicts from Ollama

        Raises:
            httpx.HTTPError: If request fails
        """
        response = httpx.get(f"{self.base_url}/api/ps", timeout=10.0)
        response.raise_for_status()
        data = response.json()
        return data.get("models", [])

    def wait_for_settle(self, timeout: float = 30.0) -> None:
        """Wait for Ollama to settle (no running generations).

        Args:
            timeout: Maximum time to wait in seconds

        Raises:
            TimeoutError: If settle doesn't happen within timeout
        """
        start = time.time()
        while time.time() - start < timeout:
            models = self.get_running_models()
            # Consider settled if no models running or all are idle
            if not models:
                logger.debug("Ollama settled (no models loaded)")
                return

            # Check if any model is actively processing
            # (Ollama /api/ps doesn't expose this cleanly, so we just wait a bit)
            time.sleep(2.0)

        # After timeout, log warning but don't fail
        logger.warning(f"Ollama settle wait timed out after {timeout}s")


class LLMClient:
    """Thin adapter for OpenAI-compatible chat completions with tool calling."""

    def __init__(
        self,
        endpoint: str,
        timeout: int = 180,
        num_predict: int | None = None,
        keep_alive: str | None = None,
    ):
        """Initialize client.

        Args:
            endpoint: Base URL of OpenAI-compatible endpoint (e.g. http://host:port/v1)
            timeout: Request timeout in seconds (default 180 for LLM inference)
            num_predict: Max tokens to predict (Ollama-specific, prevents runaway)
            keep_alive: Keep-alive duration for Ollama (e.g. "30m")
        """
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.num_predict = num_predict
        self.keep_alive = keep_alive

    def complete_with_tools(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        temperature: float = 0.0,
    ) -> dict:
        """Run chat completion with tool definitions.

        Args:
            model: Model identifier
            messages: Chat messages in OpenAI format
            tools: Tool definitions in OpenAI format
            temperature: Sampling temperature (default 0.0 for deterministic)

        Returns:
            Response dict with choices, tool_calls if any
        """
        url = f"{self.endpoint}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
        }

        # Add Ollama-specific serving fixes
        options = {}
        if self.num_predict is not None:
            options["num_predict"] = self.num_predict
        if options:
            payload["options"] = options

        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive

        # Hard timeout with cancellation potential
        response = httpx.post(url, json=payload, timeout=self.timeout)
        response.raise_for_status()
        return response.json()


def convert_tools_to_openai_format(tools: list[dict]) -> list[dict]:
    """Convert tool definitions to OpenAI chat completions format.

    Args:
        tools: List of {name, description, parameters} dicts

    Returns:
        List of OpenAI tool defs with type: "function"
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


def extract_tool_calls(response: dict) -> list[dict]:
    """Extract tool calls from OpenAI-format response.

    Args:
        response: Response dict from chat completions

    Returns:
        List of {tool, args} dicts
    """
    tool_calls = []
    choices = response.get("choices", [])
    if not choices:
        return tool_calls

    message = choices[0].get("message", {})
    raw_calls = message.get("tool_calls", [])

    for call in raw_calls:
        if call.get("type") != "function":
            continue
        function = call.get("function", {})
        tool_name = function.get("name")
        args_str = function.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse tool args: {args_str}")
            args = {}

        tool_calls.append({"tool": tool_name, "args": args})

    return tool_calls


def run_scenario(
    scenario: dict,
    model: str,
    tools: list[dict],
    client: LLMClient,
    temperature: float = 0.0,
) -> dict:
    """Run a single scenario through the LLM and score it.

    Args:
        scenario: Scenario dict
        model: Model identifier
        tools: Tool definitions
        client: LLM client instance
        temperature: Sampling temperature

    Returns:
        Result dict with id, pass, reason, started, finished, transcript
    """
    started = datetime.now(timezone.utc).isoformat()

    messages = [{"role": "user", "content": scenario["prompt"]}]
    openai_tools = convert_tools_to_openai_format(tools)

    try:
        response = client.complete_with_tools(model, messages, openai_tools, temperature)
        transcript = extract_tool_calls(response)
    except Exception as e:
        logger.error(f"Scenario {scenario['id']} failed: {e}")
        finished = datetime.now(timezone.utc).isoformat()
        return {
            "id": scenario["id"],
            "pass": False,
            "reason": f"llm_error: {e}",
            "started": started,
            "finished": finished,
            "transcript": [],
        }

    score_result = scoring.score_scenario(scenario, transcript)
    finished = datetime.now(timezone.utc).isoformat()

    return {
        "id": scenario["id"],
        "pass": score_result["pass"],
        "reason": score_result["reason"],
        "started": started,
        "finished": finished,
        "transcript": transcript,
    }


def run_all_scenarios(
    scenarios: list[dict],
    model: str,
    tools: list[dict],
    endpoint: str,
    temperature: float = 0.0,
) -> dict:
    """Run all scenarios and assemble a manifest (blind mode: single-pass, no execution).

    Args:
        scenarios: List of scenario dicts
        model: Model identifier
        tools: Tool definitions
        endpoint: LLM endpoint URL
        temperature: Sampling temperature

    Returns:
        Manifest dict
    """
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()
    client = LLMClient(endpoint)

    results = []
    for scenario in scenarios:
        logger.info(f"Running scenario: {scenario['id']}")
        result = run_scenario(scenario, model, tools, client, temperature)
        results.append(result)
        status = "PASS" if result["pass"] else "FAIL"
        logger.info(f"  Result: {status} - {result['reason']}")

    finished = datetime.now(timezone.utc).isoformat()

    # Get git SHA for corpus version (best effort)
    corpus_git_sha = "unknown"
    try:
        import subprocess

        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        corpus_git_sha = sha.stdout.strip()
    except Exception:
        pass

    return core.assemble_manifest(
        run_id=run_id,
        model=model,
        corpus_git_sha=corpus_git_sha,
        started=started,
        finished=finished,
        results=results,
    )


def run_all_scenarios_agentic(
    scenarios: list[dict],
    model: str,
    tools: list[dict],
    endpoint: str,
    mcp_endpoint: str,
    mcp_token: str,
    device: str,
    temperature: float = 0.0,
    num_predict: int | None = None,
    keep_alive: str | None = "30m",
) -> dict:
    """Run all scenarios in agentic mode with tool execution via MCP.

    Args:
        scenarios: List of scenario dicts
        model: Model identifier
        tools: Tool definitions
        endpoint: LLM endpoint URL (OpenAI-compatible)
        mcp_endpoint: MCP server endpoint URL
        mcp_token: MCP bearer token
        device: Device name to use for tool calls
        temperature: Sampling temperature
        num_predict: Max tokens (Ollama-specific)
        keep_alive: Keep-alive duration (Ollama-specific)

    Returns:
        Manifest dict with devices_touched field
    """
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()

    # Initialize clients
    llm_client = LLMClient(
        endpoint=endpoint,
        num_predict=num_predict,
        keep_alive=keep_alive,
    )
    mcp_client = MCPClient(endpoint=mcp_endpoint, token=mcp_token)

    # Health probe for Ollama (extract base URL from endpoint)
    if "/v1" in endpoint:
        ollama_base = endpoint.replace("/v1", "")
        health_probe = OllamaHealthProbe(ollama_base)
    else:
        health_probe = None

    # Create agentic runner
    agentic_runner = AgenticRunner(
        llm_client=llm_client,
        mcp_client=mcp_client,
        device=device,
        max_turns=12,
    )

    results = []
    devices_touched = {device}  # Track devices used

    for i, scenario in enumerate(scenarios):
        logger.info(f"Running scenario {i+1}/{len(scenarios)}: {scenario['id']}")

        # Wait for Ollama to settle between scenarios
        if health_probe and i > 0:
            logger.debug("Waiting for Ollama to settle...")
            try:
                health_probe.wait_for_settle(timeout=10.0)
            except Exception as e:
                logger.warning(f"Health probe failed: {e}")

        # Run scenario
        result = agentic_runner.run_scenario(scenario, model, tools, temperature)
        results.append(result)

        status = "PASS" if result["pass"] else "FAIL"
        logger.info(f"  Result: {status} - {result['reason']}")

    finished = datetime.now(timezone.utc).isoformat()

    # Get git SHA
    corpus_git_sha = "unknown"
    try:
        import subprocess

        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        corpus_git_sha = sha.stdout.strip()
    except Exception:
        pass

    # Assemble manifest with devices_touched
    manifest = core.assemble_manifest(
        run_id=run_id,
        model=model,
        corpus_git_sha=corpus_git_sha,
        started=started,
        finished=finished,
        results=results,
    )

    # Add agentic-mode specific fields
    manifest["mode"] = "agentic"
    manifest["devices_touched"] = sorted(devices_touched)

    return manifest
