"""Tests for scripts/redact_results.py, which re-applies the current
mechubbench.redact masker to committed results/*.json in place.

All addresses/hostnames below are synthetic.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "redact_results.py"
_spec = importlib.util.spec_from_file_location("redact_results", _SCRIPT_PATH)
redact_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redact_results)


def _write_manifest(path: Path, results: list[dict], **extra) -> None:
    manifest = {"results": results, **extra}
    path.write_text(json.dumps(manifest, indent=2))


def test_redact_file_masks_a_public_ip_left_by_an_older_masker(tmp_path):
    """Encodes the bug this script exists to fix: a results file committed
    under an older, RFC1918-only masker still has a public IP in tool_error
    text. Re-running the current masker over it in place must mask it.
    """
    path = tmp_path / "old-run.json"
    _write_manifest(
        path,
        [
            {
                "pass": True,
                "steps": [
                    {
                        "tool": "get_junos_config",
                        "tool_error": "connect to 198.18.0.9:22 failed",
                    }
                ],
            }
        ],
    )

    changed = redact_results.redact_file(path, allowed_literals=set())

    assert changed is True
    new_text = path.read_text()
    assert "198.18.0.9" not in new_text
    assert "<IP-1>" in new_text


def test_redact_file_leaves_an_already_clean_file_unchanged(tmp_path):
    path = tmp_path / "clean.json"
    _write_manifest(
        path,
        [{"pass": True, "steps": [{"tool": "noop", "tool_error": None}]}],
    )
    original = path.read_text()

    changed = redact_results.redact_file(path, allowed_literals=set())

    assert changed is False
    assert path.read_text() == original


def test_redact_file_is_idempotent(tmp_path):
    path = tmp_path / "run.json"
    _write_manifest(
        path,
        [
            {
                "pass": False,
                "steps": [
                    {"tool": "x", "tool_error": "unreachable at 198.18.0.9"}
                ],
            }
        ],
    )

    first = redact_results.redact_file(path, allowed_literals=set())
    once_redacted = path.read_text()
    second = redact_results.redact_file(path, allowed_literals=set())

    assert first is True
    assert second is False
    assert path.read_text() == once_redacted


def test_redact_file_respects_declared_expected_literals(tmp_path):
    path = tmp_path / "run.json"
    _write_manifest(
        path,
        [{"pass": True, "steps": [{"tool": "x", "tool_error": "seen 8.8.8.8"}]}],
    )

    redact_results.redact_file(path, allowed_literals={"8.8.8.8"})

    assert "8.8.8.8" in path.read_text()


def test_redact_file_refuses_to_write_if_pass_fail_counts_would_change(
    tmp_path, monkeypatch
):
    path = tmp_path / "run.json"
    _write_manifest(path, [{"pass": True, "steps": []}])
    original = path.read_text()

    def _tamper(manifest, allowed_literals=None):
        tampered = json.loads(json.dumps(manifest))
        tampered["results"][0]["pass"] = False
        return tampered

    monkeypatch.setattr(redact_results.redact, "redact_manifest", _tamper)

    with pytest.raises(RuntimeError, match="pass/fail counts changed"):
        redact_results.redact_file(path, allowed_literals=set())

    # Refusing to write means the file on disk is untouched.
    assert path.read_text() == original


def test_main_reports_no_changes_on_a_second_run(tmp_path, capsys):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    _write_manifest(
        results_dir / "a.json",
        [{"pass": True, "steps": [{"tool": "x", "tool_error": "at 8.8.4.4"}]}],
    )

    argv = [
        "--results-dir",
        str(results_dir),
        "--scenarios-dir",
        str(scenarios_dir),
    ]
    redact_results.main(argv)
    capsys.readouterr()
    redact_results.main(argv)
    out = capsys.readouterr().out

    assert "No changes" in out
