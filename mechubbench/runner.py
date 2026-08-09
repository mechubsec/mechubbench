"""Runner: drives scenarios through LLM with tool-calling, scores results.

Network and LLM interactions isolated here, injectable for testing.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from . import core, scoring

logger = logging.getLogger(__name__)


class LLMClient:
    """Thin adapter for OpenAI-compatible chat completions with tool calling."""

    def __init__(self, endpoint: str, timeout: int = 30):
        """Initialize client.

        Args:
            endpoint: Base URL of OpenAI-compatible endpoint (e.g. http://host:port/v1)
            timeout: Request timeout in seconds
        """
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def complete_with_tools(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> dict:
        """Run chat completion with tool definitions.

        Args:
            model: Model identifier
            messages: Chat messages in OpenAI format
            tools: Tool definitions in OpenAI format

        Returns:
            Response dict with choices, tool_calls if any
        """
        url = f"{self.endpoint}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
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
) -> dict:
    """Run a single scenario through the LLM and score it.

    Args:
        scenario: Scenario dict
        model: Model identifier
        tools: Tool definitions
        client: LLM client instance

    Returns:
        Result dict with id, pass, reason, started, finished, transcript
    """
    started = datetime.now(timezone.utc).isoformat()

    messages = [{"role": "user", "content": scenario["prompt"]}]
    openai_tools = convert_tools_to_openai_format(tools)

    try:
        response = client.complete_with_tools(model, messages, openai_tools)
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
) -> dict:
    """Run all scenarios and assemble a manifest.

    Args:
        scenarios: List of scenario dicts
        model: Model identifier
        tools: Tool definitions
        endpoint: LLM endpoint URL

    Returns:
        Manifest dict
    """
    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc).isoformat()
    client = LLMClient(endpoint)

    results = []
    for scenario in scenarios:
        logger.info(f"Running scenario: {scenario['id']}")
        result = run_scenario(scenario, model, tools, client)
        results.append(result)
        logger.info(f"  Result: {'PASS' if result['pass'] else 'FAIL'} - {result['reason']}")

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
