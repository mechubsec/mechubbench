"""Headless Claude Code driver (`claude -p`)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from mechubbench.clients.base import (
    AttemptUsage,
    ClientRun,
    Parsed,
    client_env,
    copy_login,
    execute,
    isolated_home,
    run_in_group,
    setup_error,
    write_back,
)

REAL_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


RAW_KEYS = ("total_cost_usd", "num_turns", "duration_api_ms", "usage", "modelUsage")
MODEL_USAGE_KEYS = (
    "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"
)  # fmt: skip


def _requested_model(models: list[str], requested: str | None) -> str | None:
    """The modelUsage key for the requested model, else the first key."""
    for key in models:
        if requested and (key == requested or requested in key):
            return key
    return models[0] if models else None


def parse_claude(stdout: str, requested: str | None = None) -> Parsed:
    """Parse `--output-format json` stdout.

    The model id is the ``modelUsage`` key matching ``requested`` (helper
    models such as a Haiku summariser also appear there); usage is summed
    over every ``modelUsage`` entry, since all of them billed this attempt.
    An error result or one that billed no model (empty ``modelUsage``) has
    no usage.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return Parsed("", None, None)
    try:
        result = json.loads(lines[-1])
    except ValueError:
        return Parsed("", None, None)
    final = result.get("result") or ""
    model_usage = result.get("modelUsage") or {}
    model_id = _requested_model(list(model_usage), requested)
    raw = {key: result[key] for key in RAW_KEYS if key in result}
    if result.get("is_error") or not model_usage:
        return Parsed(final, None, model_id, raw)
    totals = [
        sum(entry.get(key) or 0 for entry in model_usage.values())
        for key in MODEL_USAGE_KEYS
    ]
    return Parsed(final, AttemptUsage(*totals), model_id, raw)


class ClaudeCodeDriver:
    """Runs Claude Code with only the bench MCP server and no built-in tools."""

    def __init__(
        self,
        tmp_root: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = run_in_group,
        credentials: Path = REAL_CREDENTIALS,
    ):
        self.tmp_root = tmp_root
        self.runner = runner
        self.credentials = credentials

    def run(
        self, prompt: str, model: str, mcp_url: str, token: str, timeout_s: int
    ) -> ClientRun:
        """Run one prompt in a throwaway HOME and return the outcome."""
        home = isolated_home(self.tmp_root)
        login = home / ".claude" / ".credentials.json"
        snapshot = None
        try:
            login.parent.mkdir(mode=0o700)
            try:
                snapshot = copy_login(self.credentials, login)
            except OSError as exc:
                return setup_error(f"cannot copy Claude login: {exc}")
            mcp_config = home / "mcp.json"
            # The token stays in the env; Claude expands ${BENCH_TOKEN} itself.
            mcp_config.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "bench": {
                                "type": "http",
                                "url": mcp_url,
                                "headers": {"Authorization": "Bearer ${BENCH_TOKEN}"},
                            }
                        }
                    }
                )
            )
            argv = [
                "claude", "-p", prompt,
                "--output-format", "json",
                "--model", model,
                "--strict-mcp-config", "--mcp-config", str(mcp_config),
                "--tools", "",
                "--setting-sources", "",
                "--permission-mode", "bypassPermissions",
                "--allowedTools", "mcp__bench__*",
            ]  # fmt: skip
            return execute(
                argv,
                client_env(home, token),
                home,
                timeout_s,
                self.runner,
                lambda stdout: parse_claude(stdout, model),
            )
        finally:
            try:
                if snapshot is not None:
                    write_back(login, self.credentials, snapshot)
            finally:
                shutil.rmtree(home, ignore_errors=True)
