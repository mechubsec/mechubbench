"""Shared types for headless client drivers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from mechubbench.mecmcp_redact import redact_text


@dataclass
class AttemptUsage:
    """Token usage reported by a client for one attempt."""

    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None


@dataclass
class ClientRun:
    """Outcome of one headless client run."""

    exit_code: int
    final_message: str
    wall_seconds: float
    usage: AttemptUsage | None  # None = client reported no usage
    model_id: str | None  # exact id the client reports
    infra_error: str | None  # set when the run failed for non-model reasons
    timed_out: bool


class Driver(Protocol):
    """A headless client driver."""

    def run(
        self, prompt: str, model: str, mcp_url: str, token: str, timeout_s: int
    ) -> ClientRun:
        """Run one prompt against the MCP server and return the outcome."""
        ...


def isolated_home(tmp_root: Path) -> Path:
    """Create and return a fresh empty directory under tmp_root."""
    return Path(tempfile.mkdtemp(dir=tmp_root))


def load_clients(path: Path) -> dict[str, dict]:
    """Load bench/clients.yaml."""
    return yaml.safe_load(path.read_text())


Parser = Callable[[str], tuple[str, AttemptUsage | None, str | None]]


def client_env(home: Path, token: str, **extra: str) -> dict[str, str]:
    """Minimal child environment: no inherited agent, session or credential vars."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": str(home),
        "BENCH_TOKEN": token,
        **extra,
    }


def _text(stream: str | bytes | None) -> str:
    if isinstance(stream, bytes):
        return stream.decode(errors="replace")
    return stream or ""


def _first_line(*streams: str) -> str:
    for stream in streams:
        for line in stream.splitlines():
            if line.strip():
                return line.strip()[:500]
    return "client produced no output"


def execute(
    argv: list[str],
    env: dict[str, str],
    home: Path,
    timeout_s: int,
    runner: Callable[..., subprocess.CompletedProcess],
    parse: Parser,
) -> ClientRun:
    """Run one client process in ``home`` and classify the outcome."""
    started = time.monotonic()
    try:
        completed = runner(
            argv,
            env=env,
            cwd=str(home),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        # shortcut: subprocess.run kills only the direct child; grandchildren
        # may linger until they notice the closed pipes.
        final, usage, model_id = parse(_text(exc.stdout))
        return ClientRun(
            exit_code=-1,
            final_message=final,
            wall_seconds=time.monotonic() - started,
            usage=usage,
            model_id=model_id,
            infra_error=None,
            timed_out=True,
        )
    wall_seconds = time.monotonic() - started
    stdout, stderr = _text(completed.stdout), _text(completed.stderr)
    final, usage, model_id = parse(stdout)
    infra_error = None
    if completed.returncode != 0 or not final or usage is None:
        infra_error = redact_text(_first_line(stderr, stdout))
    return ClientRun(
        exit_code=completed.returncode,
        final_message=final,
        wall_seconds=wall_seconds,
        usage=usage,
        model_id=model_id,
        infra_error=infra_error,
        timed_out=False,
    )


def json_lines(stdout: str) -> list[dict]:
    """Parse the JSON objects in a JSONL stream, skipping non-JSON lines."""
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
