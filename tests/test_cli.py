"""Tests for CLI commands."""

import json
from pathlib import Path
from unittest.mock import patch

from mechubbench.cli import build_parser, cmd_lint, cmd_run


def test_lint_valid_scenario_passes(tmp_path):
    """Valid scenario passes lint"""
    scenario_file = tmp_path / "test.yaml"
    scenario_file.write_text("""
id: test-valid
vendor: junos
setup: "set system host-name test"
prompt: "Check config"
expected_calls:
  - tool: get_junos_config
forbidden_calls:
  - tool: apply_junos_change_set
scoring: all_expected_present_and_ordered_no_forbidden
""")

    class Args:
        scenarios = str(tmp_path)

    # Should return 0 (success)
    result = cmd_lint(Args())
    assert result == 0


def test_lint_invalid_scenario_fails(tmp_path):
    """Invalid scenario fails lint"""
    scenario_file = tmp_path / "bad.yaml"
    scenario_file.write_text("""
id: bad-scenario
# missing required fields: vendor, setup, prompt, expected_calls, scoring
""")

    class Args:
        scenarios = str(tmp_path)

    # Should return 1 (failure)
    result = cmd_lint(Args())
    assert result == 1


def test_lint_validates_real_scenario():
    """Lint the actual shipped scenario"""
    scenarios_dir = Path(__file__).parent.parent / "scenarios"

    class Args:
        scenarios = str(scenarios_dir)

    result = cmd_lint(Args())
    assert result == 0


def test_lint_rejects_missing_forbidden_calls(tmp_path):
    """Lint rejects scenarios missing required forbidden_calls field."""
    scenario_file = tmp_path / "bad.yaml"
    scenario_file.write_text("""
id: test-missing-forbidden
vendor: junos
setup: "test setup"
prompt: "test prompt"
expected_calls:
  - tool: get_junos_config
scoring: all_expected_present_and_ordered_no_forbidden
""")

    class Args:
        scenarios = str(tmp_path)

    result = cmd_lint(Args())
    assert result == 1


def test_lint_rejects_unknown_vendor(tmp_path):
    """Lint rejects scenarios with unknown vendor values."""
    scenario_file = tmp_path / "bad-vendor.yaml"
    scenario_file.write_text("""
id: test-unknown-vendor
vendor: cisco-ios
setup: "test setup"
prompt: "test prompt"
expected_calls:
  - tool: some_tool
forbidden_calls:
  - tool: apply_change
scoring: all_expected_present_and_ordered_no_forbidden
""")

    class Args:
        scenarios = str(tmp_path)

    result = cmd_lint(Args())
    assert result == 1


class TestNoFlagsStaysOnLoopback:
    """MEC-27 M3: with no flags, no request leaves loopback."""

    def test_run_endpoint_defaults_to_loopback(self):
        parser = build_parser()
        args = parser.parse_args(
            ["run", "--model", "test-model", "--scenarios", "scenarios", "--out", "/tmp/out.json"]
        )
        assert args.endpoint == "http://127.0.0.1:11434/v1"

    def test_run_endpoint_default_is_not_a_remote_host(self):
        from urllib.parse import urlparse

        parser = build_parser()
        args = parser.parse_args(
            ["run", "--model", "test-model", "--scenarios", "scenarios", "--out", "/tmp/out.json"]
        )
        assert urlparse(args.endpoint).hostname in ("127.0.0.1", "localhost")


