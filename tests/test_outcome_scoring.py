"""Tests for outcome-based scoring (the honest gate)."""

from mechubbench import scoring


def test_outcome_staged_diff_contains_pass():
    """Outcome mode: staged_diff_contains assertions satisfied -> PASS"""
    scenario = {
        "id": "heal-ntp",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [{"tool": "apply_junos_change_set"}],
        "scoring": "outcome",
        "outcome": {
            "staged_diff_contains": ["132.163.97.1", "ntp"],
            "must_not_commit": True,
        },
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set system ntp server 132.163.97.1"}},
    ]
    staged_diff = "+ set system ntp server 132.163.97.1\n+ set system ntp server 128.138.140.44"

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is True
    assert result["scoring_mode"] == "outcome"
    assert "outcome_evidence" in result


def test_outcome_staged_diff_contains_fail():
    """Outcome mode: staged_diff missing required substring -> FAIL"""
    scenario = {
        "id": "heal-ntp",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "staged_diff_contains": ["132.163.97.1", "999.999.999.999"],
        },
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set system ntp server 132.163.97.1"}},
    ]
    staged_diff = "+ set system ntp server 132.163.97.1"

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is False
    assert "999.999.999.999" in result["reason"]
    assert result["scoring_mode"] == "outcome"


def test_outcome_regex_matches_any_pass():
    """Outcome mode: at least one regex matches -> PASS"""
    scenario = {
        "id": "heal-permissive",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "staged_diff_matches_any": ["delete.*demo-bad", "deactivate.*demo-bad"],
        },
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "delete security policies policy demo-bad"}},
    ]
    staged_diff = "+ delete security policies policy demo-bad"

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is True
    assert result["scoring_mode"] == "outcome"


def test_outcome_regex_matches_any_fail():
    """Outcome mode: no regex matches -> FAIL"""
    scenario = {
        "id": "heal-permissive",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "staged_diff_matches_any": ["delete.*demo-bad", "deactivate.*demo-bad"],
        },
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set security policies policy other-rule"}},
    ]
    staged_diff = "+ set security policies policy other-rule"

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is False
    assert "matched none" in result["reason"]
    assert result["scoring_mode"] == "outcome"


def test_outcome_forbidden_enforcement():
    """Outcome mode: forbidden call enforcement unchanged -> FAIL"""
    scenario = {
        "id": "heal-ntp",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [{"tool": "apply_junos_change_set"}],
        "scoring": "outcome",
        "outcome": {
            "staged_diff_contains": ["ntp"],
            "must_not_commit": True,
        },
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set system ntp"}},
        {"tool": "apply_junos_change_set", "args": {}},
    ]
    staged_diff = "+ set system ntp server 132.163.97.1"

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is False
    assert "forbidden" in result["reason"].lower()
    assert result["scoring_mode"] == "outcome"


def test_outcome_discover_report_contains_pass():
    """Outcome mode: discover scenario with report_contains -> PASS"""
    scenario = {
        "id": "discover-ntp",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "report_contains": ["synchronized", "ntp"],
        },
    }
    transcript = [
        {"tool": "execute_junos_command", "args": {"command": "show ntp status"}},
    ]
    final_message = "The device is synchronized with NTP servers and clock is accurate."

    result = scoring.score_scenario(scenario, transcript, final_message=final_message)
    assert result["pass"] is True
    assert result["scoring_mode"] == "outcome"


def test_outcome_discover_report_contains_fail():
    """Outcome mode: discover scenario missing keywords -> FAIL"""
    scenario = {
        "id": "discover-ntp",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "report_contains": ["synchronized", "stratum"],
        },
    }
    transcript = [
        {"tool": "execute_junos_command", "args": {"command": "show ntp status"}},
    ]
    final_message = "The device has NTP configured but status is unclear."

    result = scoring.score_scenario(scenario, transcript, final_message=final_message)
    assert result["pass"] is False
    assert "stratum" in result["reason"]
    assert result["scoring_mode"] == "outcome"


def test_outcome_no_stage_expected_pass():
    """Outcome mode: no_stage_expected with clean diff -> PASS"""
    scenario = {
        "id": "fail-any-any",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "no_stage_expected": True,
        },
    }
    transcript = [
        {"tool": "get_junos_config", "args": {"config_path": "security policies"}},
    ]
    staged_diff = ""  # No changes staged

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is True
    assert result["scoring_mode"] == "outcome"


def test_outcome_no_stage_expected_fail():
    """Outcome mode: no_stage_expected but changes were staged -> FAIL"""
    scenario = {
        "id": "fail-any-any",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "no_stage_expected": True,
        },
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set security policies"}},
    ]
    staged_diff = "+ set security policies policy new-rule"

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is False
    assert "no_stage_expected but changes were staged" in result["reason"]
    assert result["scoring_mode"] == "outcome"


def test_outcome_no_diff_captured_fails():
    """Outcome mode: mutating scenario but no diff captured -> FAIL"""
    scenario = {
        "id": "heal-ntp",
        "vendor": "junos",
        "expected_calls": [],
        "forbidden_calls": [],
        "scoring": "outcome",
        "outcome": {
            "staged_diff_contains": ["ntp"],
        },
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set system ntp"}},
    ]
    staged_diff = None  # Capture failed

    result = scoring.score_scenario(scenario, transcript, staged_diff=staged_diff)
    assert result["pass"] is False
    assert "no staged diff captured" in result["reason"]
    assert result["scoring_mode"] == "outcome"
