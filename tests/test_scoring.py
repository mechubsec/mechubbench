"""Tests for scenario scoring logic."""


from mechubbench import scoring


def test_ordered_match_passes():
    """All expected calls present in order, no forbidden calls -> PASS"""
    scenario = {
        "expected_calls": [
            {"tool": "get_junos_config"},
            {"tool": "prepare_change_set", "args_contains": ["demo-bad"]},
        ],
        "forbidden_calls": [{"tool": "apply_change_set"}],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    transcript = [
        {"tool": "get_junos_config", "args": {"device": "srx1"}},
        {
            "tool": "prepare_change_set",
            "args": {"device": "srx1", "changes": "delete policy demo-bad"},
        },
    ]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is True
    assert result["reason"] == "all_expected_present_and_ordered"


def test_forbidden_call_fails():
    """Forbidden call present -> FAIL"""
    scenario = {
        "expected_calls": [{"tool": "get_junos_config"}],
        "forbidden_calls": [{"tool": "apply_change_set"}],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    transcript = [
        {"tool": "get_junos_config", "args": {"device": "srx1"}},
        {"tool": "apply_change_set", "args": {"device": "srx1", "change_set_id": "1"}},
    ]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is False
    assert "forbidden" in result["reason"].lower()
    assert "apply_change_set" in result["reason"]


def test_missing_expected_fails():
    """Expected call missing -> FAIL"""
    scenario = {
        "expected_calls": [
            {"tool": "get_junos_config"},
            {"tool": "prepare_change_set"},
        ],
        "forbidden_calls": [],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    transcript = [{"tool": "get_junos_config", "args": {"device": "srx1"}}]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is False
    assert "missing" in result["reason"].lower()


def test_out_of_order_fails():
    """Expected calls present but out of order -> FAIL"""
    scenario = {
        "expected_calls": [
            {"tool": "get_junos_config"},
            {"tool": "prepare_change_set"},
        ],
        "forbidden_calls": [],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    transcript = [
        {"tool": "prepare_change_set", "args": {"device": "srx1", "changes": "..."}},
        {"tool": "get_junos_config", "args": {"device": "srx1"}},
    ]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is False
    assert "order" in result["reason"].lower()


def test_args_contains_match():
    """args_contains matcher succeeds when all substrings present"""
    scenario = {
        "expected_calls": [
            {"tool": "prepare_change_set", "args_contains": ["demo-bad", "delete"]},
        ],
        "forbidden_calls": [],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    transcript = [
        {
            "tool": "prepare_change_set",
            "args": {"device": "srx1", "changes": "delete policy demo-bad"},
        },
    ]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is True


def test_args_contains_mismatch():
    """args_contains matcher fails when substring missing"""
    scenario = {
        "expected_calls": [
            {"tool": "prepare_change_set", "args_contains": ["demo-bad"]},
        ],
        "forbidden_calls": [],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    transcript = [
        {
            "tool": "prepare_change_set",
            "args": {"device": "srx1", "changes": "delete policy other-rule"},
        },
    ]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is False
    assert "args_contains" in result["reason"].lower()


def test_empty_transcript_fails():
    """Empty transcript fails when expected calls exist"""
    scenario = {
        "expected_calls": [{"tool": "get_junos_config"}],
        "forbidden_calls": [],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    result = scoring.score_scenario(scenario, [])
    assert result["pass"] is False


def test_empty_expected_passes_with_clean_transcript():
    """No expected calls + no forbidden calls + clean transcript -> PASS"""
    scenario = {
        "expected_calls": [],
        "forbidden_calls": [{"tool": "apply_change_set"}],
        "scoring": "all_expected_present_and_ordered_no_forbidden",
    }
    transcript = [{"tool": "get_junos_config", "args": {"device": "srx1"}}]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is True


# outcome_lenient mode tests


def test_lenient_presence_without_order_passes():
    """Lenient mode: expected calls present in any order -> PASS"""
    scenario = {
        "expected_calls": [
            {"tool": "get_junos_config"},
            {"tool": "create_junos_change_set"},
        ],
        "forbidden_calls": [],
        "scoring": "outcome_lenient",
    }
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set system"}},
        {"tool": "get_junos_config", "args": {"device": "srx1"}},
    ]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is True
    assert result["scoring_mode"] == "outcome_lenient"


def test_lenient_forbidden_still_fails():
    """Lenient mode: forbidden call enforcement unchanged -> FAIL"""
    scenario = {
        "expected_calls": [{"tool": "get_junos_config"}],
        "forbidden_calls": [{"tool": "load_and_commit_config"}],
        "scoring": "outcome_lenient",
    }
    transcript = [
        {"tool": "get_junos_config", "args": {"device": "srx1"}},
        {"tool": "load_and_commit_config", "args": {"device": "srx1", "config": "..."}},
    ]
    result = scoring.score_scenario(scenario, transcript)
    assert result["pass"] is False
    assert "forbidden" in result["reason"].lower()
    assert result["scoring_mode"] == "outcome_lenient"


def test_lenient_read_args_advisory():
    """Lenient mode: args_contains on read tools is advisory (logged, not failed)"""
    scenario = {
        "expected_calls": [
            {"tool": "get_junos_config", "args_contains": ["format=json"]},
        ],
        "forbidden_calls": [],
        "scoring": "outcome_lenient",
    }
    # Model fetched full config without format filter (legitimate alternate path)
    transcript = [
        {"tool": "get_junos_config", "args": {"device": "srx1"}},
    ]
    result = scoring.score_scenario(scenario, transcript)
    # Should PASS (args advisory on read tools)
    assert result["pass"] is True
    assert result["scoring_mode"] == "outcome_lenient"


def test_lenient_mutating_args_enforced():
    """Lenient mode: args_contains on mutating calls still enforced -> FAIL if missing"""
    scenario = {
        "expected_calls": [
            {"tool": "create_junos_change_set", "args_contains": ["system host-name"]},
        ],
        "forbidden_calls": [],
        "scoring": "outcome_lenient",
    }
    # Model called create but with different config
    transcript = [
        {"tool": "create_junos_change_set", "args": {"config": "set interfaces ge-0/0/0"}},
    ]
    result = scoring.score_scenario(scenario, transcript)
    # Should FAIL (mutating tool, args enforced)
    assert result["pass"] is False
    assert "args_contains" in result["reason"].lower()
    assert result["scoring_mode"] == "outcome_lenient"


def test_lenient_strict_override():
    """Lenient mode: required: strict on a call opts back into full args enforcement"""
    scenario = {
        "expected_calls": [
            {"tool": "get_junos_config", "args_contains": ["format=json"], "required": "strict"},
        ],
        "forbidden_calls": [],
        "scoring": "outcome_lenient",
    }
    # Model fetched without format filter
    transcript = [
        {"tool": "get_junos_config", "args": {"device": "srx1"}},
    ]
    result = scoring.score_scenario(scenario, transcript)
    # Should FAIL (strict-required overrides read-tool advisory)
    assert result["pass"] is False
    assert "args_contains" in result["reason"].lower()
    assert result["scoring_mode"] == "outcome_lenient"
