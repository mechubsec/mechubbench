"""Shared types for headless client drivers."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml


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
