"""Tests for core loading and validation functions."""

from pathlib import Path

import jsonschema
import pytest

from mechubbench import core


def test_load_scenario(tmp_path):
    """Load a valid scenario YAML"""
    scenario_file = tmp_path / "test.yaml"
    scenario_file.write_text("""
id: test-01
vendor: junos
setup: "set system host-name test"
prompt: "Check config"
expected_calls:
  - tool: get_junos_config
forbidden_calls: []
scoring: all_expected_present_and_ordered_no_forbidden
""")
    scenario = core.load_scenario(scenario_file)
    assert scenario["id"] == "test-01"
    assert scenario["vendor"] == "junos"


def test_load_scenario_rejects_non_dict(tmp_path):
    """Reject scenario that is not a YAML object"""
    scenario_file = tmp_path / "bad.yaml"
    scenario_file.write_text("- not an object\n")
    with pytest.raises(ValueError, match="must be a YAML object"):
        core.load_scenario(scenario_file)


def test_load_scenarios_from_directory(tmp_path):
    """Load multiple scenarios from directory"""
    s1_content = (
        "id: s1\nvendor: junos\nsetup: ''\nprompt: ''\n"
        "expected_calls: []\nforbidden_calls: []\nscoring: all_expected_present_and_ordered_no_forbidden\n"
    )
    s2_content = (
        "id: s2\nvendor: panos\nsetup: ''\nprompt: ''\n"
        "expected_calls: []\nforbidden_calls: []\nscoring: all_expected_present_and_ordered_no_forbidden\n"
    )
    (tmp_path / "s1.yaml").write_text(s1_content)
    (tmp_path / "s2.yml").write_text(s2_content)
    (tmp_path / "readme.txt").write_text("ignore me")

    scenarios = core.load_scenarios(tmp_path)
    assert len(scenarios) == 2
    assert {s["id"] for s in scenarios} == {"s1", "s2"}


def test_validate_scenario_accepts_valid():
    """Valid scenario passes schema validation"""
    scenario = {
        "id": "test-01",
        "vendor": "junos",
        "setup": "config here",
        "prompt": "task here",
        "expected_calls": [{"tool": "get_junos_config"}],
        "forbidden_calls": [{"tool": "apply_junos_change_set"}],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    schema_path = Path(__file__).parent.parent / "scenarios" / "schema.json"
    core.validate_scenario(scenario, schema_path)  # Should not raise


def test_validate_scenario_rejects_missing_field():
    """Scenario missing required field fails validation"""
    scenario = {
        "id": "test-01",
        # missing vendor, setup, prompt, expected_calls, scoring
    }
    schema_path = Path(__file__).parent.parent / "scenarios" / "schema.json"
    with pytest.raises(jsonschema.ValidationError):
        core.validate_scenario(scenario, schema_path)


def test_load_tools():
    """Load tool definitions from JSON"""
    tools_path = Path(__file__).parent.parent / "tools" / "junos-tools.json"
    tools = core.load_tools(tools_path)
    assert len(tools) == 6
    assert tools[0]["name"] == "get_junos_config"


def test_assemble_manifest():
    """Build a manifest from results"""
    results = [
        {
            "id": "s1",
            "pass": True,
            "reason": "all_expected_present_and_ordered",
            "started": "2026-08-09T00:00:00Z",
            "finished": "2026-08-09T00:00:05Z",
            "transcript": [{"tool": "get_junos_config", "args": {}}],
        },
    ]
    manifest = core.assemble_manifest(
        run_id="test-run-001",
        model="llama3.2:3b",
        corpus_git_sha="abc123",
        started="2026-08-09T00:00:00Z",
        finished="2026-08-09T00:00:10Z",
        results=results,
    )
    assert manifest["schema_version"] == 1
    assert manifest["run_id"] == "test-run-001"
    assert manifest["model"] == "llama3.2:3b"
    assert len(manifest["results"]) == 1
    assert manifest["results"][0]["id"] == "s1"
