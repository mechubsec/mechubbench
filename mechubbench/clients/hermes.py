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
    Parsed,
    client_env,
    execute,
    isolated_home,
    json_lines,
    run_in_group,
)

PROVIDER = "bench-ollama"
MAX_TURNS = 30


def ollama_models(url: str) -> list[str]:
    """Return the model names an Ollama server offers (GET /api/tags)."""
    response = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=10)
    response.raise_for_status()
    return [model["name"] for model in response.json().get("models", [])]


def parse_hermes(stdout: str, usage_file: Path | None) -> Parsed:
    """Parse Hermes stream-json.

    Counts come from ``usage_file`` when it exists, else the stream's result
    event. A result carrying an ``error`` (provider/API) has no usage. A
    non-zero ``exit_code`` without one is the ``--max-turns`` cap (captured
    live, fixture ``hermes_max_turns.jsonl``): a model failure.
    """
    events = json_lines(stdout)
    model_id = next(
        (event.get("model") for event in events if event.get("subtype") == "init"), None
    )
    result = next((event for event in events if event.get("type") == "result"), None)
    if result is None:
        return Parsed("", None, model_id)
    final = result.get("text") or ""
    raw = {key: result[key] for key in ("tokens", "duration_ms") if key in result}
    if result.get("error"):
        return Parsed(final, None, model_id, raw)
    # shortcut: Hermes marks the turn cap only by exit_code 1 with no error;
    # if another error-less non-zero exit appears, match it here.
    cap = f"turn cap ({MAX_TURNS}) reached" if result.get("exit_code") else None
    # Hermes' input already excludes the cached prompt (cache_read is separate).
    if usage_file is not None and usage_file.exists():
        report = json.loads(usage_file.read_text())
        keys = ("input_tokens", "output_tokens", "cache_read_tokens")
        counts = [report.get(key) for key in (*keys, "cache_write_tokens")]
    else:
        tokens = result.get("tokens") or {}
        counts = [tokens.get(key) for key in ("input", "output", "cache_read")]
        counts.append(tokens.get("cache_write"))
    if counts[0] is None:
        return Parsed(final, None, model_id, raw)
    return Parsed(final, AttemptUsage(*counts), model_id, raw, cap)


class HermesDriver:
    """Runs Hermes with only the bench MCP toolset against an Ollama provider."""

    max_turns = MAX_TURNS

    def __init__(
        self,
        ollama_url: str,
        tmp_root: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = run_in_group,
    ):
        self.ollama_url = ollama_url.rstrip("/")
        self.tmp_root = tmp_root
        self.runner = runner

    def warm_up(self, model: str) -> str | None:
        """Load ``model`` into Ollama with a one-token generate (untimed).

        Returns:
            None when Ollama answered, else a one-line error for the manifest.
        """
        try:
            response = httpx.post(
                f"{self.ollama_url}/api/generate",
                json={"model": model, "prompt": "ok", "stream": False,
                      "keep_alive": "30m", "options": {"num_predict": 1}},
                timeout=600,
            )  # fmt: skip
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return f"Ollama warm-up of {model} failed: {str(exc)[:200]}"
        return None

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
                "--max-turns", str(MAX_TURNS),
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
