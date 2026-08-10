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
    """Client for rust-junosmcp MCP server via streamable-HTTP bearer auth.

    Implements MCP streamable-HTTP protocol (2025-03-26):
    1. Send Accept: application/json, text/event-stream header
    2. Initialize session first (capture mcp-session-id)
    3. Send notifications/initialized
    4. Include mcp-session-id on all subsequent requests
    5. Parse SSE (data: lines) when Content-Type is text/event-stream
    """

    def __init__(self, endpoint: str, token: str, timeout: int = 120):
        """Initialize MCP client and establish session.

        Args:
            endpoint: MCP endpoint URL (e.g. http://198.51.100.194:30031/mcp)
            token: Bearer token for authentication
            timeout: Request timeout in seconds

        Raises:
            MCPError: If initialization fails
        """
        self.endpoint = endpoint
        self.token = token
        self.timeout = timeout
        self._request_id = 0
        self._session_id = None

        # Perform MCP initialize handshake
        self._initialize()

    def _initialize(self) -> None:
        """Perform MCP initialize + notifications/initialized handshake."""
        # Step 1: Send initialize request
        init_payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "mechubbench",
                    "version": "1.0"
                }
            }
        }

        init_response = self._post(init_payload, session_id=None)

        # Capture session ID from response headers
        self._session_id = init_response.headers.get("mcp-session-id") or init_response.headers.get("Mcp-Session-Id")
        if not self._session_id:
            raise MCPError("Server did not return Mcp-Session-Id header on initialize")

        # Parse initialize response
        init_data = self._parse_response_body(init_response)
        if "error" in init_data:
            raise MCPError(f"Initialize failed: {init_data['error'].get('message', 'Unknown error')}")

        # Step 2: Send notifications/initialized
        initialized_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }

        try:
            self._post(initialized_payload, session_id=self._session_id)
        except Exception as e:
            raise MCPError(f"notifications/initialized failed: {e}")

    def _post(self, payload: dict, session_id: str | None) -> httpx.Response:
        """Send POST request with proper MCP headers.

        Args:
            payload: JSON-RPC payload
            session_id: MCP session ID (None for initialize request)

        Returns:
            httpx.Response object

        Raises:
            MCPError: On HTTP errors (with token redacted)
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        if session_id:
            headers["Mcp-Session-Id"] = session_id

        try:
            response = httpx.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as e:
            # Redact token from error message
            error_msg = str(e)
            if self.token in error_msg:
                error_msg = error_msg.replace(self.token, "[REDACTED]")
            raise MCPError(f"HTTP error: {error_msg}")

    def _parse_response_body(self, response: httpx.Response) -> dict:
        """Parse response body, handling both JSON and SSE formats.

        Args:
            response: httpx.Response object

        Returns:
            Parsed JSON-RPC response dict

        Raises:
            MCPError: If parsing fails
        """
        content_type = response.headers.get("content-type", "")
        text = response.text

        # Handle SSE format (text/event-stream)
        if "text/event-stream" in content_type:
            return self._parse_sse(text)

        # Handle plain JSON
        try:
            return response.json()
        except json.JSONDecodeError as e:
            raise MCPError(f"Failed to parse JSON response: {e}")

    def _parse_sse(self, sse_text: str) -> dict:
        """Parse SSE stream to extract first data: line as JSON.

        Args:
            sse_text: SSE stream text

        Returns:
            Parsed JSON from first non-empty data: line

        Raises:
            MCPError: If no valid data line found
        """
        for line in sse_text.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()  # Remove "data:" prefix
                if not payload:
                    continue  # Skip empty data lines (priming events)
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue  # Skip unparseable lines

        raise MCPError("No valid JSON-RPC payload found in SSE stream")

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

        response = self._post(payload, session_id=self._session_id)
        data = self._parse_response_body(response)

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
        text_content = content[0].get("text", "")

        # Try to parse as JSON for structured results; if that fails,
        # return raw text (e.g., config dumps from get_junos_config)
        try:
            return json.loads(text_content)
        except json.JSONDecodeError:
            # Not JSON - return raw text with smart truncation
            MAX_TEXT_LEN = 8000
            if len(text_content) > MAX_TEXT_LEN:
                # Check if tool args specified a config_path (already scoped)
                config_path = arguments.get("config_path") or arguments.get("path")
                truncated, was_truncated = smart_truncate_config(
                    text_content,
                    MAX_TEXT_LEN,
                    config_path=config_path,
                    domain=None,  # Domain passed per-scenario, not available here
                )
                return truncated
            return text_content


def smart_truncate_config(
    text: str,
    max_len: int,
    config_path: str | None = None,
    domain: str | None = None,
) -> tuple[str, bool]:
    """Smart truncation: extract relevant sections or use head+tail strategy.

    Args:
        text: Full config text
        max_len: Maximum length before truncation needed
        config_path: If tool args specified a path (already scoped), keep as-is
        domain: Scenario domain hint for section extraction (e.g., "system ntp", "security policies")

    Returns:
        Tuple of (truncated_text, was_truncated_flag)
    """
    if len(text) <= max_len:
        return text, False

    # If config_path specified in tool args, the result is already scoped - keep as-is
    if config_path:
        return text, False

    # Try domain-specific section extraction
    if domain:
        sections = _extract_config_sections(text, domain)
        if sections and len(sections) <= max_len:
            logger.info(f"Smart truncation: extracted {domain} section ({len(sections)} chars)")
            return sections, True

    # Fallback: head + tail with marker
    head_len = max_len // 2
    tail_len = max_len // 2
    truncated = (
        text[:head_len] +
        f"\n\n... [truncated {len(text) - max_len} chars] ...\n\n" +
        text[-tail_len:]
    )
    logger.info(f"Smart truncation: head+tail strategy ({len(text)} → {len(truncated)} chars)")
    return truncated, True


def _extract_config_sections(text: str, domain: str) -> str:
    """Extract hierarchical config sections relevant to domain.

    Args:
        text: Full config text
        domain: Domain keyword (e.g., "system ntp", "security policies", "system name-server")

    Returns:
        Extracted sections or empty string if not found
    """
    # Junos hierarchical config extraction
    domain_lower = domain.lower()
    lines = text.split("\n")
    extracted = []
    in_section = False
    indent_level = 0
    base_indent = 0

    for line in lines:
        stripped = line.lstrip()
        current_indent = len(line) - len(stripped)

        # Check if this line starts a relevant section
        if any(keyword in stripped.lower() for keyword in domain_lower.split()):
            in_section = True
            base_indent = current_indent
            extracted.append(line)
        elif in_section:
            # Continue capturing while indented deeper than base
            if current_indent > base_indent or not stripped:
                extracted.append(line)
            else:
                # Exited the section
                in_section = False

    return "\n".join(extracted) if extracted else ""


def filter_safe_devices(devices: list[dict] | list[str]) -> list[dict] | list[str]:
    """Filter device list to exclude prod/outpost devices (safety rail).

    Args:
        devices: List of device dicts with 'name' key, or bare list of device name strings

    Returns:
        Filtered list (same type as input) excluding devices with 'prod' or 'outpost' in name (case-insensitive)
    """
    forbidden_patterns = ["prod", "outpost"]
    safe_devices = []

    for device in devices:
        # Handle both string and dict formats
        if isinstance(device, str):
            name_lower = device.lower()
        elif isinstance(device, dict):
            name_lower = device["name"].lower()
        else:
            continue  # Skip unknown types

        if not any(pattern in name_lower for pattern in forbidden_patterns):
            safe_devices.append(device)

    return safe_devices


def probe_device_liveness(mcp_client: MCPClient, device: str, timeout: int = 10) -> None:
    """Probe device liveness by calling gather_device_facts.

    Args:
        mcp_client: MCP client instance
        device: Device name to probe
        timeout: Probe timeout in seconds (default 10)

    Raises:
        MCPError: If device is unreachable or probe fails
    """
    # Temporarily override client timeout for this probe
    original_timeout = mcp_client.timeout
    mcp_client.timeout = timeout

    try:
        # gather_device_facts is a quick check that requires the device to respond
        mcp_client.call_tool("gather_device_facts", {"device": device})
    finally:
        # Restore original timeout
        mcp_client.timeout = original_timeout


class AgenticRunner:
    """Agentic loop runner: executes tool calls against real devices via MCP."""

    # Hard-coded safety rail: tools that must never be executed
    # Covers BOTH vendors' approve/apply/commit surface
    FORBIDDEN_MUTATING_TOOLS = {
        "approve_panos_change_set",
        "apply_panos_change_set",
        "commit_panos_candidate",
        "approve_junos_change_set",
        "apply_junos_change_set",
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
        change_set_ids = []  # Track created change-sets for teardown
        vendor = scenario.get("vendor", "unknown")
        final_message = None  # Track model's final text response

        # Substitute {{device}} placeholder in prompt
        prompt = scenario["prompt"]
        if "{{device}}" in prompt:
            prompt = prompt.replace("{{device}}", self.device)
        else:
            # Legacy fallback: warn and substitute known hardcoded names
            if any(name in prompt for name in ["demo-srx", "demo-pa", "demo-fw", "panosvm"]):
                logger.warning(
                    f"Scenario {scenario.get('id', 'unknown')} has hardcoded device name without {{{{device}}}} placeholder"
                )
                # Fallback substitution
                prompt = (prompt
                    .replace("demo-srx", self.device)
                    .replace("demo-pa", self.device)
                    .replace("demo-fw", self.device)
                    .replace("panosvm", self.device))

        messages = [{"role": "user", "content": prompt}]
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
                    final_message = message.get("content", "")
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

                            # Track change-set IDs for teardown
                            if tool_name in ("create_junos_change_set", "create_panos_change_set"):
                                if isinstance(tool_result, dict) and "change_set_id" in tool_result:
                                    change_set_ids.append(tool_result["change_set_id"])
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
                    "scoring_mode": score_result.get("scoring_mode", "unknown"),
                    "started": started,
                    "finished": finished,
                    "transcript": transcript,
                    "final_message": final_message,
                }

        except Exception as e:
            logger.error(f"Scenario {scenario['id']} failed: {e}")
            finished = datetime.now(timezone.utc).isoformat()
            return {
                "id": scenario["id"],
                "pass": False,
                "reason": f"runner_error: {e}",
                "scoring_mode": scenario.get("scoring", "outcome_lenient"),
                "started": started,
                "finished": finished,
                "transcript": transcript,
                "final_message": final_message,
            }
        finally:
            # OUTCOME SCORING: Capture staged state BEFORE teardown
            staged_diff = None
            if scenario.get("scoring") == "outcome" and change_set_ids:
                vendor = scenario.get("vendor", "junos")
                try:
                    if vendor == "junos":
                        # Junos: create_junos_change_set stages in MCP server store,
                        # NOT device candidate. Capture via get_junos_change_set_status.
                        captured_statuses = []
                        for cs_id in change_set_ids:
                            try:
                                status_result = self.mcp_client.call_tool(
                                    "get_junos_change_set_status",
                                    {"change_set_id": cs_id, "device": self.device}
                                )
                                # Serialize the full change-set structure (includes payload/actions)
                                if isinstance(status_result, dict):
                                    status_text = json.dumps(status_result, indent=2)
                                else:
                                    status_text = str(status_result)
                                captured_statuses.append(status_text)
                                logger.debug(f"Captured Junos change-set {cs_id}: {len(status_text)} chars")
                            except Exception as cs_error:
                                logger.warning(f"Failed to capture change-set {cs_id}: {cs_error}")
                                # Continue capturing other change-sets
                                continue

                        # Concatenate all captured statuses
                        staged_diff = "\n\n".join(captured_statuses) if captured_statuses else None

                    elif vendor == "panos":
                        # PAN-OS: change-sets DO stage to device candidate
                        diff_result = self.mcp_client.call_tool(
                            "diff_panos_candidate",
                            {"device": self.device}
                        )
                        if isinstance(diff_result, dict):
                            staged_diff = diff_result.get("diff", "")
                        else:
                            staged_diff = str(diff_result) if diff_result else ""
                        logger.debug(f"Captured PAN-OS staged diff: {len(staged_diff)} chars")
                except Exception as capture_error:
                    logger.warning(f"Failed to capture staged state: {capture_error}")
                    staged_diff = None

            # SAFETY RAIL: Discard all created change-sets, even on error paths
            # (absolute requirement per brief: "every change-set discarded in teardown even on error")
            if change_set_ids:
                vendor = scenario.get("vendor", "junos")
                logger.info(
                    f"Teardown: discarding {len(change_set_ids)} change-set(s) for {vendor} scenario {scenario['id']}"
                )

                for cs_id in change_set_ids:
                    try:
                        if vendor == "panos":
                            # PAN-OS: discard candidate config
                            self.mcp_client.call_tool(
                                "discard_panos_candidate",
                                {"device": self.device}
                            )
                            logger.debug(f"Teardown: discarded PAN-OS candidate for change-set {cs_id}")
                        elif vendor == "junos":
                            # Junos: discard candidate config (explicit tool exists)
                            self.mcp_client.call_tool(
                                "discard_candidate",
                                {"device": self.device, "timeout": 60}
                            )
                            logger.debug(f"Teardown: discarded Junos candidate for change-set {cs_id}")
                        else:
                            logger.warning(f"Teardown: unknown vendor '{vendor}', cannot discard change-set {cs_id}")
                    except Exception as teardown_error:
                        # Log teardown failures but don't propagate - scenario result already determined
                        logger.warning(
                            f"Teardown failed for change-set {cs_id} ({vendor}): {teardown_error}"
                        )

        # Score the realized transcript (with staged_diff for outcome mode)
        score_result = scoring.score_scenario(
            scenario,
            transcript,
            staged_diff=staged_diff,
            final_message=final_message,
        )
        finished = datetime.now(timezone.utc).isoformat()

        result = {
            "id": scenario["id"],
            "pass": score_result["pass"],
            "reason": score_result["reason"],
            "scoring_mode": score_result.get("scoring_mode", "unknown"),
            "started": started,
            "finished": finished,
            "transcript": transcript,
            "final_message": final_message,
        }

        # Include outcome evidence in result for audit trail
        if "outcome_evidence" in score_result:
            result["outcome_evidence"] = score_result["outcome_evidence"]

        return result


class OllamaHealthProbe:
    """Health probe for Ollama serving stack to detect degradation."""

    def __init__(self, base_url: str):
        """Initialize health probe.

        Args:
            base_url: Ollama base URL (e.g. http://inference.example.com:11434)
        """
        self.base_url = base_url.rstrip("/")

    def get_running_models(self) -> list[dict]:
        """Query /api/ps for currently loaded models (diagnostic only).

        Returns:
            List of running model dicts from Ollama

        Raises:
            httpx.HTTPError: If request fails
        """
        response = httpx.get(f"{self.base_url}/api/ps", timeout=10.0)
        response.raise_for_status()
        data = response.json()
        return data.get("models", [])

    def probe_responsiveness(self, model: str, timeout: float = 30.0) -> None:
        """Probe model responsiveness with minimal completion request.

        Args:
            model: Model identifier to probe
            timeout: Probe timeout in seconds

        Raises:
            TimeoutError: If probe fails or times out
            httpx.HTTPError: If request fails
        """
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": model,
            "prompt": "ping",
            "stream": False,
            "options": {
                "num_predict": 1,
                "temperature": 0,
            }
        }

        try:
            response = httpx.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            logger.debug(f"Model {model} is responsive")
        except (httpx.TimeoutException, httpx.HTTPError) as e:
            raise TimeoutError(f"Model {model} responsiveness probe failed: {e}")

    def wait_for_settle(self, model: str, timeout: float = 60.0) -> None:
        """Wait for Ollama to be responsive (detects wedged/zombie server).

        Args:
            model: Model identifier to probe
            timeout: Maximum time to wait in seconds

        Raises:
            TimeoutError: If server is unresponsive after retry
        """
        # Log which models are loaded (diagnostic only, not gating)
        try:
            models = self.get_running_models()
            if models:
                model_names = [m.get("name", "unknown") for m in models]
                logger.debug(f"Loaded models: {', '.join(model_names)}")
            else:
                logger.debug("No models currently loaded")
        except Exception as e:
            logger.debug(f"Could not query /api/ps: {e}")

        # Responsiveness probe: try once, retry once after 10s, then fail
        try:
            self.probe_responsiveness(model, timeout=30.0)
            return
        except TimeoutError as e:
            logger.warning(f"First responsiveness probe failed: {e}, retrying in 10s")
            time.sleep(10.0)

        # Retry
        try:
            self.probe_responsiveness(model, timeout=30.0)
            logger.info(f"Model {model} became responsive after retry")
        except TimeoutError as e:
            raise TimeoutError(f"Ollama unresponsive after retry: {e}")


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

        # Exponential backoff on 5xx errors
        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                response = httpx.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                # Retry on 5xx server errors
                if e.response.status_code >= 500 and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"LLM server error {e.response.status_code}, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                raise


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
    setup_token: str | None = None,
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
        mcp_token: MCP bearer token (AGENT token - commitless)
        device: Device name to use for tool calls
        setup_token: Optional OPERATOR-scoped token for fault setup/teardown
        temperature: Sampling temperature
        num_predict: Max tokens (Ollama-specific)
        keep_alive: Keep-alive duration (Ollama-specific)

    Returns:
        Manifest dict with devices_touched field
    """
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()

    # Initialize clients (two-token separation: agent token != setup token)
    llm_client = LLMClient(
        endpoint=endpoint,
        num_predict=num_predict,
        keep_alive=keep_alive,
    )
    mcp_client = MCPClient(endpoint=mcp_endpoint, token=mcp_token)  # AGENT token only

    # Optional setup client (OPERATOR token - can commit)
    setup_client = None
    if setup_token:
        setup_client = MCPClient(endpoint=mcp_endpoint, token=setup_token)

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

        # Probe Ollama responsiveness between scenarios
        if health_probe and i > 0:
            logger.debug("Probing Ollama responsiveness...")
            try:
                health_probe.wait_for_settle(model=model, timeout=60.0)
            except TimeoutError as e:
                # Unresponsive server is a structured failure - fail the scenario
                logger.error(f"Ollama unresponsive before scenario {scenario['id']}: {e}")
                result = {
                    "id": scenario["id"],
                    "pass": False,
                    "reason": f"ollama_unresponsive: {e}",
                    "scoring_mode": scenario.get("scoring", "outcome_lenient"),
                    "started": datetime.now(timezone.utc).isoformat(),
                    "finished": datetime.now(timezone.utc).isoformat(),
                    "transcript": [],
                }
                results.append(result)
                continue
            except Exception as e:
                logger.warning(f"Health probe non-timeout failure: {e}")

        # Apply scenario fault setup if setup client exists and scenario has setup block
        setup_applied = False
        if setup_client and scenario.get("setup"):
            setup_config = scenario["setup"]
            # Substitute {{device}} in setup block
            if "{{device}}" in setup_config:
                setup_config = setup_config.replace("{{device}}", device)

            logger.info(f"Applying fault setup for scenario {scenario['id']}")
            try:
                setup_client.call_tool(
                    "load_and_commit_config",
                    {
                        "device": device,
                        "config": setup_config,
                        "config_format": "set",
                        "commit_comment": f"mechubbench scenario setup {scenario['id']} — auto-rollback",
                    }
                )
                setup_applied = True
                logger.info(f"Fault setup committed for {scenario['id']}")
            except MCPError as e:
                logger.error(f"Fault setup failed for {scenario['id']}: {e}")
                result = {
                    "id": scenario["id"],
                    "pass": False,
                    "reason": f"setup_failed: {e}",
                    "started": datetime.now(timezone.utc).isoformat(),
                    "finished": datetime.now(timezone.utc).isoformat(),
                    "transcript": [],
                    "final_message": None,
                }
                results.append(result)
                continue

        try:
            # Run scenario
            result = agentic_runner.run_scenario(scenario, model, tools, temperature)
            results.append(result)

            status = "PASS" if result["pass"] else "FAIL"
            logger.info(f"  Result: {status} - {result['reason']}")

        finally:
            # ALWAYS roll back setup if it was applied
            if setup_applied:
                logger.info(f"Rolling back fault setup for {scenario['id']}")
                try:
                    # Rollback to version 1 (before setup)
                    setup_client.call_tool(
                        "rollback_config",
                        {"device": device, "version": 1, "commit": True}
                    )

                    # Verify clean rollback (candidate vs running, version=0)
                    diff_result = mcp_client.call_tool("junos_config_diff", {"device": device, "version": 0})
                    if isinstance(diff_result, dict):
                        diff_text = diff_result.get("diff", "")
                    else:
                        diff_text = str(diff_result) if diff_result else ""

                    if diff_text.strip():
                        logger.error(
                            f"ABORT: Rollback verification failed - candidate diff not empty after rollback. "
                            f"Device {device} may be in dirty state. Aborting sweep."
                        )
                        raise RuntimeError(f"Rollback verification failed for {scenario['id']}: dirty candidate")

                    logger.info(f"Fault setup rolled back cleanly for {scenario['id']}")

                except Exception as e:
                    logger.error(
                        f"ABORT: Rollback failed for {scenario['id']}: {e}. "
                        f"Device {device} is in dirty state. Aborting sweep."
                    )
                    raise RuntimeError(f"Rollback failure - aborting sweep to prevent cascading faults: {e}")

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
