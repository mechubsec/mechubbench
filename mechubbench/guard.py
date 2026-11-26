"""Bench guard: golden drift check, fault planting, baseline and restore.

Every call here uses the OPERATOR MCP client (rollback and commit scope).
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
_COMMIT_LINE = re.compile(r"^\s*\d+\s+(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d .*?)\s*$")
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


def _confirm_awaiting_operations(mcp, device: str) -> None:
    """Confirm change-set operations left awaiting confirmation on `device`.

    An op left AwaitingConfirmation blocks every later apply on the device.
    """
    change_sets = mcp.call_tool(
        "list_junos_change_sets", {"router_name": device}, truncate=False
    )
    for change_set in change_sets if isinstance(change_sets, list) else []:
        if change_set.get("state") != "applied" or not change_set.get("operation_id"):
            continue
        status = mcp.call_tool(
            "get_junos_change_set_status",
            {"change_set_id": change_set["id"], "router_name": device},
            truncate=False,
        )
        if status.get("commit_confirmation") != "awaiting_confirmation":
            continue
        mcp.call_tool(
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
    if versions_back > _MAX_ROLLBACK:
        raise RuntimeError(f"{versions_back} commits since baseline on {device}")
    # A plain commit also ends any CLI-level commit-confirmed window.
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
    diff = mcp.call_tool(
        "junos_config_diff", {"router_name": device, "version": 0}, truncate=False
    )
    diff_text = diff.get("diff", "") if isinstance(diff, dict) else str(diff or "")
    if diff_text.strip():
        raise RuntimeError(f"candidate not clean after rollback on {device}")


def restore(mcp, baseline: Baseline) -> None:
    """Return a device to its baseline, whatever the attempt did.

    Confirms change-set ops awaiting confirmation, rolls back every commit
    since the baseline in one plain commit, checks the candidate is clean,
    then checks the fingerprint. With no commits since the baseline only the
    fingerprint is checked. Safe to call after an attempt that raised midway.

    Raises:
        RuntimeError: On any failure; the device must not be reused.
    """
    try:
        _confirm_awaiting_operations(mcp, baseline.device)
        _rollback_to_baseline(mcp, baseline)
    except MCPError as exc:
        raise RuntimeError(f"restore of {baseline.device} failed: {exc}") from None
    verify_baseline_restored(
        mcp, baseline.device, baseline.fingerprint, f"restore {baseline.device}"
    )
