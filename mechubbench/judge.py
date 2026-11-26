"""Scripted judge: scores a headless-client attempt from answer, device and commits."""

from __future__ import annotations

import json
import re

from mechubbench.runner import MCPError
from mechubbench.scoring import score_scenario


class JudgeInfraError(Exception):
    """MCP server error or unreachable device while judging; not a model failure."""


def _check_device(check: dict, device: str | None, mcp) -> str | None:
    """Run one device check; return a failure reason or None if it passes."""
    command = check["command"]
    target = check.get("device") or device
    try:
        result = mcp.call_tool(
            "execute_junos_command",
            {"router_name": target, "command": command},
            truncate=False,
        )
    except MCPError as exc:
        # MCPError messages already have the bearer token redacted.
        raise JudgeInfraError(
            f"device check {command!r} on {target} failed: {str(exc)[:200]}"
        ) from exc
    output = result if isinstance(result, str) else json.dumps(result)

    for needle in check.get("must_contain", []):
        if needle not in output:
            return f"{command!r}: output missing {needle!r}"
    pattern = check.get("must_match")
    if pattern and not re.search(pattern, output):
        return f"{command!r}: output did not match {pattern!r}"
    for needle in check.get("must_not_contain", []):
        if needle in output:
            return f"{command!r}: output contains forbidden {needle!r}"
    return None


def judge_attempt(
    scenario: dict,
    device: str | None,
    final_message: str,
    mcp,
    commits_since_baseline: list[str],
) -> tuple[bool, str]:
    """Judge one attempt by script.

    Checks, in order: report keywords in the final message (a per-device
    list in `outcome.report_contains_by_device` replaces `report_contains`;
    a nested list is any-of; `outcome.report_order` keywords must first
    appear in that order), device state via `device_checks` (a check
    with `devices` runs only for those devices), and (when
    `commit_confirmed_required`) that some commit since the baseline was a
    `commit confirmed`.

    Args:
        scenario: Scenario dict (outcome.report_contains, device_checks, ...).
        device: Target device for per_device scenarios (checks may override it).
        final_message: The client's final answer text.
        mcp: Operator MCPClient (anything with call_tool).
        commits_since_baseline: `show system commit` entry lines newer than baseline.

    Returns:
        (passed, reason)

    Raises:
        JudgeInfraError: MCP error while running a device check (infra, not a fail).
    """
    outcome = scenario.get("outcome", {})
    report_contains = outcome.get("report_contains_by_device", {}).get(
        device, outcome.get("report_contains", [])
    )
    if report_contains:
        view = {"scoring": "outcome", "outcome": {"report_contains": report_contains}}
        scored = score_scenario(view, [], final_message=final_message)
        if not scored["pass"]:
            return False, scored["reason"]
    order = outcome.get("report_order", [])
    if order:
        message = (final_message or "").lower()
        positions = [message.find(keyword.lower()) for keyword in order]
        if -1 in positions or positions != sorted(positions):
            return False, f"report does not name {order} in that order"

    for check in scenario.get("device_checks", []):
        if "devices" in check and device not in check["devices"]:
            continue
        failure = _check_device(check, device, mcp)
        if failure:
            return False, failure

    if scenario.get("commit_confirmed_required") and not any(
        "commit confirmed" in entry for entry in commits_since_baseline
    ):
        return False, "no commit confirmed found since baseline"

    return True, "all checks passed"
