"""Bench guard: golden drift check, fault planting, baseline and restore.

Calls use the OPERATOR MCP client (rollback and commit scope), except that
`restore` confirms pending change-set ops with the AGENT client: only the
opener of an op may confirm it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from mechubbench.inventory import BenchDevice
from mechubbench.runner import (
    MCPError,
    fetch_config_fingerprint,
    prepare_setup_config,
    verify_baseline_restored,
)

# `0   2026-11-25 23:28:11 UTC by netconf via netconf commit confirmed, ...`
# The XML wrapper and indented comment lines ("    Confirming commit: ...")
# never match.
_COMMIT_LINE = re.compile(r"^\d+\s+(\d{4}-\d{2}-\d{2} \d\d:\d\d:\d\d .*?)\s*$")
_MAX_ROLLBACK = 49  # Junos keeps rollback 0..49


@dataclass(frozen=True)
class Baseline:
    """Pre-attempt identity of one device's committed config."""

    device: str
    fingerprint: str
    top_commit: str


def _fingerprint(mcp, device: str) -> str:
    fingerprint = fetch_config_fingerprint(mcp, device)
    if not fingerprint:
        raise RuntimeError(f"config fingerprint unavailable for {device}")
    return fingerprint


def _commit_entries(mcp, device: str) -> list[str]:
    """Return `show system commit` entries, newest first, without the index."""
    output = mcp.call_tool(
        "execute_junos_command",
        {"router_name": device, "command": "show system commit"},
        truncate=False,
    )
    text = output if isinstance(output, str) else json.dumps(output)
    entries = []
    for line in text.splitlines():
        match = _COMMIT_LINE.match(line)
        if match:
            entries.append(match.group(1))
    return entries


def record_golden(mcp, inventory: list[BenchDevice], path: Path) -> None:
    """Write every bench device's config fingerprint to `path` as JSON.

    Raises:
        RuntimeError: If any device's fingerprint is unavailable.
    """
    golden = {device.name: _fingerprint(mcp, device.name) for device in inventory}
    path.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")


def check_golden(
    mcp, inventory: list[BenchDevice], golden: dict[str, str]
) -> list[str]:
    """Return the names of devices whose fingerprint differs from `golden`.

    A device with no golden entry, or whose fingerprint cannot be read,
    counts as drifted.
    """
    return [
        device.name
        for device in inventory
        if device.name not in golden
        or fetch_config_fingerprint(mcp, device.name) != golden[device.name]
    ]


def capture_baseline(mcp, device: str) -> Baseline:
    """Record a device's fingerprint and newest commit entry before an attempt.

    Raises:
        RuntimeError: If the fingerprint or commit history cannot be read.
    """
    try:
        entries = _commit_entries(mcp, device)
    except MCPError as exc:
        raise RuntimeError(
            f"baseline commit history failed on {device}: {exc}"
        ) from None
    if not entries:
        raise RuntimeError(f"no commit history on {device}")
    return Baseline(device, _fingerprint(mcp, device), entries[0])


def plant_fault(mcp, device: str, fault: str) -> None:
    """Commit a scenario fault (set lines) on `device` with a plain commit.

    Raises:
        RuntimeError: If the commit fails; the device state is then unknown
            and the caller must still run `restore`.
    """
    config_text = prepare_setup_config(fault, device)
    if not config_text:
        return
    try:
        mcp.call_tool(
            "load_and_commit_config",
            {
                "router_name": device,
                "config_text": config_text,
                "config_format": "set",
                "commit_comment": "mechubbench fault - removed by restore",
                # 0 = plain commit; a confirmed window would revert the fault.
                "confirm_timeout_mins": 0,
            },
        )
    except MCPError as exc:
        raise RuntimeError(f"fault commit failed on {device}: {exc}") from None


