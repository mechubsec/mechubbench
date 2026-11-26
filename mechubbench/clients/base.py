"""Shared types for headless client drivers."""

from __future__ import annotations

import json
import os
import signal
import stat
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
    raw_usage: dict | None = None  # client's own usage fields, token-safe keys
    model_failure: str | None = None  # model-side stop (turn cap): a fail


@dataclass
class Parsed:
    """What a client parser read from one run's stdout."""

    final: str
    usage: AttemptUsage | None
    model_id: str | None
    raw_usage: dict | None = None
    model_failure: str | None = None


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


Parser = Callable[[str], Parsed]


def token_safe(value):
    """Rename ``*tokens*`` keys so the manifest's secret redactor keeps counts.

    The shared redactor treats any key naming a token as a secret and blanks
    it; ``input_tokens`` becomes ``input_tok``, ``inputTokens`` ``inputTok``.
    """
    if isinstance(value, dict):
        return {
            key.replace("tokens", "tok").replace("Tokens", "Tok"): token_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [token_safe(item) for item in value]
    return value


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


def run_in_group(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` in a new session; on timeout kill the whole group.

    SIGTERM first, SIGKILL after 5 s, so grandchildren (MCP helpers, node
    workers) do not outlive the attempt. Re-raises ``TimeoutExpired``; any
    other exception while waiting (KeyboardInterrupt, the SystemExit the
    matrix's SIGTERM handler raises) also kills the group, then re-raises.
    """
    timeout = kwargs.pop("timeout", None)
    stdin = kwargs.pop("stdin", None)
    if kwargs.pop("capture_output", False):
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with subprocess.Popen(
        argv, stdin=stdin, start_new_session=True, **kwargs
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_group(process)
            try:
                exc.stdout, exc.stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass  # a setsid'd grandchild still holds the pipes; stop reading
            raise
        except BaseException:
            _kill_group(process)
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _kill_group(process: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def copy_login(original: Path, copy: Path) -> bytes:
    """Copy a client login file to ``copy`` (0600); return the bytes copied."""
    snapshot = original.read_bytes()
    copy.write_bytes(snapshot)
    copy.chmod(0o600)
    return snapshot


def write_back(copy: Path, original: Path, snapshot: bytes) -> None:
    """Atomically replace ``original`` with ``copy`` if the client rewrote it.

    Clients rotate OAuth refresh tokens inside the throwaway home; without
    this the user's real login would hold a revoked token. Comparing against
    the ``snapshot`` taken at copy time (not the current original) keeps a
    login the user's own session refreshed meanwhile. Contents are never
    logged.
    """
    if not copy.exists() or copy.read_bytes() == snapshot:
        return
    mode = stat.S_IMODE(original.stat().st_mode)
    handle, staging = tempfile.mkstemp(dir=original.parent, prefix=".bench-")
    try:
        with os.fdopen(handle, "wb") as staged:
            staged.write(copy.read_bytes())
        os.chmod(staging, mode)
        os.replace(staging, original)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


def setup_error(message: str) -> ClientRun:
    """A run that never started because its isolated home could not be built."""
    return ClientRun(
        exit_code=-1,
        final_message="",
        wall_seconds=0.0,
        usage=None,
        model_id=None,
        infra_error=redact_text(message),
        timed_out=False,
    )


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
        parsed = parse(_text(exc.stdout))
        return ClientRun(
            exit_code=-1,
            final_message=parsed.final,
            wall_seconds=time.monotonic() - started,
            usage=parsed.usage,
            model_id=parsed.model_id,
            infra_error=None,
            timed_out=True,
            raw_usage=token_safe(parsed.raw_usage),
        )
    wall_seconds = time.monotonic() - started
    stdout, stderr = _text(completed.stdout), _text(completed.stderr)
    parsed = parse(stdout)
    # No usage = the model never ran (provider, auth, startup, transport).
    # With usage, only a parser-recognised model stop excuses a non-zero
    # exit; an empty final message is judged (and fails), not infra.
    infra_error = None
    if parsed.usage is None or (
        completed.returncode != 0 and parsed.model_failure is None
    ):
        infra_error = redact_text(_first_line(stderr, stdout))
    return ClientRun(
        exit_code=completed.returncode,
        final_message=parsed.final,
        wall_seconds=wall_seconds,
        usage=parsed.usage,
        model_id=parsed.model_id,
        infra_error=infra_error,
        timed_out=False,
        raw_usage=token_safe(parsed.raw_usage),
        model_failure=None if infra_error else parsed.model_failure,
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
