"""Tests for CLI commands."""

from pathlib import Path

from mechubbench.cli import cmd_lint


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
