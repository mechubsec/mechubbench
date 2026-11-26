"""Matrix orchestrator: every client x model x scenario x device, judged by script.

Each attempt runs between a baseline capture and an unconditional restore.
The manifest (schema v2) is redacted and rewritten atomically after every
attempt, so an interrupted run can be resumed.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mechubbench.clients.base import AttemptUsage, Driver
from mechubbench.guard import (
    capture_baseline,
    check_golden,
    commits_since,
    plant_fault,
    restore,
)
from mechubbench.inventory import BenchDevice, devices_for
from mechubbench.judge import JudgeInfraError, judge_attempt
from mechubbench.redact import redact_manifest
from mechubbench.runner import MCPClient, MCPError

MAX_COUNTED_ATTEMPTS = 3
MAX_INFRA_ERRORS = 3
DEFAULT_TIMEOUT_S = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def render_prompt(prompt: str, device: str | None, hosts: dict[str, str]) -> str:
    """Fill ``{{device}}`` and ``{{host_a}}``/``{{host_b}}``/``{{host_c}}``."""
    values = {
        "device": device or "",
        **{f"host_{k.lower()}": v for k, v in hosts.items()},
    }
    for name, value in values.items():
        prompt = prompt.replace("{{" + name + "}}", value)
    return prompt


def _tokens(usage: AttemptUsage | None) -> dict | None:
    if usage is None:
        return None
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_tokens,
    }


def _counted(cell: dict) -> list[dict]:
    return [a for a in cell["attempts"] if a["outcome"] != "infra_error"]


def _verdict(cell: dict) -> str | None:
    counted = _counted(cell)
    if counted and counted[-1]["outcome"] == "pass":
        return "pass"
    if len(counted) >= MAX_COUNTED_ATTEMPTS:
        return "full_failure"
    if cell["infra_errors"] >= MAX_INFRA_ERRORS:
        return "infra_error"
    return None


def _write(manifest: dict, path: Path, allowed_literals: set[str]) -> None:
    """Redact and atomically replace the manifest file."""
    redacted = redact_manifest(manifest, allowed_literals=allowed_literals)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.tmp")
    staging.write_text(json.dumps(redacted, indent=2) + "\n")
    os.replace(staging, path)


def _restore_all(operator_mcp, baselines, agent_mcp) -> None:
    """Restore every baseline; try them all, then raise if any failed."""
    failures = []
    for baseline in baselines:
        try:
            restore(operator_mcp, baseline, agent_mcp=agent_mcp)
        except RuntimeError as exc:
            failures.append(str(exc))
    if failures:
        raise RuntimeError("; ".join(failures))


class _Matrix:
    """State shared by every attempt of one matrix run."""

    def __init__(self, inventory, agent_mcp_url, agent_token, operator_mcp,
                 agent_mcp, hosts, timeouts):  # fmt: skip
        self.inventory = inventory
        self.agent_mcp_url = agent_mcp_url
        self.agent_token = agent_token
        self.operator_mcp = operator_mcp
        self.agent_mcp = agent_mcp
        self.hosts = hosts
        self.timeouts = timeouts

    def attempt(self, cell, scenario, device, driver: Driver, n: int) -> None:
        """Run one attempt and append its record; restore always runs.

        Raises:
            RuntimeError: If a restore failed; the run must stop.
        """
        # Path-scope records may touch any device, so every device is guarded.
        targets = [device.name] if device else [d.name for d in self.inventory]
        baselines = []
        record = {"n": n, "outcome": "infra_error", "reason": "attempt aborted",
                  "wall_s": None, "tokens": None}  # fmt: skip
        try:
            for name in targets:
                baselines.append(capture_baseline(self.operator_mcp, name))
            if scenario.get("fault"):
                plant_fault(self.operator_mcp, device.name, scenario["fault"])
            record.update(self._client_and_judge(cell, scenario, device, driver,
                                                 baselines))  # fmt: skip
        except (RuntimeError, MCPError, JudgeInfraError) as exc:
            record["reason"] = str(exc)[:500]
        finally:
            cell["attempts"].append(record)
            if record["outcome"] == "infra_error":
                cell["infra_errors"] += 1
            _restore_all(self.operator_mcp, baselines, self.agent_mcp)

    def _client_and_judge(self, cell, scenario, device, driver, baselines) -> dict:
        device_name = device.name if device else None
        timeout_s = self.timeouts.get(cell["client"], DEFAULT_TIMEOUT_S)
        run = driver.run(
            render_prompt(scenario["prompt"], device_name, self.hosts),
            cell["model"],
            self.agent_mcp_url,
            self.agent_token,
            timeout_s,
        )
        if run.model_id:
            cell["model_id"] = run.model_id
        measured = {"wall_s": round(run.wall_seconds, 1), "tokens": _tokens(run.usage)}
        if run.timed_out:
            return {**measured, "outcome": "timeout",
                    "reason": f"client timed out after {timeout_s}s"}  # fmt: skip
        if run.infra_error:
            return {**measured, "outcome": "infra_error", "reason": run.infra_error}
        commits = [
            entry
            for baseline in baselines
            for entry in commits_since(self.operator_mcp, baseline)
        ]
        passed, reason = judge_attempt(
            scenario, device_name, run.final_message, self.operator_mcp, commits
        )
        return {**measured, "outcome": "pass" if passed else "fail", "reason": reason}


def _find_cell(manifest: dict, key: dict) -> dict:
    for cell in manifest["results"]:
        if all(cell.get(field) == value for field, value in key.items()):
            return cell
    cell = {**key, "model_id": None, "attempts": [], "infra_errors": 0,
            "verdict": None, "fails_before_pass": 0}  # fmt: skip
    manifest["results"].append(cell)
    return cell


def run_matrix(
    scenarios: list[dict],
    entries: list[tuple[str, str, Driver]],
    inventory: list[BenchDevice],
    agent_mcp_url: str,
    agent_token: str,
    operator_mcp: MCPClient,
    manifest_path: Path,
    server_version: str,
    golden: dict,
    *,
    hosts: dict[str, str] | None = None,
    agent_mcp=None,
    timeouts: dict[str, int] | None = None,
    resume: bool = False,
) -> dict:
    """Run every (client, model) entry over every scenario and device.

    Args:
        scenarios: Bench scenario dicts (``id``, ``prompt``, optional ``fault``).
        entries: (client name, model, driver) triples.
        inventory: Bench devices.
        agent_mcp_url: MCP endpoint the clients use.
        agent_token: Agent bearer token, handed only to the drivers.
        operator_mcp: Operator client for baseline, fault, judge and restore.
        manifest_path: Manifest file, rewritten after every attempt.
        server_version: MCP server version recorded in the manifest.
        golden: Device name -> golden config fingerprint.
        hosts: Host label (A/B/C) -> address for prompt rendering.
        agent_mcp: Agent client used by restore to confirm pending ops;
            built from the agent URL and token when omitted.
        timeouts: Client name -> per-attempt timeout seconds.
        resume: Continue the manifest at ``manifest_path`` instead of a new run.

    Returns:
        The (unredacted) manifest dict.

    Raises:
        RuntimeError: Golden drift before the first attempt, or a failed
            restore (the manifest is written first; no further attempts run).
        ValueError: A path-scope scenario declares a fault.
    """
    for scenario in scenarios:
        if scenario.get("fault") and scenario.get("device_scope") == "path":
            raise ValueError(f"{scenario['id']}: path-scope scenarios take no fault")
    drifted = check_golden(operator_mcp, inventory, golden)
    if drifted:
        raise RuntimeError(f"golden drift, refusing to start: {', '.join(drifted)}")

    if resume and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        manifest["finished"] = None
    else:
        manifest = {"schema_version": 2, "run_id": str(uuid.uuid4()),
                    "server_version": server_version, "corpus_git_sha": _git_sha(),
                    "started": _now(), "finished": None, "results": []}  # fmt: skip
    # Device names and scenario literals must stay readable for report/resume.
    allowed = {device.name for device in inventory}
    for scenario in scenarios:
        allowed.update(scenario.get("expected_literals") or [])

    if agent_mcp is None:
        agent_mcp = MCPClient(agent_mcp_url, agent_token)
    bench = _Matrix(inventory, agent_mcp_url, agent_token, operator_mcp, agent_mcp,
                    hosts or {}, timeouts or {})  # fmt: skip
    try:
        for client, model, driver in entries:
            for scenario in scenarios:
                for device in devices_for(scenario, inventory):
                    key = {
                        "client": client,
                        "model": model,
                        "test": scenario["id"],
                        "device": device.name if device else None,
                        "platform": device.platform if device else "path",
                    }
                    cell = _find_cell(manifest, key)
                    if cell["verdict"] is not None:
                        continue
                    while (verdict := _verdict(cell)) is None:
                        attempt_number = len(_counted(cell)) + 1
                        bench.attempt(cell, scenario, device, driver, attempt_number)
                        _write(manifest, manifest_path, allowed)
                    cell["verdict"] = verdict
                    cell["fails_before_pass"] = sum(
                        a["outcome"] != "pass" for a in _counted(cell)
                    )
        manifest["finished"] = _now()
    finally:
        _write(manifest, manifest_path, allowed)
    return manifest
