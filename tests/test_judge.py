"""Tests for the scripted judge (fake MCP client)."""

from mechubbench.judge import judge_attempt
from mechubbench.runner import MCPError


class FakeMCP:
    """Returns canned output per command; records calls."""

    def __init__(self, outputs=None, error=None):
        self.outputs = outputs or {}
        self.error = error
        self.calls = []

    def call_tool(self, tool_name, arguments):
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


def test_device_check_tool_error_fails():
    scenario = _scenario(device_checks=[{"command": "show x"}])
    ok, reason = judge_attempt(scenario, "r1", "", FakeMCP(error=MCPError("boom")), [])
    assert not ok
    assert "boom" in reason


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
