"""Headless Codex driver (`codex exec --json`)."""

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
    json_lines,
    run_in_group,
    setup_error,
    write_back,
)

REAL_AUTH = Path.home() / ".codex" / "auth.json"
REAL_MODELS_CACHE = Path.home() / ".codex" / "models_cache.json"

# Built-in Codex features that give the model tools beyond the bench MCP server.
# Accepted residual built-ins (no switch): list_mcp_resources,
# list_mcp_resource_templates, read_mcp_resource, request_user_input.
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "multi_agent_v2",
    "apps",
    "plugins",
    "remote_plugin",
    "browser_use",
    "browser_use_external",
    "in_app_browser",
    "computer_use",
    "image_generation",
    "view_image",
    "goals",
    "sleep_tool",
    "multi_agent",
    "skill_search",
    "tool_suggest",
    "hooks",
)

# Catalog overrides: models ship as ``code_mode_only`` (every tool behind a JS
# ``exec`` host with a node REPL) plus apply_patch, web search, clock and
# sub-agents. Direct mode with those removed leaves the bench MCP tools.
MODEL_OVERRIDES = {
    "tool_mode": "direct",
    "experimental_supported_tools": [],
    "node_repl_disabled": True,
    "supports_search_tool": False,
}
DROPPED_MODEL_KEYS = (
    "multi_agent_version",
    "apply_patch_tool_type",
    "web_search_tool_type",
)


def isolated_catalog(models_cache: Path) -> dict:
    """Return Codex's cached model catalog with the non-MCP tool surfaces removed."""
    models = json.loads(models_cache.read_text())["models"]
    for entry in models:
        entry.update(MODEL_OVERRIDES)
        for key in DROPPED_MODEL_KEYS:
            entry.pop(key, None)
    return {"models": models}


def parse_codex(stdout: str) -> tuple[str, AttemptUsage | None, str | None]:
    """Parse `codex exec --json` events into (final, usage, model_id).

    Usage is summed over ``turn.completed`` events; Codex reports no model id
    (the driver records the requested one).
    """
    final = ""
    totals: dict[str, int] | None = None
    for event in json_lines(stdout):
        item = event.get("item") or {}
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "agent_message"
        ):
            final = item.get("text") or ""
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not usage:
            continue  # an empty usage block is "not reported", not zero
        totals = totals or {"input": 0, "cached": 0, "output": 0, "write": None}
        cached = usage.get("cached_input_tokens", 0)
        # Codex's input_tokens includes the cached prefix; keep only the uncached.
        totals["input"] += usage.get("input_tokens", 0) - cached
        totals["cached"] += cached
        totals["output"] += usage.get("output_tokens", 0)
        if "cache_write_input_tokens" in usage:
            written = usage["cache_write_input_tokens"]
            totals["write"] = (totals["write"] or 0) + written
    if totals is None:
        return final, None, None
    return (
        final,
        AttemptUsage(
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_read_tokens=totals["cached"],
            cache_write_tokens=totals["write"],
        ),
        None,
    )


class CodexDriver:
    """Runs Codex with only the bench MCP server and its shell tools disabled."""

    def __init__(
        self,
        tmp_root: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = run_in_group,
        auth: Path = REAL_AUTH,
        models_cache: Path = REAL_MODELS_CACHE,
    ):
        self.tmp_root = tmp_root
        self.runner = runner
        self.auth = auth
        self.models_cache = models_cache

    def run(
        self, prompt: str, model: str, mcp_url: str, token: str, timeout_s: int
    ) -> ClientRun:
        """Run one prompt in a throwaway CODEX_HOME and return the outcome."""
        home = isolated_home(self.tmp_root)
        login = home / "auth.json"
        snapshot = None
        try:
            try:
                snapshot = copy_login(self.auth, login)
                models = isolated_catalog(self.models_cache)
            except (OSError, ValueError, KeyError) as exc:
                return setup_error(f"cannot prepare Codex home: {exc!r}")
            if model not in {entry.get("slug") for entry in models["models"]}:
                # Unknown models get Codex's fallback metadata, whose tool
                # surface these overrides were never checked against.
                return setup_error(f"model {model!r} is not in the Codex model cache")
            catalog = home / "model_catalog.json"
            catalog.write_text(json.dumps(models))
            argv = [
                "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                "-m", model,
                "-s", "read-only",
                "-c", f"mcp_servers.bench.url={json.dumps(mcp_url)}",
                "-c", 'mcp_servers.bench.bearer_token_env_var="BENCH_TOKEN"',
                "-c", 'web_search="disabled"',
                "-c", f"model_catalog_json={json.dumps(str(catalog))}",
            ]  # fmt: skip
            for feature in DISABLED_FEATURES:
                argv += ["--disable", feature]
            argv += ["--", prompt]
            run = execute(
                argv,
                client_env(home, token, CODEX_HOME=str(home)),
                home,
                timeout_s,
                self.runner,
                parse_codex,
            )
            # Codex's JSON stream names no model; record the one requested.
            run.model_id = run.model_id or model
            return run
        finally:
            try:
                if snapshot is not None:
                    write_back(login, self.auth, snapshot)
            finally:
                shutil.rmtree(home, ignore_errors=True)
