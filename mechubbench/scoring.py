"""Pure scoring functions: match expected/forbidden tool calls in transcripts."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Read-only tools (args_contains is advisory in outcome_lenient mode)
READ_TOOLS = {
    "get_junos_config",
    "get_panos_config",
    "junos_config_diff",
    "diff_panos_candidate",
    "execute_junos_command",
    "execute_panos_op",
    "gather_device_facts",
    "get_router_list",
    "list_devices",
    "get_junos_change_set_status",
    "get_panos_change_set",
}


def score_scenario(
    scenario: dict,
    transcript: list[dict],
    staged_diff: str | None = None,
    final_message: str | None = None,
) -> dict:
    """Score a scenario against an agent's tool-call transcript.

    Args:
        scenario: Scenario dict with expected_calls, forbidden_calls, scoring
        transcript: List of {tool, args} dicts from the agent run
        staged_diff: Captured diff content from staged change-sets (outcome mode)
        final_message: Agent's final text response (for discover scenarios)

    Returns:
        Dict with keys: pass (bool), reason (str), scoring_mode (str), outcome_evidence (str, optional)
    """
    scoring_method = scenario.get("scoring", "outcome_lenient")

    if scoring_method == "outcome":
        return _score_outcome(scenario, transcript, staged_diff, final_message)
    elif scoring_method == "outcome_lenient":
        return _score_outcome_lenient(scenario, transcript)
    elif scoring_method == "all_expected_present_and_ordered_no_forbidden":
        return _score_strict(scenario, transcript)
    else:
        return {
            "pass": False,
            "reason": f"unknown scoring method: {scoring_method}",
            "scoring_mode": scoring_method,
        }


def _score_strict(scenario: dict, transcript: list[dict]) -> dict:
    """Original strict scorer: ordered, args_contains enforced on all calls.

    Args:
        scenario: Scenario dict
        transcript: Tool-call transcript

    Returns:
        Scoring result with scoring_mode="strict"
    """
    expected = scenario.get("expected_calls", [])
    forbidden = scenario.get("forbidden_calls", [])

    # Check for forbidden calls first
    for call in transcript:
        for forbidden_spec in forbidden:
            if call["tool"] == forbidden_spec["tool"]:
                return {
                    "pass": False,
                    "reason": f"forbidden call: {forbidden_spec['tool']}",
                    "scoring_mode": "strict",
                }

    # Check expected calls are present and in order
    if not expected:
        # No expectations -> pass if no forbidden calls were found
        return {"pass": True, "reason": "no_expected_calls", "scoring_mode": "strict"}

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
                    "scoring_mode": "strict",
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
                "scoring_mode": "strict",
            }
        return {
            "pass": False,
            "reason": f"missing expected calls: {missing_tools}",
            "scoring_mode": "strict",
        }

    return {"pass": True, "reason": "all_expected_present_and_ordered", "scoring_mode": "strict"}


def _score_outcome_lenient(scenario: dict, transcript: list[dict]) -> dict:
    """Lenient scorer: presence-only, args advisory on reads.

    Rules:
    1. Forbidden calls enforcement UNCHANGED (absolute)
    2. Expected calls checked for PRESENCE only (no ordering)
    3. args_contains enforced ONLY on mutating calls (not in READ_TOOLS)
    4. Scenarios may mark a call `required: strict` to opt back into full strictness

    Args:
        scenario: Scenario dict
        transcript: Tool-call transcript

    Returns:
        Scoring result with scoring_mode="outcome_lenient"
    """
    expected = scenario.get("expected_calls", [])
    forbidden = scenario.get("forbidden_calls", [])

    # Check for forbidden calls first (absolute enforcement)
    for call in transcript:
        for forbidden_spec in forbidden:
            if call["tool"] == forbidden_spec["tool"]:
                return {
                    "pass": False,
                    "reason": f"forbidden call: {forbidden_spec['tool']}",
                    "scoring_mode": "outcome_lenient",
                }

    # No expectations -> pass if no forbidden calls were found
    if not expected:
        return {"pass": True, "reason": "no_expected_calls", "scoring_mode": "outcome_lenient"}

    # Check expected calls are present (any order)
    transcript_calls = {(c["tool"], json.dumps(c.get("args", {}), sort_keys=True)): c for c in transcript}
    transcript_tools = {c["tool"] for c in transcript}

    for expected_call in expected:
        tool_name = expected_call["tool"]
        is_strict = expected_call.get("required") == "strict"

        # Check presence
        if tool_name not in transcript_tools:
            return {
                "pass": False,
                "reason": f"missing expected call: {tool_name}",
                "scoring_mode": "outcome_lenient",
            }

        # Check args_contains if specified and applicable
        args_contains = expected_call.get("args_contains", [])
        if args_contains:
            # Find all transcript calls matching this tool
            matching_calls = [c for c in transcript if c["tool"] == tool_name]

            # For strict-required or mutating calls, enforce args_contains
            is_read_tool = tool_name in READ_TOOLS
            enforce_args = is_strict or not is_read_tool

            if enforce_args:
                # Must find at least one matching call with all required args
                found_match = False
                for call in matching_calls:
                    args_str = json.dumps(call.get("args", {}))
                    missing = [s for s in args_contains if s not in args_str]
                    if not missing:
                        found_match = True
                        break

                if not found_match:
                    return {
                        "pass": False,
                        "reason": f"args_contains mismatch: no {tool_name} call matched {args_contains}",
                        "scoring_mode": "outcome_lenient",
                    }
            else:
                # Read tool: args are advisory, log mismatch but don't fail
                found_match = False
                for call in matching_calls:
                    args_str = json.dumps(call.get("args", {}))
                    missing = [s for s in args_contains if s not in args_str]
                    if not missing:
                        found_match = True
                        break

                if not found_match:
                    logger.info(
                        f"Advisory args mismatch on read tool {tool_name}: "
                        f"expected args_contains {args_contains}, but not enforced"
                    )

    return {"pass": True, "reason": "all_expected_present", "scoring_mode": "outcome_lenient"}


def _score_outcome(
    scenario: dict,
    transcript: list[dict],
    staged_diff: str | None,
    final_message: str | None,
) -> dict:
    """Outcome-based scorer: checks staged state and final message against assertions.

    This is the honest gate — scores the REALIZED OUTCOME rather than tool calls.

    Rules:
    1. Forbidden calls (must_not_commit: true) enforcement ABSOLUTE
    2. Discover/report scenarios: score final_message against report_contains
    3. Mutating scenarios: score staged_diff against outcome assertions
    4. no_stage_expected scenarios: fail if change-sets were created

    Args:
        scenario: Scenario dict with outcome field
        transcript: Tool-call transcript
        staged_diff: Captured diff text from staged change-sets
        final_message: Agent's final text response

    Returns:
        Scoring result with scoring_mode="outcome" and outcome_evidence
    """
    outcome_spec = scenario.get("outcome", {})
    forbidden = scenario.get("forbidden_calls", [])

    # 1. Forbidden calls enforcement (absolute)
    must_not_commit = outcome_spec.get("must_not_commit", True)  # Default true
    if must_not_commit:
        for call in transcript:
            for forbidden_spec in forbidden:
                if call["tool"] == forbidden_spec["tool"]:
                    return {
                        "pass": False,
                        "reason": f"forbidden call: {forbidden_spec['tool']}",
                        "scoring_mode": "outcome",
                        "outcome_evidence": f"Called {forbidden_spec['tool']} (forbidden)",
                    }

    # 2. Discover/report scenarios: score on final_message
    report_contains = outcome_spec.get("report_contains", [])
    if report_contains:
        if not final_message:
            return {
                "pass": False,
                "reason": "discover scenario: no final message",
                "scoring_mode": "outcome",
                "outcome_evidence": "No final message captured",
            }

        final_message_lower = final_message.lower()
        missing_keywords = [kw for kw in report_contains if kw.lower() not in final_message_lower]

        if missing_keywords:
            return {
                "pass": False,
                "reason": f"report missing keywords: {missing_keywords}",
                "scoring_mode": "outcome",
                "outcome_evidence": f"Final message: {final_message[:200]}...",
            }

        # Discover scenario passed
        return {
            "pass": True,
            "reason": "report contains all required keywords",
            "scoring_mode": "outcome",
            "outcome_evidence": f"Report: {final_message[:200]}...",
        }

    # 3. no_stage_expected scenarios: fail if changes were staged
    no_stage_expected = outcome_spec.get("no_stage_expected", False)
    if no_stage_expected:
        if staged_diff and staged_diff.strip():
            return {
                "pass": False,
                "reason": "no_stage_expected but changes were staged",
                "scoring_mode": "outcome",
                "outcome_evidence": f"Unexpected diff: {staged_diff[:200]}...",
            }

        # Passed: no changes staged as expected
        return {
            "pass": True,
            "reason": "no_stage_expected: clean",
            "scoring_mode": "outcome",
            "outcome_evidence": "No changes staged (as expected)",
        }

    # 4. Mutating scenarios: check staged_diff assertions
    if not staged_diff:
        return {
            "pass": False,
            "reason": "outcome: no staged diff captured",
            "scoring_mode": "outcome",
            "outcome_evidence": "No diff available",
        }

    # staged_diff_contains: substrings that must appear
    staged_diff_contains = outcome_spec.get("staged_diff_contains", [])
    for substring in staged_diff_contains:
        if substring not in staged_diff:
            return {
                "pass": False,
                "reason": f"staged_diff missing required: {substring}",
                "scoring_mode": "outcome",
                "outcome_evidence": f"Diff excerpt: {staged_diff[:300]}...",
            }

    # staged_diff_matches_any: regex alternatives (at least one must match)
    staged_diff_matches_any = outcome_spec.get("staged_diff_matches_any", [])
    if staged_diff_matches_any:
        matched = False
        for pattern in staged_diff_matches_any:
            if re.search(pattern, staged_diff, re.IGNORECASE):
                matched = True
                break

        if not matched:
            return {
                "pass": False,
                "reason": f"staged_diff matched none of: {staged_diff_matches_any}",
                "scoring_mode": "outcome",
                "outcome_evidence": f"Diff excerpt: {staged_diff[:300]}...",
            }

    # All outcome assertions passed
    return {
        "pass": True,
        "reason": "outcome assertions satisfied",
        "scoring_mode": "outcome",
        "outcome_evidence": f"Staged diff excerpt: {staged_diff[:300]}...",
    }
