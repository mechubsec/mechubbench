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
    client_env,
    copy_login,
    execute,
    isolated_home,
    run_in_group,
    setup_error,
    write_back,
)

REAL_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


def parse_claude(stdout: str) -> tuple[str, AttemptUsage | None, str | None]:
    """Parse `--output-format json` stdout into (final, usage, model_id).

    An error result or one that billed no model (empty ``modelUsage``) has no usage.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return "", None, None
    try:
        result = json.loads(lines[-1])
    except ValueError:
        return "", None, None
    final = result.get("result") or ""
    models = list(result.get("modelUsage") or {})
    usage = result.get("usage") or {}
    if result.get("is_error") or not models or not usage:
        return final, None, models[0] if models else None
    return (
        final,
        AttemptUsage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_write_tokens=usage.get("cache_creation_input_tokens"),
        ),
        models[0],
    )


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
                parse_claude,
            )
        finally:
            try:
                if snapshot is not None:
                    write_back(login, self.credentials, snapshot)
            finally:
                shutil.rmtree(home, ignore_errors=True)
