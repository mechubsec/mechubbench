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