class TestMCPEndpointNoLabDefault:
    """Percy F1: --mcp-endpoint must not default to a real lab host; agentic
    mode must supply it explicitly."""

    def test_run_mcp_endpoint_has_no_default(self):
        parser = build_parser()
        args = parser.parse_args(
            ["run", "--model", "test-model", "--scenarios", "scenarios", "--out", "/tmp/out.json"]
        )
        assert args.mcp_endpoint is None

    def test_no_lab_ip_in_cli_or_runner_source(self):
        repo_root = Path(__file__).parent.parent
        for relative_path in ("mechubbench/cli.py", "mechubbench/runner.py"):
            text = (repo_root / relative_path).read_text()
            assert "198.51.100.194" not in text, f"lab IP leaked into {relative_path}"

    def test_agentic_mode_without_mcp_endpoint_fails(self, tmp_path):
        scenarios_dir = tmp_path / "scenarios"
        scenarios_dir.mkdir()
        (scenarios_dir / "test.yaml").write_text("""
id: test-agentic
vendor: junos
setup: "set system host-name test"
prompt: "Check config"
expected_calls:
  - tool: get_junos_config
forbidden_calls:
  - tool: apply_junos_change_set
scoring: all_expected_present_and_ordered_no_forbidden
""")
        tools_path = tmp_path / "tools.json"
        tools_path.write_text(
            json.dumps(
                [
                    {
                        "name": "get_junos_config",
                        "description": "Get config",
                        "parameters": {
                            "type": "object",
                            "properties": {"device": {"type": "string"}},
                        },
                    }
                ]
            )
        )

        class Args:
            scenarios = str(scenarios_dir)
            tools = str(tools_path)
            out = str(tmp_path / "results" / "out.json")
            mode = "agentic"
            mcp_endpoint = None
            model = "test-model"

        result = cmd_run(Args())
        assert result == 1


class TestTokensFromFileOrEnvOnly:
    """MEC-27 Low: tokens are accepted from a file or the environment, never a bare CLI value."""

    def test_mcp_token_file_flag_replaces_bare_token_flag(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "--model",
                "test-model",
                "--scenarios",
                "scenarios",
                "--out",
                "/tmp/out.json",
                "--mcp-token-file",
                "/tmp/token-file",
            ]
        )
        assert args.mcp_token_file == "/tmp/token-file"
        assert not hasattr(args, "mcp_token")

    def test_bare_mcp_token_flag_is_rejected(self):
        """--mcp-token no longer exists; allow_abbrev=False stops it silently
        prefix-matching --mcp-token-file."""
        import pytest

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "run",
                    "--model",
                    "test-model",
                    "--scenarios",
                    "scenarios",
                    "--out",
                    "/tmp/out.json",
                    "--mcp-token",
                    "leaked-on-the-command-line",
                ]
            )


class TestResultFileRedaction:
    """MEC-27 M4: a result file written from a synthetic run has secret shapes redacted."""

    def test_written_manifest_has_secrets_redacted(self, tmp_path):
        scenarios_dir = tmp_path / "scenarios"
        scenarios_dir.mkdir()
        (scenarios_dir / "test.yaml").write_text("""
id: test-secret
vendor: junos
setup: "set system host-name test"
prompt: "Check config"
expected_calls:
  - tool: get_junos_config
forbidden_calls:
  - tool: apply_junos_change_set
scoring: all_expected_present_and_ordered_no_forbidden
""")
        tools_path = tmp_path / "tools.json"
        tools_path.write_text(
            json.dumps(
                [
                    {
                        "name": "get_junos_config",
                        "description": "Get config",
                        "parameters": {
                            "type": "object",
                            "properties": {"device": {"type": "string"}},
                        },
                    }
                ]
            )
        )
        out_path = tmp_path / "results" / "synthetic.json"

        secret_manifest = {
            "schema_version": 1,
            "run_id": "synthetic",
            "model": "test-model",
            "results": [
                {
                    "id": "test-secret",
                    "pass": True,
                    "reason": "ok",
                    "transcript": [
                        {
                            "tool": "get_junos_config",
                            "args": {
                                "device": "vsrx-ci",
                                "text": (
                                    'encrypted-password "$6$FAKEsalt$FAKEhashdata12345"; '
                                    'secret "FAKEradiussecret"; '
                                    'community "FAKEcommunitystring"; '
                                    "host 192.168.5.9;"
                                ),
                            },
                        }
                    ],
                }
            ],
        }

        class Args:
            scenarios = str(scenarios_dir)
            tools = str(tools_path)
            out = str(out_path)
            mode = "blind"
            endpoint = "http://127.0.0.1:11434/v1"
            temperature = 0.0
            model = "test-model"

        with patch("mechubbench.runner.run_all_scenarios", return_value=secret_manifest):
            result = cmd_run(Args())

        assert result == 0
        written_blob = out_path.read_text()
        for secret_shape in [
            "$6$FAKEsalt$FAKEhashdata12345",
            "FAKEradiussecret",
            "FAKEcommunitystring",
            "192.168.5.9",
        ]:
            assert secret_shape not in written_blob, f"{secret_shape!r} leaked into results/*.json"
