"""Headless Hermes Agent driver (`hermes chat -q --format stream-json`)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import yaml

from mechubbench.clients.base import (
    AttemptUsage,
    ClientRun,
    client_env,
    execute,
    isolated_home,
    json_lines,
)

PROVIDER = "bench-ollama"


def ollama_models(url: str) -> list[str]:
    """Return the model names an Ollama server offers (GET /api/tags)."""
    response = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=10)
    response.raise_for_status()
    return [model["name"] for model in response.json().get("models", [])]


def parse_hermes(
    stdout: str, usage_file: Path | None
) -> tuple[str, AttemptUsage | None, str | None]:
    """Parse Hermes stream-json into (final, usage, model_id).

    Counts come from ``usage_file`` when it exists, else the stream's result event.
    A result carrying an error or a non-zero exit code has no usage.
    """
    events = json_lines(stdout)
    model_id = next(
        (event.get("model") for event in events if event.get("subtype") == "init"), None
    )
    result = next((event for event in events if event.get("type") == "result"), None)
    if result is None:
        return "", None, model_id
    final = result.get("text") or ""
    if result.get("error") or result.get("exit_code"):
        return final, None, model_id
    if usage_file is not None and usage_file.exists():
        report = json.loads(usage_file.read_text())
        counts = (report.get("input_tokens"), report.get("output_tokens"))
    else:
        tokens = result.get("tokens") or {}
        counts = (tokens.get("input"), tokens.get("output"))
    if counts[0] is None:
        return final, None, model_id
    # Ollama has no prompt cache accounting, so the cache columns stay unknown.
    usage = AttemptUsage(counts[0], counts[1], None, None)
    return final, usage, model_id


class HermesDriver:
    """Runs Hermes with only the bench MCP toolset against an Ollama provider."""

    def __init__(
        self,
        ollama_url: str,
        tmp_root: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.ollama_url = ollama_url.rstrip("/")
        self.tmp_root = tmp_root
        self.runner = runner

    def run(
        self, prompt: str, model: str, mcp_url: str, token: str, timeout_s: int
    ) -> ClientRun:
        """Run one prompt in a throwaway HERMES_HOME and return the outcome."""
        home = isolated_home(self.tmp_root)
        try:
            base_url = f"{self.ollama_url}/v1"
            config = {
                "model": {"provider": PROVIDER, "default": model, "base_url": base_url},
                "providers": {PROVIDER: {"base_url": base_url, "default_model": model}},
                # Hermes interpolates ${BENCH_TOKEN} from the env at load time.
                "mcp_servers": {
                    "bench": {
                        "url": mcp_url,
                        "headers": {"Authorization": "Bearer ${BENCH_TOKEN}"},
                    }
                },
            }
            (home / "config.yaml").write_text(yaml.safe_dump(config))
            argv = [
                "hermes", "chat", "-q", prompt, "--oneshot",
                "--format", "stream-json",
                "--ignore-user-config", "--ignore-rules",
                "-m", model,
                "--provider", PROVIDER,
                "--max-turns", "30",
                # MCP discovery runs after `mcp-bench` is validated, so only the
                # server-name alias selects the bench tools; built-ins stay off.
                "-t", "bench",
                "--source", "tool",
            ]  # fmt: skip
            return execute(
                argv,
                client_env(home, token, HERMES_HOME=str(home)),
                home,
                timeout_s,
                self.runner,
                lambda stdout: parse_hermes(stdout, None),
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)
