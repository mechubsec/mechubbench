"""Tests for the mecmcp-redact CLI subprocess wrapper (MEC-1241).

All secret-shaped values below are fabricated for this test file; none are
derived from real device output or real crypt/SSH-key material.
"""

import os
import re
from pathlib import Path

import pytest

from mechubbench import mecmcp_redact


def test_pinned_tag_is_a_release_tag():
    """mecmcp-redact.pin must name an actual mecmcp release tag (`vX.Y.Z`),
    not a branch name or commit hash, so scripts/vendor_mecmcp_redact.sh
    builds a reproducible, reviewable pin."""
    tag = mecmcp_redact.pinned_tag()
    assert tag.startswith("v")
    assert tag.count(".") == 2


def test_pinned_commit_is_a_full_sha():
    """MEC-1241 review F4: a tag ref can be force-moved to point at
    different content after the fact, so mecmcp-redact.pin alone isn't a
    reproducible pin. mecmcp-redact.commit must record the full 40-character
    commit SHA the tag resolved to when it was pinned, so
    scripts/vendor_mecmcp_redact.sh can verify the checkout against it."""
    commit_file = Path(__file__).resolve().parent.parent / "mecmcp-redact.commit"
    commit = commit_file.read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", commit), commit


def test_binary_path_raises_when_nothing_is_configured(monkeypatch):
    """Fail closed: with no override, nothing on PATH, and no vendored
    build, resolving the binary must raise rather than silently returning a
    best-effort guess a caller might mistake for a found binary."""
    monkeypatch.delenv("MECMCP_REDACT_BIN", raising=False)
    monkeypatch.setattr(mecmcp_redact.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        mecmcp_redact,
        "_VENDORED_BINARY",
        mecmcp_redact._VENDORED_BINARY.parent / "does-not-exist",
    )
    with pytest.raises(mecmcp_redact.MecmcpRedactUnavailable):
        mecmcp_redact._binary_path()


def test_binary_path_honors_explicit_override(monkeypatch, tmp_path):
    """$MECMCP_REDACT_BIN must win even when the file doesn't exist yet -
    the caller declared it explicitly, so resolution shouldn't silently fall
    through to PATH or the vendored build instead."""
    fake_bin = tmp_path / "mecmcp-redact"
    monkeypatch.setenv("MECMCP_REDACT_BIN", str(fake_bin))
    assert mecmcp_redact._binary_path() == fake_bin


def test_binary_path_prefers_vendored_build_over_path(monkeypatch, tmp_path):
    """MEC-1241 review F3: `_binary_path` used to check PATH before the
    vendored build, so any `mecmcp-redact` on PATH - possibly a stale build
    or a different version entirely - silently won over the pinned one with
    no version check. The vendored build must win instead."""
    monkeypatch.delenv("MECMCP_REDACT_BIN", raising=False)
    vendored = tmp_path / "vendored-mecmcp-redact"
    vendored.write_text("")
    monkeypatch.setattr(mecmcp_redact, "_VENDORED_BINARY", vendored)
    monkeypatch.setattr(
        mecmcp_redact.shutil, "which", lambda _name: "/usr/local/bin/mecmcp-redact"
    )
    assert mecmcp_redact._binary_path() == vendored


def test_run_rejects_binary_with_wrong_version(monkeypatch, tmp_path):
    """MEC-1241 review F3: a binary that resolves (via override or PATH) but
    reports a different version than `mecmcp-redact.pin` must be refused
    rather than trusted - it may carry a different denylist/shape set and
    silently change what gets redacted."""
    fake_bin = tmp_path / "fake-mecmcp-redact.sh"
    fake_bin.write_text("#!/bin/sh\necho 'mecmcp-redact 0.0.1-evil'\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("MECMCP_REDACT_BIN", str(fake_bin))
    with pytest.raises(mecmcp_redact.MecmcpRedactUnavailable, match="0.0.1-evil"):
        mecmcp_redact.redact_text("set snmp community FAKEvalue")


@pytest.mark.skipif(
    not os.environ.get("MECMCP_REDACT_BIN"),
    reason="requires a built mecmcp-redact binary (scripts/vendor_mecmcp_redact.sh)",
)
def test_redact_text_calls_the_real_binary():
    out = mecmcp_redact.redact_text('set snmp community "FAKEcommsecret"')
    assert "FAKEcommsecret" not in out
    assert "[REDACTED]" in out


@pytest.mark.skipif(
    not os.environ.get("MECMCP_REDACT_BIN"),
    reason="requires a built mecmcp-redact binary (scripts/vendor_mecmcp_redact.sh)",
)
def test_redact_json_str_calls_the_real_binary():
    out = mecmcp_redact.redact_json_str('{"password": "FAKEpw123"}')
    assert "FAKEpw123" not in out
    assert "[REDACTED]" in out


def test_unavailable_binary_raises_instead_of_returning_input(monkeypatch, tmp_path):
    """A binary path that doesn't exist must raise when invoked (fail
    closed), not silently hand back the unredacted input."""
    monkeypatch.setenv("MECMCP_REDACT_BIN", str(tmp_path / "does-not-exist"))
    with pytest.raises(mecmcp_redact.MecmcpRedactUnavailable):
        mecmcp_redact.redact_text("set snmp community FAKEvalue")


def test_nonzero_exit_raises(monkeypatch, tmp_path):
    """A binary that exits non-zero (e.g. malformed JSON input, which
    mecmcp-redact refuses to pass through unredacted) must surface as
    MecmcpRedactUnavailable, not a silently-truncated or empty result."""
    fake_bin = tmp_path / "fake-mecmcp-redact.sh"
    fake_bin.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("MECMCP_REDACT_BIN", str(fake_bin))
    with pytest.raises(mecmcp_redact.MecmcpRedactUnavailable, match="boom"):
        mecmcp_redact.redact_json_str("not valid json")
