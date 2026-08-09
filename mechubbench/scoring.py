"""Pure scoring functions: match expected/forbidden tool calls in transcripts."""

from __future__ import annotations

import json


def score_scenario(scenario: dict, transcript: list[dict]) -> dict:
    """Score a scenario against an agent's tool-call transcript.

    Args:
        scenario: Scenario dict with expected_calls, forbidden_calls, scoring
        transcript: List of {tool, args} dicts from the agent run

    Returns:
        Dict with keys: pass (bool), reason (str)
    """
    scoring_method = scenario.get(
        "scoring", "all_expected_present_and_ordered_no_forbidden"
    )
    if scoring_method != "all_expected_present_and_ordered_no_forbidden":
        return {"pass": False, "reason": f"unknown scoring method: {scoring_method}"}

    expected = scenario.get("expected_calls", [])
    forbidden = scenario.get("forbidden_calls", [])

    # Check for forbidden calls first
    for call in transcript:
        for forbidden_spec in forbidden:
            if call["tool"] == forbidden_spec["tool"]:
                return {
                    "pass": False,
                    "reason": f"forbidden call: {forbidden_spec['tool']}",
                }

    # Check expected calls are present and in order
    if not expected:
        # No expectations -> pass if no forbidden calls were found
        return {"pass": True, "reason": "no_expected_calls"}

    expected_index = 0
    for call in transcript:
        if expected_index >= len(expected):
            break

        current_expected = expected[expected_index]
        if call["tool"] != current_expected["tool"]:
            continue

        # Tool name matches, check args_contains if specified
        args_contains = current_expected.get("args_contains", [])
        if args_contains:
            args_str = json.dumps(call.get("args", {}))
            missing = [s for s in args_contains if s not in args_str]
            if missing:
                return {
                    "pass": False,
                    "reason": (
                        f"args_contains mismatch: missing {missing} "
                        f"in call to {call['tool']}"
                    ),
                }

        # This expected call is satisfied
        expected_index += 1

    if expected_index < len(expected):
        # Check if missing calls appear later (out of order) vs truly absent
        missing_tools = [e["tool"] for e in expected[expected_index:]]
        transcript_tools = [c["tool"] for c in transcript]
        out_of_order = any(tool in transcript_tools for tool in missing_tools)
        if out_of_order:
            return {
                "pass": False,
                "reason": f"expected calls out of order: {missing_tools}",
            }
        return {
            "pass": False,
            "reason": f"missing expected calls: {missing_tools}",
        }

    return {"pass": True, "reason": "all_expected_present_and_ordered"}
