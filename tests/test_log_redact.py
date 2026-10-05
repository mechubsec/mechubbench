"""Tests for log-output redaction (MEC-1804, MEC-1241 follow-up).

Manifests and transcripts are redacted before being written to disk, but
log records (which go to stderr, which operators commonly tee to a file)
were not. All secret-shaped values below are fabricated for this test
file; none are derived from real device output.
"""

from __future__ import annotations

import logging

import pytest

from mechubbench import mecmcp_redact, redact, runner


def test_malformed_tool_args_never_logged_raw(caplog):
    """A model's raw tool-call `arguments` string can carry config payloads
    (PSKs, set-commands with secrets); a JSON-parse failure must not echo it
    to the log at all, not even "redacted" - the body is dropped entirely
    (runner.py's warning logs only the length now)."""
    fake_psk = "FAKEtoolargs-psk-do-not-leak-9f3c1a"
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "execute_junos_command",
                                "arguments": f"not-json {{{fake_psk}",
                            },
                        }
                    ]
                }
            }
        ]
    }

    with caplog.at_level(logging.WARNING):
        runner.extract_tool_calls(response)

    assert fake_psk not in caplog.text


def test_log_filter_redacts_secret_shaped_message_when_binary_available(caplog):
    """When the pinned binary is available, the filter must route the
    rendered message through `mecmcp_redact.redact_text` and replace
    secret-shaped content with [REDACTED] rather than passing it through."""
    try:
        mecmcp_redact.redact_text("probe")
    except mecmcp_redact.MecmcpRedactUnavailable:
        pytest.skip("requires a built mecmcp-redact binary (MECMCP_REDACT_BIN)")

    caplog.handler.addFilter(redact.RedactingLogFilter())
    logger = logging.getLogger("mechubbench.test_log_redact.available")

    with caplog.at_level(logging.WARNING):
        logger.warning('set snmp community "FAKEcommsecret"')

    assert "FAKEcommsecret" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_log_filter_withholds_raw_message_when_redaction_unavailable(
    caplog, monkeypatch, tmp_path
):
    """Fail closed: if `mecmcp_redact` can't run (binary missing here), the
    filter must replace the message with a fixed placeholder, never pass
    the raw (unredacted) text through to a handler."""
    monkeypatch.setenv("MECMCP_REDACT_BIN", str(tmp_path / "does-not-exist"))

    caplog.handler.addFilter(redact.RedactingLogFilter())
    logger = logging.getLogger("mechubbench.test_log_redact.unavailable")
    fake_secret = "FAKEunavailable-secret-6f21"

    with caplog.at_level(logging.WARNING):
        logger.warning(f"device error: upstream rejected value {fake_secret}")

    assert fake_secret not in caplog.text
    assert "[log message withheld: redaction unavailable]" in caplog.text
