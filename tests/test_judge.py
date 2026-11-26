"""Tests for the scripted judge (fake MCP client)."""

import pytest

from mechubbench.judge import JudgeInfraError, judge_attempt
from mechubbench.runner import MCPError


class FakeMCP:
    """Returns canned output per command; records calls."""

    def __init__(self, outputs=None, error=None):
        self.outputs = outputs or {}
        self.error = error
        self.calls = []

    def call_tool(self, tool_name, arguments, truncate=True):
        self.truncate = truncate
        self.calls.append((tool_name, arguments))
        if self.error:
            raise self.error
        return self.outputs.get(arguments["command"], "")


def _scenario(**extra):
    return {"scoring": "outcome", **extra}


def test_report_keywords_required():
    scenario = _scenario(outcome={"report_contains": ["ospf"]})
    ok, reason = judge_attempt(scenario, "r1", "all good", FakeMCP(), [])
    assert not ok
    assert "ospf" in reason


def test_empty_final_message_fails():
    scenario = _scenario(outcome={"report_contains": ["ospf"]})
    ok, _ = judge_attempt(scenario, "r1", "", FakeMCP(), [])
    assert not ok


def test_report_keywords_pass():
    scenario = _scenario(outcome={"report_contains": ["ospf"]})
    ok, _ = judge_attempt(scenario, "r1", "OSPF is up", FakeMCP(), [])
    assert ok


def test_device_check_must_contain():
    scenario = _scenario(
        device_checks=[{"command": "show x", "must_contain": ["foo"]}]
    )
    assert not judge_attempt(scenario, "r1", "", FakeMCP({"show x": "bar"}), [])[0]
    mcp = FakeMCP({"show x": "foo bar"})
    assert judge_attempt(scenario, "r1", "", mcp, [])[0]
    assert mcp.calls == [
        ("execute_junos_command", {"router_name": "r1", "command": "show x"})
    ]


def test_device_check_match_and_not_contain():
    check = {"command": "show x", "must_match": r"mtu \d+", "must_not_contain": ["bad"]}
    scenario = _scenario(device_checks=[check])
    assert judge_attempt(scenario, "r1", "", FakeMCP({"show x": "mtu 1500"}), [])[0]
    assert not judge_attempt(scenario, "r1", "", FakeMCP({"show x": "nothing"}), [])[0]
    assert not judge_attempt(
        scenario, "r1", "", FakeMCP({"show x": "mtu 1 bad"}), []
    )[0]


def test_device_scope_path_uses_check_device():
    scenario = _scenario(
        device_scope="path",
        device_checks=[{"command": "show x", "device": "r2"}],
    )
    mcp = FakeMCP({"show x": "ok"})
    assert judge_attempt(scenario, None, "", mcp, [])[0]
    assert mcp.calls[0][1]["router_name"] == "r2"


def test_device_check_mcp_error_is_infra_error():
    scenario = _scenario(device_checks=[{"command": "show x"}])
    with pytest.raises(JudgeInfraError):
        judge_attempt(scenario, "r1", "", FakeMCP(error=MCPError("boom")), [])


def test_device_check_reads_untruncated_output():
    scenario = _scenario(
        device_checks=[{"command": "show x", "must_contain": ["TAIL"]}]
    )
    mcp = FakeMCP({"show x": "a" * 9000 + "TAIL"})
    assert judge_attempt(scenario, "r1", "", mcp, [])[0]
    assert mcp.truncate is False


def test_call_tool_truncate_opt_out(monkeypatch):
    from mechubbench.runner import MCPClient

    client = MCPClient.__new__(MCPClient)
    client._request_id = 0
    client._session_id = "s"
    client._post = lambda payload, session_id: None
    big = "a" * 9000 + "TAIL"
    client._parse_response_body = lambda response: {
        "result": {"content": [{"type": "text", "text": big}]}
    }
    assert client.call_tool("t", {}, truncate=False) == big
    assert client.call_tool("t", {}) != big


def test_commit_confirmed_required_plain_commit_fails():
    scenario = _scenario(commit_confirmed_required=True)
    commits = ["0 2026-11-26 10:00:00 UTC by netconf via netconf"]
    ok, reason = judge_attempt(scenario, "r1", "", FakeMCP(), commits)
    assert not ok
    assert "no commit confirmed" in reason


def test_commit_confirmed_required_ok():
    scenario = _scenario(commit_confirmed_required=True)
    commits = ["0 2026-11-26 by netconf commit confirmed, rollback in 10mins"]
    assert judge_attempt(scenario, "r1", "", FakeMCP(), commits)[0]


def test_report_any_of_and_per_device_keywords():
    scenario = _scenario(outcome={
        "report_contains": ["never-used"],
        "report_contains_by_device": {"r1": ["ge-0/0/0", ["br1-users", "151"]]},
    })  # fmt: skip
    assert judge_attempt(scenario, "r1", "ge-0/0/0 VLAN 151", FakeMCP(), [])[0]
    ok, reason = judge_attempt(scenario, "r1", "ge-0/0/0 only", FakeMCP(), [])
    assert not ok and "br1-users" in reason
    assert not judge_attempt(scenario, "r2", "ge-0/0/0 151", FakeMCP(), [])[0]


def test_device_check_limited_to_devices():
    scenario = _scenario(device_checks=[
        {"command": "show a", "devices": ["r1"], "must_contain": ["permit"]},
    ])  # fmt: skip
    mcp = FakeMCP()
    assert judge_attempt(scenario, "r2", "", mcp, [])[0]
    assert mcp.calls == []
    assert not judge_attempt(scenario, "r1", "", mcp, [])[0]
