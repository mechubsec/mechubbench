"""Subprocess wrapper around the pinned `mecmcp-redact` CLI binary.

`mechubbench` is Python and cannot link the `mecmcp-redact` Rust crate
directly, so it reaches the same shared denylist-and-shape secret-redaction
engine every mecmcp vendor MCP server uses over stdin/stdout instead (see
mechubsec/mecmcp's `crates/mecmcp-redact/src/bin/mecmcp-redact.rs`). This is
the single source of truth for *secret* redaction in this repo; `redact.py`
still does its own identifier (IP/hostname/serial/user/domain) masking and
config-size digesting locally, since neither has an equivalent in the shared
engine - see `redact.py`'s module docstring.

Fail closed: if the binary cannot be found or exits non-zero, this raises
rather than returning the input unredacted. A manifest write must not
silently skip the mandatory redaction pass just because the pinned binary
isn't built yet.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PINNED_TAG_FILE = Path(__file__).resolve().parent.parent / "mecmcp-redact.pin"
_VENDORED_BINARY = (
    Path(__file__).resolve().parent.parent / ".mecmcp-redact" / "bin" / "mecmcp-redact"
)


class MecmcpRedactUnavailable(RuntimeError):
    """The pinned `mecmcp-redact` binary could not be located or run."""


def pinned_tag() -> str:
    """The mecmcp release tag `scripts/vendor_mecmcp_redact.sh` builds from."""
    return PINNED_TAG_FILE.read_text().strip()


def _binary_path() -> Path:
    """Resolve the `mecmcp-redact` binary: an explicit override first, then
    the vendored build `scripts/vendor_mecmcp_redact.sh` produces, then
    whatever is on PATH.

    The vendored build is preferred over PATH (not the other way around):
    PATH is caller-controlled and can point at any binary with that name, so
    checking it first would let a stale or substituted `mecmcp-redact` -
    built from a different denylist than the one this repo pins - decide
    what gets redacted without anyone noticing. `$MECMCP_REDACT_BIN` is an
    explicit, deliberate override (CI pointing at a binary it already
    built), so it still wins over the vendored path.
    """
    override = os.environ.get("MECMCP_REDACT_BIN")
    if override:
        return Path(override)
    if _VENDORED_BINARY.exists():
        return _VENDORED_BINARY
    on_path = shutil.which("mecmcp-redact")
    if on_path:
        return Path(on_path)
    raise MecmcpRedactUnavailable(
        "mecmcp-redact CLI not found (checked $MECMCP_REDACT_BIN, "
        f"{_VENDORED_BINARY}, and PATH). Run scripts/vendor_mecmcp_redact.sh "
        f"to build the pinned {pinned_tag()} binary before writing a result "
        "manifest."
    )


def _check_version(binary: Path) -> None:
    """Verify `binary --version` matches the pinned tag before first use.

    A `mecmcp-redact` found via `$MECMCP_REDACT_BIN` or PATH is not
    necessarily the pinned build - a different version can carry a
    different denylist/shape set and silently change what gets redacted.
    Fail closed rather than trust the binary's identity.
    """
    expected = f"mecmcp-redact {pinned_tag().lstrip('v')}"
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MecmcpRedactUnavailable(
            f"failed to execute mecmcp-redact at {binary}: {exc}"
        ) from exc
    actual = result.stdout.strip()
    if result.returncode != 0 or actual != expected:
        raise MecmcpRedactUnavailable(
            f"mecmcp-redact at {binary} reports version {actual!r} "
            f"(exit {result.returncode}, stderr: {result.stderr.strip()!r}), "
            f"expected {expected!r}; refusing to "
            "redact with an unverified binary"
        )


def _run(fmt: str, input_text: str) -> str:
    binary = _binary_path()
    _check_version(binary)
    try:
        result = subprocess.run(
            [str(binary), "--format", fmt],
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise MecmcpRedactUnavailable(
            f"mecmcp-redact --format {fmt} timed out after 60s"
        ) from exc
    except OSError as exc:
        raise MecmcpRedactUnavailable(
            f"failed to execute mecmcp-redact at {binary}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise MecmcpRedactUnavailable(
            f"mecmcp-redact --format {fmt} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def redact_text(text: str) -> str:
    """Run the shared denylist-and-shape engine over free text."""
    return _run("text", text)


def redact_json_str(json_text: str) -> str:
    """Run the shared denylist-and-shape engine over a JSON document,
    recursing through every object/array and preserving structure."""
    return _run("json", json_text)