def commits_since(mcp, baseline: Baseline) -> list[str]:
    """Return `show system commit` entries newer than the baseline, newest first.

    Entry indexes shift on every commit, so the baseline is found by its
    timestamp and user text.

    Raises:
        RuntimeError: If the baseline entry is no longer in the history.
    """
    entries = _commit_entries(mcp, baseline.device)
    # shortcut: two commits in the same second by the same user/client look
    # identical; the restore fingerprint check catches a miscount.
    if baseline.top_commit not in entries:
        raise RuntimeError(
            f"baseline commit {baseline.top_commit!r} not found on {baseline.device}"
        )
    return entries[: entries.index(baseline.top_commit)]


def _confirm_awaiting_operations(mcp, agent_mcp, device: str) -> None:
    """Confirm change-set operations left awaiting confirmation on `device`.

    An op left AwaitingConfirmation blocks every later apply on the device.
    The operator lists and reads status (no owner check there), but only the
    opener may confirm, so the confirm goes through `agent_mcp`.
    """
    change_sets = mcp.call_tool(
        "list_junos_change_sets", {"router_name": device}, truncate=False
    )
    # The server returns a JSON array of records; fail closed on anything else.
    if not isinstance(change_sets, list):
        raise RuntimeError(f"unexpected list_junos_change_sets result on {device}")
    for change_set in change_sets:
        if change_set.get("state") != "applied" or not change_set.get("operation_id"):
            continue
        status = mcp.call_tool(
            "get_junos_change_set_status",
            {"change_set_id": change_set["id"], "router_name": device},
            truncate=False,
        )
        if not isinstance(status, dict):
            raise RuntimeError(
                f"unexpected change-set status for {change_set['id']} on {device}"
            )
        if status.get("commit_confirmation") != "awaiting_confirmation":
            continue
        if agent_mcp is None:
            raise RuntimeError(
                f"change set {change_set['id']} awaits confirmation on {device} "
                "and no agent client was given to confirm it"
            )
        agent_mcp.call_tool(
            "confirm_junos_change_set",
            {
                "router_name": device,
                "operation_id": status.get("operation_id")
                or change_set["operation_id"],
            },
        )


def _rollback_to_baseline(mcp, baseline: Baseline) -> None:
    device = baseline.device
    versions_back = len(commits_since(mcp, baseline))
    if versions_back == 0:
        return
    try:
        _rollback(mcp, device, versions_back)
    except MCPError as exc:
        # The server refuses a rollback inside a direct commit-confirmed
        # window; close it, then roll back past the confirming commit too.
        if "commit_confirmed_active" not in str(exc):
            raise
        mcp.call_tool("confirm_commit", {"device": device})
        _rollback(mcp, device, len(commits_since(mcp, baseline)))


def _rollback(mcp, device: str, versions_back: int) -> None:
    if versions_back > _MAX_ROLLBACK:
        raise RuntimeError(f"{versions_back} commits since baseline on {device}")
    mcp.call_tool(
        "rollback_config",
        {
            "router_name": device,
            "version": versions_back,
            "commit": True,
            "confirm_timeout_mins": 0,
            "commit_comment": "mechubbench restore to baseline",
        },
    )


def restore(mcp, baseline: Baseline, agent_mcp=None) -> None:
    """Return a device to its baseline, whatever the attempt did.

    Confirms change-set ops awaiting confirmation (with `agent_mcp`, the
    opener; required when one is pending), then rolls back every commit
    since the baseline in one plain commit, discards any uncommitted
    candidate, then checks the fingerprint.
    Safe to call after an attempt that raised midway.

    Raises:
        RuntimeError: On any failure; the device must not be reused.
    """
    try:
        _confirm_awaiting_operations(mcp, agent_mcp, baseline.device)
        _rollback_to_baseline(mcp, baseline)
        # An uncommitted candidate left by the attempt would fail the
        # fingerprint check; discarding a clean candidate is a no-op.
        mcp.call_tool("discard_candidate", {"router_name": baseline.device})
    except (MCPError, KeyError, AttributeError, TypeError) as exc:
        raise RuntimeError(f"restore of {baseline.device} failed: {exc!r}") from None
    # The candidate fingerprint is the real gate: junos_config_diff v0
    # compares against rollback 0 and is always empty.
    verify_baseline_restored(
        mcp, baseline.device, baseline.fingerprint, f"restore {baseline.device}"
    )
